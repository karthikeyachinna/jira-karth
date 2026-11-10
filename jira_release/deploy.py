"""Deployment settings from gocd/gocd.yaml (or the file in $GOCD_CONFIG).

One place for everything that differs between GoCD servers. Secrets are never in it; they
come from .env (locally) or the GoCD environment's secure variables (in jobs).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ("JIRA_TOKEN", "OPENAI_API_KEY", "GOCD_PASSWORD")
SPACE_PREFIX = "JIRA_WORKSPACES_"   # JIRA_WORKSPACES_<KEY>=YES|NONE, one switch per Jira space
REGION_PREFIX = "RELEASE_REGION_"   # RELEASE_REGION_<US|INT>=YES|NONE, one switch per layout
REGIONS = ("US", "INT")
ON = {"YES", "Y", "TRUE", "ON", "1"}


def is_on(value) -> bool:
    return value is True or str(value).strip().upper() in ON


def region_space_lists() -> dict[str, list[str]]:
    """release.spaces as {US|INT: [space keys]}; [] = every space not listed for another region."""
    spaces = get("release.spaces", {}) or {}
    out = {}
    for r in REGIONS:
        v = spaces.get(r, spaces.get(r.lower())) or []
        v = v.split(",") if isinstance(v, str) else v
        out[r] = [str(k).strip().upper() for k in v if str(k).strip()]
    return out


def region_switches() -> dict[str, str]:
    """release.regions as {US|INT: "YES"|"NONE"}. Accepts a mapping (US: none, INT: yes) or the
    older release.region: US|INT (that one is YES, the other NONE)."""
    regions = get("release.regions")
    if isinstance(regions, dict):
        return {r: "YES" if is_on(regions.get(r, regions.get(r.lower()))) else "NONE"
                for r in REGIONS}
    one = "INT" if str(get("release.region", "US")).strip().upper().startswith("INT") else "US"
    return {r: "YES" if r == one else "NONE" for r in REGIONS}


def space_switches() -> dict[str, str]:
    """jira.spaces as {KEY: "YES"|"NONE"}. Accepts a mapping (DEV: yes, SCRUM: none) or the
    older list form (every listed space is YES)."""
    spaces = get("jira.spaces", {})
    if isinstance(spaces, list):
        return {str(k).upper(): "YES" for k in spaces}
    if isinstance(spaces, str):
        return {k.strip().upper(): "YES" for k in spaces.split(",") if k.strip()}
    return {str(k).upper(): ("YES" if is_on(v) else "NONE")
            for k, v in (spaces or {}).items()}


def config_path() -> Path:
    p = Path(os.environ.get("GOCD_CONFIG", "gocd/gocd.yaml"))
    return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def cfg() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    import yaml
    return yaml.safe_load(path.read_text()) or {}


def get(dotted: str, default=None):
    node = cfg()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def job_env() -> dict[str, str]:
    """Non-secret variables the jobs need, as set on the GoCD environment."""
    csv = lambda v: ",".join(v) if isinstance(v, list) else str(v or "")
    values = {
        "JIRA_BASE_URL": get("jira.base_url", ""),
        "JIRA_EMAIL": get("jira.email", ""),
        "READY_STATUSES": csv(get("jira.ready_statuses", [])),
        "OPENAI_MODEL": get("ai.model", ""),
        "RELEASE": get("release.number", ""),
        "GOCD_URL": get("gocd.agent_url", ""),
    }
    values.update({SPACE_PREFIX + k: v for k, v in space_switches().items()})
    values.update({REGION_PREFIX + k: v for k, v in region_switches().items()})
    for r in REGIONS:
        values[f"RELEASE_SPACES_{r}"] = ",".join(region_space_lists()[r])
        values[f"CONFLUENCE_SPACE_KEY_{r}"] = get(f"confluence.{r}.space_key",
                                                  get("confluence.space_key", ""))
        values[f"CONFLUENCE_PARENT_PAGE_ID_{r}"] = str(get(f"confluence.{r}.parent_id",
                                                           get("confluence.parent_id", "")))
    return {k: str(v) for k, v in values.items()
            if v not in (None, "") or k.startswith("RELEASE_SPACES_")}


# names used across the scripts
def environment() -> str:
    return get("gocd.environment", "jira-release")


def group(kind: str) -> str:
    return get(f"gocd.groups.{kind}", {"connect": "jira", "spaces": "jira-spaces",
                                       "approval": "jira-approval"}[kind])


def repo_url() -> str:
    return get("repo.url", "https://github.com/karthikeyachinna/jira-karth.git")


def repo_branch() -> str:
    return get("repo.branch", "main")


def gocd_url() -> str:
    """URL for API calls from where this code runs: GOCD_URL in jobs, gocd.url locally."""
    return (os.environ.get("GOCD_URL") or get("gocd.url", "http://localhost:8153/go")).rstrip("/")
