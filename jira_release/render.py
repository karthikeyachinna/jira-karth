"""Render release notes to XHTML (Confluence-safe body), a styled HTML page and a PDF."""
from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from datetime import date, datetime
from html import escape
from pathlib import Path


def _fmt_date(value: str | None) -> str:
    if not value:
        return "TBD"
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%a, %b %-d, %Y")
    except ValueError:
        return escape(value)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _ul(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>" if items else "<p>None.</p>"


def build_body(workspace: str, version: dict, issues: list[dict], content: dict,
               ready_statuses: list[str], existing_releases: list[str] | None = None) -> str:
    """XHTML fragment. Valid Confluence storage format, also used inside the PDF page."""
    by_key = {i["key"]: i for i in issues}
    name = version.get("name", "")
    ready = {s.lower() for s in ready_statuses}
    n_ready = sum(1 for i in issues if i["status"].lower() in ready)
    is_draft = n_ready < len(issues) and not version.get("released")

    out = [
        # release banner: the release number is the first and biggest thing on the page
        f'<p style="background:#0052cc;color:#ffffff;font-size:22pt;font-weight:bold;'
        f'padding:14px 18px;border-radius:6px;margin:0 0 6px 0">{escape(name)}</p>',
        f"<h1>{escape(workspace)} Release Overview"
        f"{' — DRAFT' if is_draft else ''}</h1>",
        f"<p><strong>Workspace:</strong> {escape(workspace)} · <strong>Release date:</strong> "
        f"{_fmt_date(version.get('releaseDate'))} · <strong>Total tickets:</strong> {len(issues)} · "
        f"<strong>Ready:</strong> {n_ready}/{len(issues)}</p>",
        f"<p>{escape(content['overview'])}</p>",
        f"<p><em>Generated from the Jira fixVersion on {date.today():%a, %b %-d, %Y}.</em></p>",
        "<h2>Release Information</h2>",
        _table(["Field", "Value"], [
            ["Release Number", escape(name)],
            ["Fix Version", escape(name)],
            ["Start Date", _fmt_date(version.get("startDate"))],
            ["Release Date", _fmt_date(version.get("releaseDate"))],
            ["Released", "Yes" if version.get("released") else "No"],
            ["Description", escape(version.get("description") or "—")],
        ]),
        "<h2>Headline Themes</h2>",
        "<ul>" + "".join(
            f"<li><strong>{escape(t['name'])} ({len(t['issue_keys'])})</strong> — "
            f"{escape(t['summary'])}</li>" for t in content["headline_themes"]) + "</ul>",
        "<h2>Change Implementation</h2>",
        _table(["Task", "Description", "Workflow", "Duration", "Implementer"],
               [[escape(t[k]) for k in ("task", "description", "workflow", "duration", "implementer")]
                for t in content["change_implementation"]]),
        "<h2>List of Changes</h2>",
    ]

    placed: set[str] = set()
    themes = list(content["headline_themes"])
    leftovers = [k for k in by_key if k not in {x for t in themes for x in t["issue_keys"]}]
    if leftovers:
        themes.append({"name": "Other", "summary": "", "issue_keys": leftovers})
    for theme in themes:
        keys = [k for k in theme["issue_keys"] if k in by_key and k not in placed]
        if not keys:
            continue
        placed.update(keys)
        out.append(f"<h3>{escape(theme['name'])} ({len(keys)})</h3>")
        out.append(_table(["Project", "Key", "Type", "Status", "Assignee", "Summary"], [[
            escape(i["project"]), f'<a href="{escape(i["url"])}">{escape(i["key"])}</a>',
            escape(i["type"]), escape(i["status"]), escape(i["assignee"]), escape(i["summary"]),
        ] for i in (by_key[k] for k in keys)]))

    status_counts = Counter(i["status"] for i in issues)
    out += [
        "<h2>Database Changes</h2>", f"<p>{escape(content['database_changes'])}</p>",
        "<h2>Release Readiness — Scope Warnings</h2>",
        "<p>" + escape(", ".join(f"{s}: {n}" for s, n in status_counts.most_common())) + "</p>",
        _ul(content["readiness_warnings"]),
        "<h2>Post-Deployment Activities</h2>", _ul(content["post_deployment"]),
        "<h2>Release History</h2>",
        f"<p><strong>Current:</strong> {escape(name)}</p>",
        "<p><strong>Existing releases:</strong> "
        + (escape(", ".join(existing_releases)) if existing_releases else "none yet") + "</p>",
    ]
    return "\n".join(out)


CSS = """
body{font-family:Arial,Helvetica,sans-serif;font-size:10.5pt;color:#172b4d;margin:28px}
h1{font-size:20pt;border-bottom:3px solid #0052cc;padding-bottom:6px}
h2{font-size:14pt;color:#0052cc;margin-top:26px}
h3{font-size:11.5pt;margin-top:18px}
table{border-collapse:collapse;width:100%;margin:8px 0;page-break-inside:auto}
th,td{border:1px solid #c1c7d0;padding:5px 7px;text-align:left;vertical-align:top}
th{background:#f4f5f7}
tr{page-break-inside:avoid}
a{color:#0052cc;text-decoration:none}
"""


def build_page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    chrome = os.environ.get("CHROME_BIN") or next(
        (shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable")
         if shutil.which(b)), None)
    if not chrome:
        print("[pdf] no Chrome/Chromium found on the agent (set CHROME_BIN); skipping PDF")
        return False
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                    "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
                    html_path.resolve().as_uri()],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    print(f"[pdf] wrote {pdf_path}")
    return True
