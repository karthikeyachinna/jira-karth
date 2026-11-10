"""Create/update the fan-out pipelines in GoCD: one pipeline per Jira space, one release-notes
pipeline per region switched on (RELEASE_REGION_<US|INT>=YES), then one Approval.

                      ┌─▶ jira-space-DEV   ─┐
                      ├─▶ jira-space-QS    ─┤   ┌─▶ jira-release-notes-US  ─┐
    jira-connect ─────┼─▶ jira-space-SCR   ─┼───┤                          ├──▶ jira-approvals
                      ├─▶ jira-space-SCRUM ─┤   └─▶ jira-release-notes-INT ─┘     (manual)
                      └─▶ jira-space-SUP   ─┘     (one AI document each)

Every pipeline is defined here and written through the GoCD API (not a YAML config repo):
GoCD does not allow API-defined pipelines to depend on config-repo pipelines, and the number of
space pipelines depends on Jira. gocd/setup.py creates them once; after that each jira-connect
run re-syncs them, then GoCD triggers each space pipeline, and the approval pipeline waits
(fan-in) for all of them and a manual click.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from . import deploy
from .config import region_spaces, release_regions

UPSTREAM = "jira-connect"
UPSTREAM_STAGE = "connect-jira"
SPACE_PREFIX = "jira-space-"
SPACE_STAGE = "release-notes"
SPACE_JOB = "generate"
APPROVAL = "jira-approvals"
NOTES_PREFIX = "jira-release-notes-"  # + us / int: one combined document per region
NOTES_STAGE = "release-notes"
NOTES_JOB = "combine"
LEGACY = ["jira-release-spaces", "jira-release-approval", "jira-release-notes"]   # earlier names, removed on sync
# names that differ per GoCD server come from gocd/gocd.yaml
CONNECT_GROUP = deploy.group("connect")
SPACES_GROUP = deploy.group("spaces")
APPROVAL_GROUP = deploy.group("approval")
GROUPS = (CONNECT_GROUP, SPACES_GROUP, APPROVAL_GROUP)
ENVIRONMENT = deploy.environment()
REPO_URL = deploy.repo_url()

NO_AUTH = {"roles": [], "users": []}
ACCESS_FILE = deploy.ROOT / deploy.get("auth.access_file", "gocd/access.yaml")


def approval_access() -> dict | None:
    """Who may approve jira-approvals, from gocd/access.yaml: `approval.emails` (users, matched
    by the email on their user entry) plus optional `approval.roles`. Applied only once login
    is on (GOCD_USER set): GoCD rejects stage permissions for unknown users/roles."""
    if not os.environ.get("GOCD_USER") or not ACCESS_FILE.is_file():
        return None
    import yaml
    cfg = yaml.safe_load(ACCESS_FILE.read_text()) or {}
    approval = cfg.get("approval") or {}
    by_email = {(u.get("email") or "").lower(): u["login"] for u in cfg.get("users", [])}
    users = [by_email[e.lower()] for e in approval.get("emails", []) if e.lower() in by_email]
    roles = list(approval.get("roles", []))
    return {"users": users, "roles": roles} if users or roles else None


def space_pipeline(space: str) -> str:
    return f"{SPACE_PREFIX}{space}"


def notes_pipeline(region: str) -> str:
    return f"{NOTES_PREFIX}{region.upper()}"     # jira-release-notes-US / -INT


def _git_material() -> dict:
    # code only - commits must not trigger these pipelines (jira-connect does)
    return {"type": "git", "attributes": {
        "name": "repo", "url": REPO_URL, "branch": deploy.repo_branch(), "auto_update": True,
        "shallow_clone": True, "filter": {"ignore": ["**/*", "*"]}, "invert_filter": False}}


def _dependency(pipeline: str, stage: str) -> dict:
    return {"type": "dependency", "attributes": {
        "name": pipeline, "pipeline": pipeline, "stage": stage,
        "auto_update": True, "ignore_for_scheduling": False}}


def _exec(*args: str) -> dict:
    return {"type": "exec", "attributes": {
        "command": "gocd/run.sh", "arguments": list(args), "run_if": ["passed"]}}


def _stage(name: str, approval: str, jobs: list[dict], access: dict | None = None) -> dict:
    return {"name": name, "fetch_materials": True, "clean_working_directory": True,
            "never_cleanup_artifacts": False, "environment_variables": [],
            "approval": {"type": approval, "allow_only_on_success": True,
                         "authorization": access or NO_AUTH},
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


def connect_config(release: str) -> dict:
    job = _job(UPSTREAM_STAGE, [_exec("check"), _exec("sync")],
               tabs=[{"name": "spaces", "path": "spaces/workspaces.html"}],
               artifacts=[{"type": "build", "source": "output/workspaces.*",
                           "destination": "spaces"}])
    repo = _git_material()
    repo["attributes"]["filter"] = None          # code changes DO trigger jira-connect
    repo["attributes"]["shallow_clone"] = False
    # RELEASE is set ONCE on the "jira-release" environment. GoCD only resolves ${env:...} in
    # run labels from pipeline variables, so sync copies it here as RELEASE_LABEL (display only;
    # deliberately not named RELEASE, which would override the environment value).
    return _pipeline(CONNECT_GROUP, UPSTREAM, [repo], [_stage(UPSTREAM_STAGE, "success", [job])],
                     label="${env:RELEASE_LABEL}-${COUNT}",
                     env=[_var("RELEASE_LABEL", release or "no-release")])


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
        _var("EXISTING_RELEASES", ",".join(space.get("released", [])) or "none"),
        _var("UPCOMING_RELEASES", ",".join(space.get("upcoming", [])) or "none"),
    ]
    if current:
        # run label in the GoCD UI, e.g. "release-26.1-7"
        env.append(_var("RELEASE_LABEL", current))
        label = "${env:RELEASE_LABEL}-${COUNT}"
    else:
        # release not in this space: plain run number (empty release tag), job passes, 0 tickets
        target = space.get("target_release") or ""
        env.append(_var("SKIP_REASON", f"release {target} is not a fixVersion in {key}" if target
                        else f"{key} has no unreleased version"))
        label = "${COUNT}"
    return _pipeline(SPACES_GROUP, space_pipeline(key),
                     [_dependency(UPSTREAM, UPSTREAM_STAGE), _git_material()],
                     [_stage(SPACE_STAGE, "success", [job])], label=label, env=env)


def notes_config(region: str, spaces: list[str], release_label: str = "") -> dict:
    """ONE release-notes document for the release in one region's layout: fan-in from every
    space that builds it, fetch their collected tickets, one AI pass (filter + group across
    spaces), HTML + PDF. NOTES_REGION pins the `combine` job to this region."""
    fetches = [{"type": "fetch", "attributes": {
        "artifact_origin": "gocd", "pipeline": space_pipeline(s), "stage": SPACE_STAGE,
        "job": SPACE_JOB, "is_source_a_file": False, "source": s, "destination": "output",
        "run_if": ["passed"]}} for s in spaces]
    job = _job(NOTES_JOB, fetches + [_exec("combine")],
               tabs=[{"name": "release-notes", "path": f"combined/{region}/release_notes.html"}],
               artifacts=[{"type": "build", "source": "output/combined", "destination": ""}])
    return _pipeline(APPROVAL_GROUP, notes_pipeline(region),
                     [_dependency(space_pipeline(s), SPACE_STAGE) for s in spaces]
                     + [_git_material()],
                     [_stage(NOTES_STAGE, "success", [job])],
                     label="${env:RELEASE_LABEL}-${COUNT}",
                     env=[_var("RELEASE_LABEL", release_label or "release"),
                          _var("NOTES_REGION", region)])


def approval_config(regions: list[str], release_label: str = "") -> dict:
    """Waits for every region's release-notes pipeline; publishes all their documents."""
    fetches = [{"type": "fetch", "attributes": {
        "artifact_origin": "gocd", "pipeline": notes_pipeline(r), "stage": NOTES_STAGE,
        "job": NOTES_JOB, "is_source_a_file": False, "source": "combined",
        "destination": "output", "run_if": ["passed"]}} for r in regions]
    return _pipeline(APPROVAL_GROUP, APPROVAL,
                     [_dependency(notes_pipeline(r), NOTES_STAGE) for r in regions]
                     + [_git_material()],
                     # manual first stage: runs only when an approver clicks Approve
                     [_stage("approval", "manual",
                             [_job("publish-confluence", fetches + [_exec("publish")])],
                             access=approval_access())],
                     label="${env:RELEASE_LABEL}-${COUNT}",
                     env=[_var("RELEASE_LABEL", release_label or "release")])


class GoCD:
    def __init__(self):
        self.base = deploy.gocd_url()
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
    def environment_release(self) -> str:
        """RELEASE from the "jira-release" GoCD environment (the single place it is set)."""
        env = self.call("GET", f"/admin/environments/{ENVIRONMENT}", 3)
        if env.status_code == 200:
            for v in env.json().get("environment_variables", []):
                if v["name"] == "RELEASE" and not v.get("secure"):
                    return (v.get("value") or "").strip()
        return ""

    def sync(self, catalog: list[dict]) -> None:
        spaces = [c["workspace"] for c in catalog]
        release = catalog[0].get("target_release", "") if catalog else ""
        self.upsert(connect_config(release))
        for c in catalog:
            self.upsert(space_config(c))
        # every space is wired to its region's notes pipeline; a space without the release
        # passes with 0 tickets (see cmd_generate), so the fan-in never uses a stale run
        building = [c["workspace"] for c in catalog if c.get("current_release")]
        regions = release_regions()                      # RELEASE_REGION_<US|INT>=YES
        notes = [notes_pipeline(r) for r in regions]
        # GoCD pipeline names are case-insensitive, so a name that differs only in case (the
        # earlier jira-release-notes-int) cannot be renamed in place: detach jira-approvals
        # from it, delete it, and let it be created below with the new name
        recased = [p for p in self.pipeline_names() if p.startswith(NOTES_PREFIX)
                   and p not in notes and p.lower() in {n.lower() for n in notes}]
        if recased:
            self.upsert(approval_config([], release))
            for name in recased:
                self.delete(name)
        for r in regions:                                 # rewire fan-in before deleting spaces
            # INT = RELEASE_SPACES_INT (e.g. EI); US = every other space
            self.upsert(notes_config(r, region_spaces(r, spaces), release))
            print(f"[gocd] {notes_pipeline(r)} <- {', '.join(region_spaces(r, spaces)) or 'no space'}")
        self.upsert(approval_config(regions, release))    # before deleting old notes pipelines
        self.env_pipelines(add=[UPSTREAM] + [space_pipeline(s) for s in spaces]
                           + notes + [APPROVAL])

        wanted = {space_pipeline(s) for s in spaces} | set(notes)
        stale = [p for p in self.pipeline_names()
                 if ((p.startswith(SPACE_PREFIX) or p.startswith(NOTES_PREFIX)) and p not in wanted)
                 or p in LEGACY]
        for name in stale:
            self.delete(name)
        skipped = [c["workspace"] for c in catalog if not c.get("current_release")]
        print(f"[gocd] {len(spaces)} space pipelines; building: {', '.join(building) or 'none'}; "
              f"0 tickets (no release): {', '.join(skipped) or 'none'} -> "
              f"{', '.join(notes) or 'no release-notes pipeline (no region YES)'} -> {APPROVAL}")

    def cancel_own_stage(self) -> bool:
        """Cancel the stage this job runs in (GoCD shows it as Cancelled). False if not in GoCD."""
        keys = ("GO_PIPELINE_NAME", "GO_PIPELINE_COUNTER", "GO_STAGE_NAME", "GO_STAGE_COUNTER")
        if not all(os.environ.get(k) for k in keys):
            return False
        name, counter, stage, stage_counter = (os.environ[k] for k in keys)
        r = self.call("POST", f"/stages/{name}/{counter}/{stage}/{stage_counter}/cancel", 3,
                      headers={"X-GoCD-Confirm": "true"})
        return r.status_code == 200
