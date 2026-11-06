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
        "JIRA_WORKSPACES": csv(get("jira.spaces", [])),
        "READY_STATUSES": csv(get("jira.ready_statuses", [])),
        "OPENAI_MODEL": get("ai.model", ""),
        "CONFLUENCE_SPACE_KEY": get("confluence.space_key", ""),
        "CONFLUENCE_PARENT_PAGE_ID": str(get("confluence.parent_id", "")),
        "RELEASE": get("release.number", ""),
        "RELEASE_REGION": get("release.region", "US"),
        "GOCD_URL": get("gocd.agent_url", ""),
    }
    return {k: str(v) for k, v in values.items() if v not in (None, "")}


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
