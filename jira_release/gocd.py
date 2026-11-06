"""Create/update the fan-out pipeline in GoCD: one parallel job per Jira space, then Approval.

    jira-connect (config repo)                 jira-release-spaces (written by this module)
    ┌──────────────┐        ┌───────────────────────────────┐   ┌─────────────────────────┐
    │ connect-jira │ ─────▶ │ stage spaces: space-DEV        │──▶│ stage approval (manual) │
    └──────────────┘        │               space-SCRUM  ... │   │   publish-confluence    │
                            └───────────────────────────────┘   └─────────────────────────┘

GoCD jobs are static per pipeline run, so the Jira job rewrites the *downstream* pipeline
before it finishes; the downstream run it triggers then has exactly one job per space.
"""
from __future__ import annotations

import os

import requests

UPSTREAM = "jira-connect"
UPSTREAM_STAGE = "connect-jira"
PIPELINE = "jira-release-spaces"
GROUP = "jira"
ENVIRONMENT = "jira-release"
REPO_URL = "https://github.com/karthikeyachinna/jira-karth.git"


def job_name(space: str) -> str:
    return f"space-{space}"


def pipeline_config(spaces: list[str]) -> dict:
    exec_task = lambda *args: {"type": "exec", "attributes": {
        "command": "gocd/run.sh", "arguments": list(args), "run_if": ["passed"]}}

    space_jobs = [{
        "name": job_name(s),
        "timeout": 30,
        "environment_variables": [{"name": "WORKSPACE", "value": s, "secure": False}],
        "resources": [], "tabs": [{"name": "release-notes", "path": f"{s}/release_notes.html"}],
        "artifacts": [{"type": "build", "source": f"output/{s}", "destination": ""}],
        "tasks": [exec_task("generate")],
    } for s in spaces]

    fetch_tasks = [{"type": "fetch", "attributes": {
        "artifact_origin": "gocd", "pipeline": "", "stage": "spaces", "job": job_name(s),
        "is_source_a_file": False, "source": s, "destination": "output", "run_if": ["passed"],
    }} for s in spaces]

    return {
        "group": GROUP,
        "pipeline": {
            "name": PIPELINE,
            "label_template": "${COUNT}",
            "lock_behavior": "unlockWhenFinished",
            "materials": [
                {"type": "dependency", "attributes": {
                    "name": UPSTREAM, "pipeline": UPSTREAM, "stage": UPSTREAM_STAGE,
                    "auto_update": True, "ignore_for_scheduling": False}},
                # code only - commits must not trigger this pipeline (jira-connect does)
                {"type": "git", "attributes": {
                    "name": "repo", "url": REPO_URL, "branch": "main", "auto_update": True,
                    "shallow_clone": True, "filter": {"ignore": ["**/*", "*"]},
                    "invert_filter": False}},
            ],
            "stages": [
                {"name": "spaces", "fetch_materials": True, "clean_working_directory": True,
                 "never_cleanup_artifacts": False,
                 "approval": {"type": "success", "allow_only_on_success": True,
                              "authorization": {"roles": [], "users": []}},
                 "environment_variables": [], "jobs": space_jobs},
                {"name": "approval", "fetch_materials": True, "clean_working_directory": True,
                 "never_cleanup_artifacts": False,
                 "approval": {"type": "manual", "allow_only_on_success": True,
                              "authorization": {"roles": [], "users": []}},
                 "environment_variables": [],
                 "jobs": [{"name": "publish-confluence", "timeout": 30, "resources": [],
                           "environment_variables": [], "tabs": [], "artifacts": [],
                           "tasks": fetch_tasks + [exec_task("publish")]}]},
            ],
        },
    }


class GoCD:
    def __init__(self):
        self.base = os.environ.get("GOCD_URL", "http://localhost:8153/go").rstrip("/")
        user, pw = os.environ.get("GOCD_USER"), os.environ.get("GOCD_PASSWORD")
        self.auth = (user, pw) if user else None

    def call(self, method: str, path: str, version: int, **kw) -> requests.Response:
        headers = {"Accept": f"application/vnd.go.cd.v{version}+json",
                   "Content-Type": "application/json", **kw.pop("headers", {})}
        r = requests.request(method, f"{self.base}/api{path}", headers=headers,
                             auth=self.auth, timeout=30, **kw)
        if r.status_code >= 400 and r.status_code != 404:
            raise SystemExit(f"GoCD {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r

    def sync_spaces_pipeline(self, spaces: list[str]) -> None:
        body = pipeline_config(spaces)
        existing = self.call("GET", f"/admin/pipelines/{PIPELINE}", 11)
        if existing.status_code == 404:
            self.call("POST", "/admin/pipelines", 11, json=body)
            self.call("POST", f"/pipelines/{PIPELINE}/unpause", 1,
                      headers={"X-GoCD-Confirm": "true"})
            print(f"[gocd] created pipeline {PIPELINE} with {len(spaces)} parallel jobs")
        else:
            self.call("PUT", f"/admin/pipelines/{PIPELINE}", 11, json=body["pipeline"],
                      headers={"If-Match": existing.headers["ETag"]})
            print(f"[gocd] updated pipeline {PIPELINE}: jobs = "
                  + ", ".join(job_name(s) for s in spaces))

        env = self.call("GET", f"/admin/environments/{ENVIRONMENT}", 3)
        if env.status_code == 200 and PIPELINE not in {p["name"] for p in env.json().get("pipelines", [])}:
            self.call("PATCH", f"/admin/environments/{ENVIRONMENT}", 3,
                      json={"pipelines": {"add": [PIPELINE], "remove": []}})
            print(f"[gocd] added {PIPELINE} to environment {ENVIRONMENT}")
