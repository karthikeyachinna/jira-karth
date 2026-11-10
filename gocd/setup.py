#!/usr/bin/env python3
"""Configure a GoCD server from gocd/gocd.yaml (+ secrets in .env). Idempotent.

    gocd/bootstrap.sh                               # recommended: venv + this script
    .venv/bin/python gocd/setup.py                  # same, venv already set up
    GOCD_CONFIG=gocd/prod.yaml gocd/bootstrap.sh    # another GoCD domain

Steps:
  1. login       - if auth.enabled: apply auth.access_file (users, roles, approvers) via
                   gocd/auth/setup_auth.py
  2. environment - create/update gocd.environment with the job settings from gocd.yaml and
                   the secrets from .env (JIRA_TOKEN, OPENAI_API_KEY, GOCD_PASSWORD: secure)
  3. agents      - if agents.docker: start agents.count local Docker agents; enable every agent
                   and add it to the environment
  4. pipelines   - create jira-connect, jira-space-<KEY>, jira-release-notes, jira-approvals
                   (jira_release/gocd.py, via `gocd/run.sh sync`)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jira_release import deploy  # noqa: E402

REPO_ID = "jira-karth"  # legacy YAML config repo, removed by migrate_from_config_repo()
SECRETS = {"JIRA_TOKEN", "OPENAI_API_KEY"}
GOCD = deploy.gocd_url() if not os.environ.get("GOCD_URL") else os.environ["GOCD_URL"].rstrip("/")
AUTH = None


def api(method: str, path: str, version: int, ok=(404,), **kw) -> requests.Response:
    headers = {"Accept": f"application/vnd.go.cd.v{version}+json",
               "Content-Type": "application/json", **kw.pop("headers", {})}
    r = requests.request(method, f"{GOCD}/api{path}", headers=headers, auth=AUTH, timeout=30, **kw)
    if r.status_code >= 400 and r.status_code not in ok:
        sys.exit(f"GoCD {method} {path} -> {r.status_code}: {r.text[:400]}")
    return r


def read_dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    out = {}
    if path.is_file():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def step(n: int, text: str) -> None:
    print(f"\n[{n}/4] {text}")


def main() -> None:
    global AUTH
    if not deploy.cfg():
        sys.exit(f"{deploy.config_path()} not found")
    env_name = deploy.environment()
    print(f"GoCD {GOCD}  (config: {deploy.config_path().relative_to(ROOT)})")
    dotenv = read_dotenv()
    missing = [k for k in ("JIRA_TOKEN",) if not dotenv.get(k)]
    if missing:
        sys.exit(f".env is missing secrets: {', '.join(missing)}")
    moved = sorted(k for k in deploy.job_env() if k in dotenv and k != "GOCD_URL")
    if moved:
        print(f"  note: {', '.join(moved)} in .env are ignored - gocd.yaml is used for them")

    # 1. login -------------------------------------------------------------------------
    step(1, "login")
    if deploy.get("auth.enabled", False):
        subprocess.run([sys.executable, "-u", str(ROOT / "gocd" / "auth" / "setup_auth.py")],
                       check=True, env={**os.environ, "GOCD_URL": GOCD})
        dotenv = read_dotenv()                    # setup_auth may have generated passwords
    else:
        print("  auth.enabled is false - GoCD stays open (no login)")
    if dotenv.get("GOCD_USER") and dotenv.get("GOCD_PASSWORD"):
        AUTH = (dotenv["GOCD_USER"], dotenv["GOCD_PASSWORD"])

    # 2. environment -------------------------------------------------------------------
    step(2, f"environment {env_name}")
    values = {**deploy.job_env(), **{k: dotenv[k] for k in SECRETS if dotenv.get(k)}}
    if AUTH:
        values.update({"GOCD_USER": AUTH[0], "GOCD_PASSWORD": AUTH[1]})
    variables = [{"name": k, "value": v, "secure": k in SECRETS or k == "GOCD_PASSWORD"}
                 for k, v in sorted(values.items())]
    env_path = f"/admin/environments/{env_name}"
    current = api("GET", env_path, 3)
    if current.status_code == 404:
        api("POST", "/admin/environments", 3, json={
            "name": env_name, "pipelines": [], "environment_variables": variables})
        print(f"  created {env_name}")
    else:   # PATCH only our variables (keeps pipelines and any extra variables)
        # also drop the old JIRA_WORKSPACES list and switches for spaces removed from the yaml
        stale = [v["name"] for v in current.json().get("environment_variables", [])
                 if v["name"] in values or v["name"] in ("JIRA_WORKSPACES", "RELEASE_REGION")
                 or v["name"].startswith((deploy.SPACE_PREFIX, deploy.REGION_PREFIX))]
        if stale:
            api("PATCH", env_path, 3, json={"environment_variables": {"add": [], "remove": stale}})
        api("PATCH", env_path, 3, json={"environment_variables": {"add": variables, "remove": []}})
        print(f"  updated {env_name}")
    print("  " + ", ".join(f"{v['name']}{' (secure)' if v['secure'] else '=' + v['value']}"
                           for v in variables))

    # 3. agents ------------------------------------------------------------------------
    step(3, "agents")
    if deploy.get("agents.docker", False):
        count = str(deploy.get("agents.count", 3))
        subprocess.run([str(ROOT / "gocd" / "agent" / "start.sh"), count], check=True, env={
            **os.environ, "GOCD_URL": GOCD, "AGENT_SERVER_URL": deploy.get("gocd.agent_url", GOCD),
            "AGENT_ENVIRONMENT": env_name})
        for _ in range(30):                        # wait for them to register
            if len(api("GET", "/agents", 7).json()["_embedded"]["agents"]) >= int(count):
                break
            time.sleep(3)
    agents = api("GET", "/agents", 7).json()["_embedded"]["agents"]
    live = [a for a in agents if a.get("agent_state") != "LostContact"]
    if live:
        api("PATCH", "/agents", 7, json={"uuids": [a["uuid"] for a in live],
                                         "agent_config_state": "Enabled",
                                         "operations": {"environments": {"add": [env_name]}}})
        print(f"  {len(live)} agent(s) enabled in {env_name}")
    else:
        print("  ! no agents - set agents.docker: true, or register agents on this server")

    # 4. pipelines ---------------------------------------------------------------------
    step(4, "pipelines")
    migrate_from_config_repo(env_name)
    subprocess.run([str(ROOT / "gocd" / "run.sh"), "sync"], check=True,
                   env={**os.environ, "GOCD_URL": GOCD})
    print(f"\nDone. Open {GOCD}/pipelines and run jira-connect.")


def migrate_from_config_repo(env_name: str) -> None:
    """Earlier versions defined jira-connect in a YAML config repo. GoCD forbids API pipelines
    depending on config-repo pipelines, so drop the generated pipelines, then the config repo;
    `sync` recreates everything through the API (run history is kept by pipeline name)."""
    if api("GET", f"/admin/config_repos/{REPO_ID}", 4).status_code == 404:
        return
    groups = api("GET", "/admin/pipeline_groups", 1).json()["_embedded"]["groups"]
    generated = [p["name"] for g in groups for p in g.get("pipelines", [])
                 if p["name"] in ("jira-release-approval", "jira-approvals", "jira-release-notes")
                 or p["name"].startswith("jira-space-")]
    generated.sort(key=lambda n: n.startswith("jira-space-"))   # downstream first
    env = api("GET", f"/admin/environments/{env_name}", 3)
    if env.status_code == 200:
        drop = [n for n in generated if n in {p["name"] for p in env.json().get("pipelines", [])}]
        if drop:
            api("PATCH", f"/admin/environments/{env_name}", 3,
                json={"pipelines": {"add": [], "remove": drop}})
    for name in generated:
        api("DELETE", f"/admin/pipelines/{name}", 11)
    api("DELETE", f"/admin/config_repos/{REPO_ID}", 4)
    print(f"  migrated: removed config repo {REPO_ID} and {len(generated)} generated pipeline(s)")


if __name__ == "__main__":
    main()
