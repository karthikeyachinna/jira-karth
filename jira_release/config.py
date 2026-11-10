"""Settings loaded from environment variables (GoCD env/secure vars) with a local .env fallback."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables always win (GoCD sets them)."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _csv(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def _workspaces() -> list[str] | None:
    """Spaces switched on with JIRA_WORKSPACES_<KEY>=YES (anything else, e.g. NONE, is off).
    Falls back to the older JIRA_WORKSPACES=A,B,C list. None = no filter (all Jira spaces)."""
    from .deploy import ON, SPACE_PREFIX
    switches = {k[len(SPACE_PREFIX):].upper(): v.strip().upper()
                for k, v in os.environ.items() if k.startswith(SPACE_PREFIX) and k != SPACE_PREFIX}
    if switches:
        return sorted(k for k, v in switches.items() if v in ON)
    legacy = [w.upper() for w in _csv("JIRA_WORKSPACES")]
    return legacy or None


@dataclass
class Settings:
    jira_base_url: str
    jira_email: str
    jira_token: str
    workspaces: list[str] | None   # None = every Jira space
    ready_statuses: list[str]
    openai_api_key: str
    openai_model: str
    confluence_space_key: str
    confluence_parent_page_id: str
    # selection (GoCD "Trigger with options")
    workspace: str = ""
    release: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        # precedence: real environment (GoCD job) > gocd/gocd.yaml > .env (secrets)
        from . import deploy
        for key, value in deploy.job_env().items():
            if key != "GOCD_URL":           # locally the API URL is gocd.url (deploy.gocd_url)
                os.environ.setdefault(key, value)
        _load_dotenv(ROOT / ".env")
        return cls(
            jira_base_url=os.environ.get("JIRA_BASE_URL", "").rstrip("/"),
            jira_email=os.environ.get("JIRA_EMAIL", ""),
            jira_token=os.environ.get("JIRA_TOKEN", ""),
            workspaces=_workspaces(),
            ready_statuses=_csv("READY_STATUSES", "Ready for Release,Released,Closed,Done"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-5"),
            confluence_space_key=os.environ.get("CONFLUENCE_SPACE_KEY", ""),
            confluence_parent_page_id=os.environ.get("CONFLUENCE_PARENT_PAGE_ID", ""),
            workspace=os.environ.get("WORKSPACE", "").strip().upper(),
            release=os.environ.get("RELEASE", "").strip(),
        )

    def require_jira(self) -> None:
        missing = [n for n, v in (("JIRA_BASE_URL", self.jira_base_url),
                                  ("JIRA_EMAIL", self.jira_email),
                                  ("JIRA_TOKEN", self.jira_token)) if not v]
        if missing:
            raise SystemExit(f"Missing required settings: {', '.join(missing)}")
