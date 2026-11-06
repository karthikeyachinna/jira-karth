#!/usr/bin/env python3
"""Turn on GoCD login (password-file plugin) with users, roles and a release-approval gate.

    .venv/bin/python gocd/auth/setup_auth.py        (idempotent - re-run after editing .env)

Accounts, all read from .env (missing passwords are generated and written back to .env,
never printed):

    GOCD_ADMIN_USER / GOCD_ADMIN_PASSWORD   human admin (GoCD system admin)
    GOCD_USER / GOCD_PASSWORD               service account used by the pipelines + scripts
    GOCD_APPROVERS=karthikeya,alice         role "release-approvers": who may approve releases
    GOCD_USERS=alice,bob                    extra users (passwords generated as
                                            GOCD_PASSWORD_<NAME> in .env)

Flow:
  1. bcrypt-hash every password into passwords.properties, copy it into the GoCD server
     container (/godata/config/passwords.properties)
  2. create the "file-auth" authorization config  -> GoCD now requires login
  3. verify the admin can log in, then set system admins (admin + service account)
  4. create role "release-approvers"; jira-approvals' approval stage only allows that role
  5. store GOCD_USER / GOCD_PASSWORD (secure) on the jira-release environment for the jobs

Locked out? gocd/auth/disable_auth.sh removes the security block from the server config.
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

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
GOCD = os.environ.get("GOCD_URL", "http://localhost:8153/go").rstrip("/")
CONTAINER_PATH = "/godata/config/passwords.properties"
AUTH_ID = "file-auth"
PLUGIN = "cd.go.authentication.passwordfile"
APPROVER_ROLE = "release-approvers"
ENVIRONMENT = "jira-release"


# ---------------------------------------------------------------- .env helpers
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
        text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_FILE.write_text(text)


def ensure(env: dict, key: str, default: str | None = None, secret: bool = False) -> str:
    if env.get(key):
        return env[key]
    value = secrets.token_urlsafe(18) if secret else default
    set_env(key, value)
    env[key] = value
    print(f"  .env: {key} {'generated' if secret else '= ' + value}")
    return value


def csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# ---------------------------------------------------------------- GoCD API
def api(method: str, path: str, version: int, auth=None, ok=(200, 201, 404), **kw):
    headers = {"Accept": f"application/vnd.go.cd.v{version}+json",
               "Content-Type": "application/json", **kw.pop("headers", {})}
    r = requests.request(method, f"{GOCD}/api{path}", headers=headers, auth=auth,
                         timeout=30, **kw)
    if r.status_code not in ok:
        sys.exit(f"GoCD {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r


def upsert(path: str, collection: str, version: int, body: dict, auth) -> str:
    current = api("GET", path, version, auth)
    if current.status_code == 404:
        api("POST", collection, version, auth, json=body)
        return "created"
    api("PUT", path, version, auth, json=body, headers={"If-Match": current.headers["ETag"]})
    return "updated"


def server_container() -> str:
    name = subprocess.run(
        ["docker", "ps", "--filter", "ancestor=gocd/gocd-server:v26.1.0", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True).stdout.split()
    if not name:
        sys.exit("GoCD server container not found (set GOCD_SERVER_CONTAINER)")
    return os.environ.get("GOCD_SERVER_CONTAINER", name[0])


def hash_pw(password: str) -> str:
    # $2a$ is the same bcrypt algorithm; it is the prefix every Java bcrypt library accepts
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode().replace("$2b$", "$2a$", 1)


# ---------------------------------------------------------------- main
def main() -> None:
    if not ENV_FILE.is_file():
        sys.exit(f"{ENV_FILE} not found")
    env = read_env()
    print("Accounts")
    admin = ensure(env, "GOCD_ADMIN_USER", "karthikeya")
    admin_pw = ensure(env, "GOCD_ADMIN_PASSWORD", secret=True)
    bot = ensure(env, "GOCD_USER", "gocd-bot")
    bot_pw = ensure(env, "GOCD_PASSWORD", secret=True)
    approvers = csv(ensure(env, "GOCD_APPROVERS", admin))
    users = {admin: admin_pw, bot: bot_pw}
    for name in csv(env.get("GOCD_USERS", "")) + approvers:
        if name not in users:
            users[name] = ensure(env, f"GOCD_PASSWORD_{re.sub(r'[^A-Za-z0-9]', '_', name).upper()}",
                                 secret=True)

    # 1. password file -> server container
    container = server_container()
    with tempfile.NamedTemporaryFile("w", suffix=".properties", delete=False) as fh:
        fh.write("# managed by gocd/auth/setup_auth.py - bcrypt hashes, edit .env and re-run\n")
        for name, pw in sorted(users.items()):
            fh.write(f"{name}={hash_pw(pw)}\n")
        local = fh.name
    os.chmod(local, 0o644)
    subprocess.run(["docker", "cp", local, f"{container}:{CONTAINER_PATH}"], check=True)
    os.unlink(local)
    print(f"\n1. password file with {len(users)} user(s) -> {container}:{CONTAINER_PATH}")

    # 2. authorization config (first run: GoCD is still open, so no credentials are needed)
    admin_auth = (admin, admin_pw)
    probe = api("GET", "/admin/security/auth_configs", 2, ok=(200, 401, 403))
    auth = None if probe.status_code == 200 else admin_auth
    body = {"id": AUTH_ID, "plugin_id": PLUGIN, "allow_only_known_users_to_login": False,
            "properties": [{"key": "PasswordFilePath", "value": CONTAINER_PATH}]}
    print(f"2. auth config {AUTH_ID}: "
          + upsert(f"/admin/security/auth_configs/{AUTH_ID}", "/admin/security/auth_configs",
                   2, body, auth) + "  (login is now required)")

    # 3. admin can log in?  then system admins
    me = requests.get(f"{GOCD}/api/current_user", auth=admin_auth, timeout=30,
                      headers={"Accept": "application/vnd.go.cd.v1+json"})
    if me.status_code != 200:
        sys.exit(f"Admin login check failed ({me.status_code}). Run gocd/auth/disable_auth.sh "
                 "to turn login off, then re-run this script.")
    print(f"3. admin login OK as {me.json().get('login_name')}")
    current = api("GET", "/admin/security/system_admins", 2, admin_auth)
    admins = sorted({admin, bot} | set(current.json().get("users", [])))
    api("PUT", "/admin/security/system_admins", 2, admin_auth,
        json={"roles": current.json().get("roles", []), "users": admins},
        headers={"If-Match": current.headers["ETag"]})
    print(f"   system admins: {', '.join(admins)}")

    # 4. approvers role
    print(f"4. role {APPROVER_ROLE} ({', '.join(approvers)}): "
          + upsert(f"/admin/security/roles/{APPROVER_ROLE}", "/admin/security/roles", 3,
                   {"name": APPROVER_ROLE, "type": "gocd",
                    "attributes": {"users": approvers}}, admin_auth))

    # 5. service account for the jobs (password stored encrypted)
    env_path = f"/admin/environments/{ENVIRONMENT}"
    if api("GET", env_path, 3, admin_auth).status_code == 200:
        api("PATCH", env_path, 3, admin_auth, json={"environment_variables": {
            "add": [], "remove": ["GOCD_USER", "GOCD_PASSWORD"]}}, ok=(200, 422))
        api("PATCH", env_path, 3, admin_auth, json={"environment_variables": {"add": [
            {"name": "GOCD_USER", "value": bot, "secure": False},
            {"name": "GOCD_PASSWORD", "value": bot_pw, "secure": True}], "remove": []}})
        print(f"5. {ENVIRONMENT}: GOCD_USER={bot}, GOCD_PASSWORD (secure)")

    print(f"\nDone. Log in at {GOCD}/ as {admin} (password: GOCD_ADMIN_PASSWORD in .env).")
    print("Next: python3 gocd/setup.py   (re-syncs pipelines with the approval gate)")


if __name__ == "__main__":
    main()
