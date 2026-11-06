#!/usr/bin/env python3
"""Wire this repo into GoCD - run once, re-run any time .env changes (idempotent).

1. Creates/updates the GoCD environment `jira-release` holding the settings from .env.
   JIRA_TOKEN / OPENAI_API_KEY are stored as *secure* (encrypted) variables, so no
   secret is ever committed to the public repo.
2. Adds every registered agent to that environment.
3. Creates all pipelines (jira-connect, one jira-space-<KEY> per Jira space,
   jira-approvals) from jira_release/gocd.py via `gocd/run.sh sync`.
   Removes the old YAML config repo if it is still registered.

Usage:  python3 gocd/setup.py            (GOCD_URL defaults to http://localhost:8153/go)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import requests

GOCD = os.environ.get("GOCD_URL", "http://localhost:8153/go").rstrip("/")
AUTH = (os.environ["GOCD_USER"], os.environ["GOCD_PASSWORD"]) if os.environ.get("GOCD_USER") else None
REPO_ID = "jira-karth"  # legacy YAML config repo, removed by migrate_from_config_repo()
ENV_NAME = "jira-release"
SECURE = {"JIRA_TOKEN", "OPENAI_API_KEY"}
SKIP = {"WORKSPACE", "RELEASE"}  # WORKSPACE: per space pipeline; RELEASE: on jira-connect


def api(method: str, path: str, version: int, **kw) -> requests.Response:
    headers = {"Accept": f"application/vnd.go.cd.v{version}+json",
               "Content-Type": "application/json", **kw.pop("headers", {})}
    r = requests.request(method, f"{GOCD}/api{path}", headers=headers, auth=AUTH, timeout=30, **kw)
    if r.status_code >= 400 and r.status_code != 404:
        sys.exit(f"GoCD {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            if v and k.strip() not in SKIP:
                out[k.strip()] = v
    return out


def main() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        sys.exit(f"{env_file} not found - copy .env.example to .env and fill it in")
    values = read_env(env_file)
    # the connect-jira job calls the GoCD API from inside the agent container
    values.setdefault("GOCD_URL", "http://host.docker.internal:8153/go")
    missing = [k for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN") if k not in values]
    if missing:
        sys.exit(f".env is missing: {', '.join(missing)}")

    # 1. environment with variables (secure ones encrypted by GoCD)
    variables = [{"name": k, "value": v, "secure": k in SECURE} for k, v in sorted(values.items())]
    env_path = f"/admin/environments/{ENV_NAME}"
    current = api("GET", env_path, 3)
    if current.status_code == 404:
        api("POST", "/admin/environments", 3, json={
            "name": ENV_NAME, "pipelines": [], "environment_variables": variables})
        print(f"created {env_path}")
    else:
        # PATCH only the variables (keeps the pipelines list): drop ours, add fresh values.
        names = {v["name"] for v in variables}
        stale = [v["name"] for v in current.json().get("environment_variables", [])
                 if v["name"] in names]
        if stale:
            api("PATCH", env_path, 3, json={"environment_variables": {"add": [], "remove": stale}})
        api("PATCH", env_path, 3, json={"environment_variables": {"add": variables, "remove": []}})
        print(f"updated {env_path}")
    print("  variables: " + ", ".join(f"{v['name']}{' (secure)' if v['secure'] else ''}"
                                      for v in variables))

    # 2. agents -> environment
    agents = api("GET", "/agents", 7).json()["_embedded"]["agents"]
    if agents:
        api("PATCH", "/agents", 7, json={"uuids": [a["uuid"] for a in agents],
                                         "agent_config_state": "Enabled",
                                         "operations": {"environments": {"add": [ENV_NAME]}}})
        print(f"added {len(agents)} agent(s) to {ENV_NAME}")
    else:
        print("! no GoCD agents registered - run gocd/agent/start.sh, then re-run this script")

    # 3. pipelines (all defined in jira_release/gocd.py)
    migrate_from_config_repo()
    root = Path(__file__).resolve().parent.parent
    # RELEASE from .env (e.g. release-26.14) becomes the jira-connect pipeline variable
    release = read_release(env_file)
    subprocess.run([str(root / "gocd" / "run.sh"), "sync"], check=True,
                   env={**os.environ, "GOCD_URL": GOCD,
                        **({"SET_RELEASE": release} if release else {})})


def read_release(env_file: Path) -> str:
    for raw in env_file.read_text().splitlines():
        if raw.strip().startswith("RELEASE="):
            return raw.split("=", 1)[1].split(" #", 1)[0].strip().strip('"').strip("'")
    return ""


def migrate_from_config_repo() -> None:
    """Earlier versions defined jira-connect in a YAML config repo. GoCD forbids API pipelines
    depending on config-repo pipelines, so drop the generated pipelines, then the config repo;
    `sync` recreates everything through the API (run history is kept by pipeline name)."""
    if api("GET", f"/admin/config_repos/{REPO_ID}", 4).status_code == 404:
        return
    groups = api("GET", "/admin/pipeline_groups", 1).json()["_embedded"]["groups"]
    generated = [p["name"] for g in groups for p in g.get("pipelines", [])
                 if p["name"] in ("jira-release-approval", "jira-approvals")
                 or p["name"].startswith("jira-space-")]
    generated.sort(key=lambda n: n.startswith("jira-space-"))   # downstream (approval) first
    env = api("GET", f"/admin/environments/{ENV_NAME}", 3)
    if env.status_code == 200:
        in_env = {p["name"] for p in env.json().get("pipelines", [])}
        drop = [n for n in generated if n in in_env]
        if drop:
            api("PATCH", f"/admin/environments/{ENV_NAME}", 3,
                json={"pipelines": {"add": [], "remove": drop}})
    for name in generated:
        api("DELETE", f"/admin/pipelines/{name}", 11)
    api("DELETE", f"/admin/config_repos/{REPO_ID}", 4)
    print(f"migrated: removed config repo {REPO_ID} and {len(generated)} generated pipeline(s)")


if __name__ == "__main__":
    main()
