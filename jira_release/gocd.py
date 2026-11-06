"""Create/update the fan-out pipelines in GoCD: one pipeline per Jira space, then one Approval.

                      ┌─▶ jira-space-DEV   ─┐
                      ├─▶ jira-space-QS    ─┤
    jira-connect ─────┼─▶ jira-space-SCR   ─┼──▶ jira-approvals (manual)
    (config repo)     ├─▶ jira-space-SCRUM ─┤
                      └─▶ jira-space-SUP   ─┘

Every pipeline is defined here and written through the GoCD API (not a YAML config repo):
GoCD does not allow API-defined pipelines to depend on config-repo pipelines, and the number of
space pipelines depends on Jira. gocd/setup.py creates them once; after that each jira-connect
run re-syncs them, then GoCD triggers each space pipeline, and the approval pipeline waits
(fan-in) for all of them and a manual click.
"""
from __future__ import annotations

import os

import requests

UPSTREAM = "jira-connect"
UPSTREAM_STAGE = "connect-jira"
SPACE_PREFIX = "jira-space-"
SPACE_STAGE = "release-notes"
SPACE_JOB = "generate"
APPROVAL = "jira-approvals"
LEGACY = ["jira-release-spaces", "jira-release-approval"]   # earlier names, removed on sync
# GoCD pipeline groups
CONNECT_GROUP = "jira"
SPACES_GROUP = "jira-spaces"
APPROVAL_GROUP = "jira-approval"
GROUPS = (CONNECT_GROUP, SPACES_GROUP, APPROVAL_GROUP)
ENVIRONMENT = "jira-release"
REPO_URL = "https://github.com/karthikeyachinna/jira-karth.git"

NO_AUTH = {"roles": [], "users": []}


def space_pipeline(space: str) -> str:
    return f"{SPACE_PREFIX}{space}"


def _git_material() -> dict:
    # code only - commits must not trigger these pipelines (jira-connect does)
    return {"type": "git", "attributes": {
        "name": "repo", "url": REPO_URL, "branch": "main", "auto_update": True,
        "shallow_clone": True, "filter": {"ignore": ["**/*", "*"]}, "invert_filter": False}}


def _dependency(pipeline: str, stage: str) -> dict:
    return {"type": "dependency", "attributes": {
        "name": pipeline, "pipeline": pipeline, "stage": stage,
        "auto_update": True, "ignore_for_scheduling": False}}


def _exec(*args: str) -> dict:
    return {"type": "exec", "attributes": {
        "command": "gocd/run.sh", "arguments": list(args), "run_if": ["passed"]}}


def _stage(name: str, approval: str, jobs: list[dict]) -> dict:
    return {"name": name, "fetch_materials": True, "clean_working_directory": True,
            "never_cleanup_artifacts": False, "environment_variables": [],
            "approval": {"type": approval, "allow_only_on_success": True,
                         "authorization": NO_AUTH},
            "jobs": jobs}


def _job(name: str, tasks: list[dict], **extra) -> dict:
    return {"name": name, "timeout": 30, "resources": [], "environment_variables": [],
            "tabs": [], "artifacts": [], "tasks": tasks, **extra}


def _pipeline(group: str, name: str, materials: list[dict], stages: list[dict],
              label: str = "${COUNT}", env: list[dict] | None = None) -> dict:
    return {"group": group, "pipeline": {
        "name": name, "label_template": label, "lock_behavior": "unlockWhenFinished",
        "environment_variables": env or [], "materials": materials, "stages": stages}}


def _var(name: str, value: str) -> dict:
    return {"name": name, "value": value, "secure": False}


def connect_config() -> dict:
    job = _job(UPSTREAM_STAGE, [_exec("check"), _exec("sync")],
               tabs=[{"name": "spaces", "path": "spaces/workspaces.html"}],
               artifacts=[{"type": "build", "source": "output/workspaces.*",
                           "destination": "spaces"}])
    repo = _git_material()
    repo["attributes"]["filter"] = None          # code changes DO trigger jira-connect
    repo["attributes"]["shallow_clone"] = False
    return _pipeline(CONNECT_GROUP, UPSTREAM, [repo], [_stage(UPSTREAM_STAGE, "success", [job])])


def space_config(space: dict) -> dict:
    """space = one entry of the discover catalog: workspace, current_release, released, ..."""
    key = space["workspace"]
    current = space.get("current_release") or ""
    job = _job(SPACE_JOB, [_exec("generate")],
               tabs=[{"name": "release-notes", "path": f"{key}/release_notes.html"}],
               artifacts=[{"type": "build", "source": f"output/{key}", "destination": ""}])
    env = [
        _var("WORKSPACE", key),
        _var("RELEASE", current),                                   # what this run builds
        _var("RELEASE_LABEL", space.get("label") or current or "no-release"),  # run label
        _var("EXISTING_RELEASES", ",".join(space.get("released", [])) or "none"),
        _var("UPCOMING_RELEASES", ",".join(space.get("upcoming", [])) or "none"),
    ]
    # run label in the GoCD UI, e.g. "release-26.1-7"
    return _pipeline(SPACES_GROUP, space_pipeline(key),
                     [_dependency(UPSTREAM, UPSTREAM_STAGE), _git_material()],
                     [_stage(SPACE_STAGE, "success", [job])],
                     label="${env:RELEASE_LABEL}-${COUNT}", env=env)


def approval_config(spaces: list[str], release_label: str = "") -> dict:
    fetches = [{"type": "fetch", "attributes": {
        "artifact_origin": "gocd", "pipeline": space_pipeline(s), "stage": SPACE_STAGE,
        "job": SPACE_JOB, "is_source_a_file": False, "source": s, "destination": "output",
        "run_if": ["passed"]}} for s in spaces]
    return _pipeline(APPROVAL_GROUP, APPROVAL,
                     [_dependency(space_pipeline(s), SPACE_STAGE) for s in spaces]
                     + [_git_material()],
                     # manual first stage: runs only when someone clicks Approve
                     [_stage("approval", "manual",
                             [_job("publish-confluence", fetches + [_exec("publish")])])],
                     label="${env:RELEASE_LABEL}-${COUNT}",
                     env=[_var("RELEASE_LABEL", release_label or "release")])


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
        if r.status_code >= 400 and r.status_code not in (404, 409):  # 409 = already unpaused
            raise SystemExit(f"GoCD {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r

    # ---- pipelines --------------------------------------------------------
    def upsert(self, body: dict) -> None:
        name = body["pipeline"]["name"]
        existing = self.call("GET", f"/admin/pipelines/{name}", 11)
        if existing.status_code == 404:
            self.call("POST", "/admin/pipelines", 11, json=body)
            self.call("POST", f"/pipelines/{name}/unpause", 1, headers={"X-GoCD-Confirm": "true"})
            print(f"[gocd] created  {name}")
        else:
            self.call("PUT", f"/admin/pipelines/{name}", 11,
                      json={**body["pipeline"], "group": body["group"]},
                      headers={"If-Match": existing.headers["ETag"]})
            print(f"[gocd] updated  {name}")

    def delete(self, name: str) -> None:
        if self.call("GET", f"/admin/pipelines/{name}", 11).status_code == 404:
            return
        self.env_pipelines(remove=[name])
        self.call("DELETE", f"/admin/pipelines/{name}", 11)
        print(f"[gocd] deleted  {name}")

    def pipeline_names(self) -> list[str]:
        groups = self.call("GET", "/admin/pipeline_groups", 1).json()
        return [p["name"] for g in groups["_embedded"]["groups"] if g["name"] in GROUPS
                for p in g.get("pipelines", [])]

    # ---- environment ------------------------------------------------------
    def env_pipelines(self, add: list[str] = (), remove: list[str] = ()) -> None:
        env = self.call("GET", f"/admin/environments/{ENVIRONMENT}", 3)
        if env.status_code != 200:
            return
        current = {p["name"] for p in env.json().get("pipelines", [])}
        add = [p for p in add if p not in current]
        remove = [p for p in remove if p in current]
        if add or remove:
            self.call("PATCH", f"/admin/environments/{ENVIRONMENT}", 3,
                      json={"pipelines": {"add": add, "remove": remove}})

    # ---- main entry -------------------------------------------------------
    def sync(self, catalog: list[dict]) -> None:
        spaces = [c["workspace"] for c in catalog]
        self.upsert(connect_config())
        for c in catalog:
            self.upsert(space_config(c))
        self.upsert(approval_config(spaces, catalog[0].get("target_release", "") if catalog else ""))     # rewire fan-in before deleting old spaces
        self.env_pipelines(add=[UPSTREAM] + [space_pipeline(s) for s in spaces] + [APPROVAL])

        wanted = {space_pipeline(s) for s in spaces}
        stale = [p for p in self.pipeline_names()
                 if (p.startswith(SPACE_PREFIX) and p not in wanted) or p in LEGACY]
        for name in stale:
            self.delete(name)
        print(f"[gocd] {len(spaces)} space pipelines -> {APPROVAL}")
