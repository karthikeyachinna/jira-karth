#!/usr/bin/env python3
"""Apply gocd/access.yaml to GoCD: login, users, system admins, roles, group permissions.

    .venv/bin/python gocd/auth/setup_auth.py            (idempotent - re-run after any edit)
    .venv/bin/python gocd/auth/setup_auth.py --dry-run  (show what would be applied)

access.yaml is the source of truth (no secrets in it). Passwords live only in .env:
GOCD_PASSWORD_<LOGIN> per user, GOCD_PASSWORD for the service account. Missing passwords are
generated into .env and never printed. GoCD only receives bcrypt hashes.

Steps:
  1. passwords.properties (bcrypt) -> GoCD server container
  2. "file-auth" authorization config (password-file plugin)  -> login is required
  3. users in Admin > Users: create / update email + enabled; users not in the YAML are disabled
  4. roles; system admins; pipeline group permissions; stale GoCD roles removed
  5. service account (GOCD_USER / GOCD_PASSWORD) on the jira-release environment, for the jobs

Locked out? gocd/auth/disable_auth.sh turns login off again.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import bcrypt
import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
ACCESS_FILE = ROOT / "gocd" / "access.yaml"
GOCD = os.environ.get("GOCD_URL", "http://localhost:8153/go").rstrip("/")
CONTAINER_PATH = "/godata/config/passwords.properties"
AUTH_ID = "file-auth"
PLUGIN = "cd.go.authentication.passwordfile"
ENVIRONMENT = "jira-release"
APPROVAL_GROUP = "jira-approval"
DRY = "--dry-run" in sys.argv


# ---------------------------------------------------------------- .env
def read_env() -> dict[str, str]:
    out = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def set_env(key: str, value: str) -> None:
    text = ENV_FILE.read_text()
    if re.search(rf"^{re.escape(key)}=", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", lambda _: f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_FILE.write_text(text)


def password_key(login: str, service_account: str) -> str:
    if login == service_account:
        return "GOCD_PASSWORD"
    return "GOCD_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", login).upper()


def ensure_password(env: dict, key: str) -> str:
    if not env.get(key):
        env[key] = secrets.token_urlsafe(18)
        if not DRY:
            set_env(key, env[key])
        print(f"  .env: {key} generated")
    return env[key]


# ---------------------------------------------------------------- GoCD API
def api(method: str, path: str, version: int, auth=None, ok=(200, 201, 204, 404), **kw):
    headers = {"Accept": f"application/vnd.go.cd.v{version}+json",
               "Content-Type": "application/json", **kw.pop("headers", {})}
    r = requests.request(method, f"{GOCD}/api{path}", headers=headers, auth=auth,
                         timeout=30, **kw)
    if r.status_code not in ok:
        sys.exit(f"GoCD {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r


def server_container() -> str:
    if os.environ.get("GOCD_SERVER_CONTAINER"):
        return os.environ["GOCD_SERVER_CONTAINER"]
    names = subprocess.run(
        ["docker", "ps", "--filter", "ancestor=gocd/gocd-server:v26.1.0", "--format",
         "{{.Names}}"], capture_output=True, text=True, check=True).stdout.split()
    if not names:
        sys.exit("GoCD server container not found (set GOCD_SERVER_CONTAINER)")
    return names[0]


def hash_pw(password: str) -> str:
    # $2a$ = same bcrypt algorithm, the prefix every Java bcrypt library accepts
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode().replace("$2b$", "$2a$", 1)


def perms(spec: dict | None) -> dict:
    spec = spec or {}
    return {"users": list(spec.get("users", [])), "roles": list(spec.get("roles", []))}


def approver_logins(cfg: dict) -> list[str]:
    by_email = {(u.get("email") or "").lower(): u["login"] for u in cfg.get("users", [])}
    return [by_email[e.lower()] for e in (cfg.get("approval") or {}).get("emails", [])
            if e.lower() in by_email]


# ---------------------------------------------------------------- validate
def load_access() -> dict:
    cfg = yaml.safe_load(ACCESS_FILE.read_text()) or {}
    logins = [u["login"] for u in cfg.get("users", [])]
    roles = cfg.get("roles", {}) or {}
    errors = []
    sa = cfg.get("service_account")
    if sa not in logins:
        errors.append(f"service_account {sa!r} is not in users")
    if sa not in cfg.get("system_admins", {}).get("users", []):
        errors.append(f"service_account {sa!r} must be a system admin (pipelines edit config)")
    for role, members in roles.items():
        errors += [f"role {role}: unknown user {m!r}" for m in members if m not in logins]
    referenced = list(cfg.get("system_admins", {}).get("roles", []))
    referenced += (cfg.get("approval") or {}).get("roles", [])
    emails = {(u.get("email") or "").lower(): u for u in cfg.get("users", []) if u.get("email")}
    for e in (cfg.get("approval") or {}).get("emails", []):
        if e.lower() not in emails:
            errors.append(f"approval email {e!r} does not belong to any user in users:")
        elif not emails[e.lower()].get("enabled", True):
            errors.append(f"approval email {e!r} belongs to a disabled user")
    if not (cfg.get("approval") or {}).get("emails") and not (cfg.get("approval") or {}).get("roles"):
        errors.append("approval: list at least one approver email (or role)")
    for g in (cfg.get("groups") or {}).values():
        for level in ("view", "operate", "admins"):
            referenced += (g or {}).get(level, {}).get("roles", [])
            errors += [f"unknown user {u!r} in groups" for u in (g or {}).get(level, {}).get("users", [])
                       if u not in logins]
    errors += [f"unknown role {r!r}" for r in referenced if r not in roles]
    if errors:
        sys.exit("access.yaml is invalid:\n  - " + "\n  - ".join(errors))
    return cfg


# ---------------------------------------------------------------- main
def main() -> None:
    cfg = load_access()
    env = read_env()
    sa = cfg["service_account"]
    users = cfg["users"]
    print(f"access.yaml: {len(users)} users, {len(cfg.get('roles', {}))} roles, "
          f"{len(cfg.get('groups') or {})} groups{'  (dry run)' if DRY else ''}")

    passwords = {u["login"]: ensure_password(env, password_key(u["login"], sa)) for u in users}
    if not DRY and env.get("GOCD_USER") != sa:
        set_env("GOCD_USER", sa)

    # 1. password file (enabled users only - a disabled user cannot log in)
    lines = [f"{u['login']}={hash_pw(passwords[u['login']])}" for u in users
             if u.get("enabled", True)]
    print(f"1. password file: {len(lines)} login(s)")
    if not DRY:
        with tempfile.NamedTemporaryFile("w", suffix=".properties", delete=False) as fh:
            fh.write("# managed by gocd/auth/setup_auth.py from gocd/access.yaml\n")
            fh.write("\n".join(lines) + "\n")
        os.chmod(fh.name, 0o644)
        subprocess.run(["docker", "cp", fh.name, f"{server_container()}:{CONTAINER_PATH}"],
                       check=True)
        os.unlink(fh.name)

    # 2. authorization config (first run: GoCD is still open, so no credentials needed)
    auth = (sa, passwords[sa])
    open_ = api("GET", "/admin/security/auth_configs", 2, ok=(200, 401, 403)).status_code == 200
    body = {"id": AUTH_ID, "plugin_id": PLUGIN, "allow_only_known_users_to_login": False,
            "properties": [{"key": "PasswordFilePath", "value": CONTAINER_PATH}]}
    existing = api("GET", f"/admin/security/auth_configs/{AUTH_ID}", 2, None if open_ else auth,
                   ok=(200, 401, 403, 404))
    print(f"2. login (auth config {AUTH_ID}): {'create' if existing.status_code == 404 else 'keep'}")
    if DRY:
        return _dry_rest(cfg)
    if existing.status_code == 404:
        api("POST", "/admin/security/auth_configs", 2, None if open_ else auth, json=body)
    me = requests.get(f"{GOCD}/api/current_user", auth=auth, timeout=30,
                      headers={"Accept": "application/vnd.go.cd.v1+json"})
    if me.status_code != 200:
        sys.exit(f"Service account login failed ({me.status_code}). "
                 "Run gocd/auth/disable_auth.sh, fix .env/access.yaml, re-run.")
    print(f"   login OK as {sa}")

    # 3. users (Admin > Users)
    known = {u["login_name"]: u for u in
             api("GET", "/users", 3, auth).json()["_embedded"]["users"]}
    for u in users:
        login = u["login"]
        if login not in known:
            api("POST", "/users", 3, auth, json={"login_name": login})
        api("PATCH", f"/users/{login}", 3, auth, json={
            "enabled": bool(u.get("enabled", True)), "email": u.get("email") or "",
            "email_me": False, "checkin_aliases": u.get("checkin_aliases", [])})
        print(f"3. user {login}: {'created' if login not in known else 'updated'}"
              f"{'' if u.get('enabled', True) else ' (disabled)'}")
    for login, u in known.items():
        if login not in passwords and u.get("enabled"):
            api("PATCH", f"/users/{login}", 3, auth, json={"enabled": False})
            print(f"3. user {login}: disabled (not in access.yaml)")

    # 4a. roles
    roles = cfg.get("roles", {}) or {}
    for name, members in roles.items():
        cur = api("GET", f"/admin/security/roles/{name}", 3, auth)
        role = {"name": name, "type": "gocd", "attributes": {"users": list(members)}}
        if cur.status_code == 404:
            api("POST", "/admin/security/roles", 3, auth, json=role)
        else:
            api("PUT", f"/admin/security/roles/{name}", 3, auth, json=role,
                headers={"If-Match": cur.headers["ETag"]})
        print(f"4. role {name}: {', '.join(members) or '(empty)'}")

    # 4b. system admins
    cur = api("GET", "/admin/security/system_admins", 2, auth)
    admins = cfg.get("system_admins", {})
    api("PUT", "/admin/security/system_admins", 2, auth,
        json={"users": admins.get("users", []), "roles": admins.get("roles", [])},
        headers={"If-Match": cur.headers["ETag"]})
    print(f"4. system admins: {', '.join(admins.get('users', []) + admins.get('roles', []))}")

    # 4c. pipeline group permissions (+ approvers, resolved from their emails)
    approvers = approver_logins(cfg)
    print(f"4. approvers (by email): {', '.join(approvers) or '(none)'}")
    for group, spec in (cfg.get("groups") or {}).items():
        cur = api("GET", f"/admin/pipeline_groups/{group}", 1, auth)
        if cur.status_code == 404:
            print(f"4. group {group}: not found yet (run python3 gocd/setup.py, then re-run)")
            continue
        data = cur.json()
        authz = {k: perms((spec or {}).get(k)) for k in ("view", "operate", "admins")}
        if group == APPROVAL_GROUP:   # approvers must be able to see and click the stage
            for level in ("view", "operate"):
                authz[level]["users"] = sorted(set(authz[level]["users"]) | set(approvers))
        api("PUT", f"/admin/pipeline_groups/{group}", 1, auth, headers={"If-Match": cur.headers["ETag"]},
            json={"name": group, "pipelines": [{"name": p["name"]} for p in data.get("pipelines", [])],
                  "authorization": authz})
        print(f"4. group {group}: permissions applied")

    # 4d. GoCD roles no longer in the YAML
    for r in api("GET", "/admin/security/roles?type=gocd", 3, auth).json()["_embedded"]["roles"]:
        if r["name"] not in roles:
            api("DELETE", f"/admin/security/roles/{r['name']}", 3, auth, ok=(200, 404, 422))
            print(f"4. role {r['name']}: removed (not in access.yaml)")

    # 5. service account for the jobs
    env_path = f"/admin/environments/{ENVIRONMENT}"
    if api("GET", env_path, 3, auth).status_code == 200:
        api("PATCH", env_path, 3, auth, ok=(200, 422), json={"environment_variables": {
            "add": [], "remove": ["GOCD_USER", "GOCD_PASSWORD"]}})
        api("PATCH", env_path, 3, auth, json={"environment_variables": {"add": [
            {"name": "GOCD_USER", "value": sa, "secure": False},
            {"name": "GOCD_PASSWORD", "value": passwords[sa], "secure": True}], "remove": []}})
        print(f"5. {ENVIRONMENT}: GOCD_USER={sa}, GOCD_PASSWORD (secure)")

    human = next((u["login"] for u in users if u["login"] != sa), sa)
    print(f"\nDone. Log in at {GOCD}/ as {human} "
          f"(password: {password_key(human, sa)} in .env).")


def _dry_rest(cfg: dict) -> None:
    for u in cfg["users"]:
        print(f"3. user {u['login']} enabled={u.get('enabled', True)} email={u.get('email') or '-'}")
    for name, members in (cfg.get("roles") or {}).items():
        print(f"4. role {name}: {', '.join(members)}")
    print(f"4. system admins: {cfg.get('system_admins')}")
    for g, spec in (cfg.get("groups") or {}).items():
        print(f"4. group {g}: {spec}")
    print(f"   approvers by email -> users: {', '.join(approver_logins(cfg))}")


if __name__ == "__main__":
    main()
