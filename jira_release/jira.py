"""Thin Jira Cloud REST v3 client: projects (workspaces), versions (releases), issues."""
from __future__ import annotations

from typing import Iterator

import requests

from .config import Settings

ISSUE_FIELDS = [
    "summary", "issuetype", "status", "assignee", "priority",
    "labels", "components", "parent", "description", "fixVersions", "updated",
]
MAX_DESCRIPTION_CHARS = 4000  # per ticket, keeps the AI prompt bounded on big releases


class JiraClient:
    def __init__(self, settings: Settings):
        settings.require_jira()
        self.base = settings.jira_base_url
        self.session = requests.Session()
        self.session.auth = (settings.jira_email, settings.jira_token)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=60)
        _raise_for_status(resp)
        return resp.json()

    def myself(self) -> dict:
        return self._get("/rest/api/3/myself")

    # ---- workspaces -------------------------------------------------------
    def projects(self) -> list[dict]:
        out, start = [], 0
        while True:
            page = self._get("/rest/api/3/project/search", startAt=start, maxResults=50)
            out.extend(page.get("values", []))
            if page.get("isLast", True):
                return out
            start += len(page.get("values", []))

    # ---- releases ---------------------------------------------------------
    def versions(self, project_key: str, status: str = "unreleased,released") -> list[dict]:
        out, start = [], 0
        while True:
            page = self._get(f"/rest/api/3/project/{project_key}/version",
                             startAt=start, maxResults=50, status=status,
                             orderBy="-releaseDate")
            out.extend(page.get("values", []))
            if page.get("isLast", True):
                return out
            start += len(page.get("values", []))

    def find_version(self, project_key: str, name: str) -> dict | None:
        for v in self.versions(project_key):
            if v.get("name") == name:
                return v
        return None

    # ---- issues -----------------------------------------------------------
    def search(self, jql: str) -> Iterator[dict]:
        token = None
        while True:
            body = {"jql": jql, "fields": ISSUE_FIELDS, "maxResults": 100}
            if token:
                body["nextPageToken"] = token
            resp = self.session.post(f"{self.base}/rest/api/3/search/jql", json=body, timeout=120)
            _raise_for_status(resp)
            data = resp.json()
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return

    def release_issues(self, project_key: str, release: str) -> list[dict]:
        jql = f'project = "{project_key}" AND fixVersion = "{_jql_escape(release)}" ORDER BY issuetype, key'
        return [simplify_issue(i, self.base) for i in self.search(jql)]


def simplify_issue(issue: dict, base_url: str) -> dict:
    f = issue.get("fields", {})
    parent = f.get("parent") or {}
    return {
        "key": issue["key"],
        "project": issue["key"].split("-")[0],
        "url": f"{base_url}/browse/{issue['key']}",
        "type": (f.get("issuetype") or {}).get("name", ""),
        "status": (f.get("status") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", ""),
        "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
        "summary": f.get("summary", ""),
        "labels": f.get("labels", []),
        "components": [c.get("name") for c in f.get("components", [])],
        "epic": (parent.get("fields") or {}).get("summary", "") if parent else "",
        "description": adf_to_text(f.get("description"))[:MAX_DESCRIPTION_CHARS],
    }


def adf_to_text(node) -> str:
    """Flatten Atlassian Document Format into plain text."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    parts: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            elif n.get("type") in ("hardBreak",):
                parts.append("\n")
            for child in n.get("content", []) or []:
                walk(child)
            if n.get("type") in ("paragraph", "heading", "listItem", "codeBlock"):
                parts.append("\n")

    walk(node)
    return "".join(parts).strip()


def _jql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _raise_for_status(resp: requests.Response) -> None:
    if resp.ok:
        return
    hint = {401: "check JIRA_EMAIL / JIRA_TOKEN", 403: "token lacks permission",
            404: "check JIRA_BASE_URL / project key"}.get(resp.status_code, "")
    raise SystemExit(f"Jira API {resp.status_code} on {resp.request.method} {resp.url}: "
                     f"{resp.text[:300]} {('(' + hint + ')') if hint else ''}")
