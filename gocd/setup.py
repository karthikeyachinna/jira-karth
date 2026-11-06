#!/usr/bin/env python3
"""Wire this repo into GoCD - run once, re-run any time .env changes (idempotent).

1. Registers the GitHub repo as a GoCD config repo (yaml.config.plugin) so
   .gocd/*.gocd.yaml defines the `jira-release-notes` pipeline.
2. Creates/updates the GoCD environment `jira-release` holding the settings from .env.
   JIRA_TOKEN / OPENAI_API_KEY are stored as *secure* (encrypted) variables, so no
   secret is ever committed to the public repo.
3. Adds the pipeline and every registered agent to that environment.

Usage:  python3 gocd/setup.py            (GOCD_URL defaults to http://localhost:8153/go)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

GOCD = os.environ.get("GOCD_URL", "http://localhost:8153/go").rstrip("/")
AUTH = (os.environ["GOCD_USER"], os.environ["GOCD_PASSWORD"]) if os.environ.get("GOCD_USER") else None
REPO_ID = "jira-karth"
REPO_URL = "https://github.com/karthikeyachinna/jira-karth.git"
ENV_NAME = "jira-release"
PIPELINE = "jira-release-notes"
SECURE = {"JIRA_TOKEN", "OPENAI_API_KEY"}
SKIP = {"WORKSPACE", "RELEASE"}  # chosen per run via "Trigger with options"


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


def upsert(path: str, version: int, body: dict) -> None:
    existing = api("GET", path, version)
    if existing.status_code == 404:
        collection = path.rsplit("/", 1)[0]
        api("POST", collection, version, json=body)
        print(f"created {path}")
    else:
        api("PUT", path, version, json=body, headers={"If-Match": existing.headers["ETag"]})
        print(f"updated {path}")


def main() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        sys.exit(f"{env_file} not found - copy .env.example to .env and fill it in")
    values = read_env(env_file)
    missing = [k for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_TOKEN") if k not in values]
    if missing:
        sys.exit(f".env is missing: {', '.join(missing)}")

    # 1. config repo
    upsert(f"/admin/config_repos/{REPO_ID}", 4, {
        "id": REPO_ID, "plugin_id": "yaml.config.plugin",
        "material": {"type": "git", "attributes": {"url": REPO_URL, "branch": "main",
                                                   "auto_update": True}},
        "configuration": [{"key": "file_pattern", "value": ".gocd/*.gocd.yaml"}],
    })

    # wait for the YAML to be parsed so the pipeline exists
    for _ in range(30):
        if api("GET", f"/admin/pipelines/{PIPELINE}", 11).status_code == 200:
            break
        time.sleep(2)
    else:
        print(f"! pipeline {PIPELINE} not visible yet - push .gocd/ to GitHub, then re-run")

    pipeline_exists = api("GET", f"/admin/pipelines/{PIPELINE}", 11).status_code == 200

    # 2. environment with variables (secure ones encrypted by GoCD)
    variables = [{"name": k, "value": v, "secure": k in SECURE} for k, v in sorted(values.items())]
    upsert(f"/admin/environments/{ENV_NAME}", 3, {
        "name": ENV_NAME,
        "pipelines": [{"name": PIPELINE}] if pipeline_exists else [],
        "environment_variables": variables,
    })
    print("  variables: " + ", ".join(f"{v['name']}{' (secure)' if v['secure'] else ''}"
                                      for v in variables))

    # 3. agents -> environment
    agents = api("GET", "/agents", 7).json()["_embedded"]["agents"]
    if agents:
        api("PATCH", "/agents", 7, json={"uuids": [a["uuid"] for a in agents],
                                         "agent_config_state": "Enabled",
                                         "operations": {"environments": {"add": [ENV_NAME]}}})
        print(f"added {len(agents)} agent(s) to {ENV_NAME}")
    else:
        print("! no GoCD agents registered - start one, then re-run this script")


if __name__ == "__main__":
    main()
