"""Render release notes (XHTML body = valid Confluence storage format), HTML page and PDF.

Two layouts, switched on by RELEASE_REGION_US / RELEASE_REGION_INT = YES, following the sample release notes:
  US  -> "Release Notes - Sprint <n>"
  INT -> "Evolus International - <release>"
Sections the AI left empty are rendered with their heading and table header but no content, so
a human can fill them in Confluence.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from datetime import date, datetime
from html import escape
from pathlib import Path

REGIONS = ("US", "INT")


def title_for(region: str, release: str) -> str:
    number = release.replace("release-", "")
    if region == "INT":
        return f"Evolus International - {release}"
    return f"Release Notes - Sprint {number}"


# ---------------------------------------------------------------- helpers
def _fmt_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%a, %b %-d, %Y")
    except ValueError:
        return escape(value)


def _table(headers: list[str], rows: list[list[str]], blank_rows: int = 1) -> str:
    """rows are already-escaped HTML cells; an empty table keeps its header + blank row(s)."""
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    if not rows:
        rows = [[""] * len(headers)] * blank_rows
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _ul(items: list[str]) -> str:
    items = [i for i in items if i]
    return ("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>") if items else "<p></p>"


def _p(text: str) -> str:
    return f"<p>{escape(text)}</p>" if text else "<p></p>"


def _banner(release: str) -> str:
    return (f'<p style="background:#0052cc;color:#ffffff;font-size:22pt;font-weight:bold;'
            f'padding:14px 18px;border-radius:6px;margin:0 0 6px 0">{escape(release)}</p>')


def _link(issue: dict) -> str:
    return f'<a href="{escape(issue["url"])}">{escape(issue["key"])}</a>'


def _themes(content: dict, by_key: dict) -> list[tuple[dict, list[dict]]]:
    """(theme, issues) pairs; tickets the AI excluded are dropped, unplaced ones go to Other."""
    excluded = {e["key"] for e in content.get("excluded", [])}
    themes = list(content["headline_themes"])
    placed_any = {k for t in themes for k in t["issue_keys"]}
    leftovers = [k for k in by_key if k not in excluded and k not in placed_any]
    if leftovers:
        themes.append({"name": "Other", "summary": "", "issue_keys": leftovers})
    out, placed = [], set()
    for t in themes:
        keys = [k for k in t["issue_keys"] if k in by_key and k not in placed and k not in excluded]
        placed.update(keys)
        if keys:
            out.append((t, [by_key[k] for k in keys]))
    return out


def _excluded(content: dict, by_key: dict) -> str:
    rows = [[_link(by_key[e["key"]]), escape(by_key[e["key"]]["summary"]), escape(e["reason"])]
            for e in content.get("excluded", []) if e["key"] in by_key]
    if not rows:
        return ""
    return ("<h2>Filtered out by AI (not in the release notes)</h2>"
            + _table(["Key", "Summary", "Reason"], rows))


def _tasks(content: dict) -> str:
    return _table(["Task", "Description", "Workflow", "Duration", "Implementer"],
                  [[escape(t[k]) for k in ("task", "description", "workflow", "duration",
                                           "implementer")]
                   for t in content["change_implementation"]])


def _validation(content: dict, int_layout: bool) -> str:
    headers = ["#", "Validation Step", "Expected Result", "Validator"] + (["Notes"] if int_layout else [])
    return _table(headers, [[str(n), escape(v["step"]), escape(v["expected"]), ""]
                            + ([""] if int_layout else [])
                            for n, v in enumerate(content.get("validation_plan", []), 1)])


def _db(content: dict) -> str:
    return _table(["Ticket Number", "File Path", "Command"],
                  [[escape(d["ticket"]), escape(d["file_path"]), escape(d["command"])]
                   for d in content.get("db_changes", [])])


CONTACTS = _table(["Name", "Group / Responsibility", "Mobile"], [], blank_rows=2)


# ---------------------------------------------------------------- US layout
def _build_us(spaces: str, version: dict, issues: list[dict], content: dict,
              ready_statuses: list[str], existing: list[str]) -> str:
    by_key = {i["key"]: i for i in issues}
    release = version.get("name", "")
    ready = {s.lower() for s in ready_statuses}
    n_ready = sum(1 for i in issues if i["status"].lower() in ready)
    draft = n_ready < len(issues) and not version.get("released")
    themes = _themes(content, by_key)
    in_notes = sum(len(v) for _, v in themes)

    out = [
        _banner(release),
        f"<h1>Release {escape(release.replace('release-', ''))} Overview"
        f"{' — DRAFT' if draft else ''}</h1>",
        f"<p><strong>Release Date:</strong> {_fmt_date(version.get('releaseDate'))} · "
        f"<strong>Implementer:</strong> @DevOps Team · <strong>Total tickets:</strong> {in_notes}</p>",
        _p(content["overview"]),
        f"<p><em>Generated from the Jira fixVersion on {date.today():%a, %b %-d, %Y} "
        f"(spaces: {escape(spaces)}).</em></p>",
        "<h2>Release Schedule</h2>",
        _table(["Milestone", "Date"], [
            ["Sprint Start", _fmt_date(version.get("startDate"))],
            ["Draft Release Branch", ""],
            ["Release Branch Cut", ""],
            ["Sprint End / Release", _fmt_date(version.get("releaseDate"))]]),
        "<h2>Release Information</h2>",
        _table(["Field", "Value"], [
            ["Release Number", escape(f"Release {release.replace('release-', '')}")],
            ["Branches", escape(release)],
            ["Fix Version", escape(release)],
            ["Release Date", _fmt_date(version.get("releaseDate"))],
            ["Implementer(s)", "@DevOps Team"],
            ["Release sign-off", "done" if version.get("released") else "pending"],
            ["CR", ""],
            ["Status Page", ""]]),
        "<h2>Headline Themes</h2>",
        "<ul>" + "".join(f"<li><strong>{escape(t['name'])}</strong> — {escape(t['summary'])} "
                         f"{len(v)} tickets.</li>" for t, v in themes) + "</ul>",
        "<h2>Change Implementation</h2>", _tasks(content),
        "<h2>List of Changes</h2>",
    ]
    for t, items in themes:
        out.append(f"<h3>{escape(t['name'])} ({len(items)})</h3>")
        out.append(_table(["Project", "Key", "Type", "Status", "Summary"], [[
            escape(i["project"]), _link(i), escape(i["type"]), escape(i["status"]),
            escape(i["summary"])] for i in items]))
    status_counts = Counter(i["status"] for i in issues)
    out += [
        "<h2>Database Changes</h2>", _db(content),
        "<h2>Release Readiness — Scope Warnings</h2>",
        f"<p>{n_ready} of {len(issues)} tickets are ready. "
        + escape(", ".join(f"{s}: {n}" for s, n in status_counts.most_common())) + "</p>",
        _ul(content["readiness_warnings"]),
        "<h2>Validation Plan</h2>", _validation(content, int_layout=False),
        "<h2>Post-Deployment Activities</h2>",
        _ul([p["description"] for p in content.get("post_deployment", [])]),
        _excluded(content, by_key),
        "<h2>Contact Information</h2>",
        _table(["Role", "Contact"], [["Release Implementer", ""], ["Escalation", ""]]),
        "<h2>Release History</h2>",
        f"<p><strong>Current:</strong> {escape(release)} · <strong>Existing releases:</strong> "
        + (escape(", ".join(existing)) if existing else "none yet") + "</p>",
    ]
    return "\n".join(x for x in out if x)


# ---------------------------------------------------------------- INT layout
def _build_int(spaces: str, version: dict, issues: list[dict], content: dict,
               ready_statuses: list[str], existing: list[str]) -> str:
    by_key = {i["key"]: i for i in issues}
    release = version.get("name", "")
    themes = _themes(content, by_key)
    out = [
        _banner(release),
        f"<h1>{escape(title_for('INT', release))}</h1>",
        _table(["Field", "Value"], [
            ["Release Number", escape(release)],
            ["Release Date", escape((version.get("releaseDate") or "")[:10])],
            ["Implementer(s)", "DevOps Team"],
            ["Total changes", str(sum(len(v) for _, v in themes))]]),
        _p(content["overview"]),
        "<h2>Change Implementation</h2>", _tasks(content),
        "<h2>Change Summary</h2>",
    ]
    for t, _ in themes:
        out += [f"<h3>{escape(t['name'])}</h3>", _p(t["summary"])]
    for t, items in themes:
        out.append(f"<h2>List of Changes — {escape(t['name'])}</h2>")
        out.append(_table(["Project", "Key", "Summary"], [[
            escape(i["project"]), _link(i), escape(i["summary"])] for i in items]))
    if content["readiness_warnings"]:
        out += ["<h2>Release Readiness</h2>", _ul(content["readiness_warnings"])]
    out += [
        "<h2>Validation Plan</h2>", _validation(content, int_layout=True),
        "<h2>DB Changes</h2>", _db(content),
        "<h2>Post Deployment Activities</h2>",
        _table(["Ticket #", "Template Name", "Description", "Status"],
               [[escape(p["ticket"]), "", escape(p["description"]), ""]
                for p in content.get("post_deployment", [])]),
        _excluded(content, by_key),
        "<h2>Contact Information</h2>", CONTACTS,
    ]
    return "\n".join(x for x in out if x)


def build_body(region: str, spaces: str, version: dict, issues: list[dict], content: dict,
               ready_statuses: list[str], existing_releases: list[str] | None = None) -> str:
    builder = _build_int if region == "INT" else _build_us
    return builder(spaces, version, issues, content, ready_statuses, existing_releases or [])


# ---------------------------------------------------------------- page + PDF
CSS = """
body{font-family:Arial,Helvetica,sans-serif;font-size:10.5pt;color:#172b4d;margin:28px}
h1{font-size:20pt;border-bottom:3px solid #0052cc;padding-bottom:6px}
h2{font-size:14pt;color:#0052cc;margin-top:26px}
h3{font-size:11.5pt;margin-top:18px}
table{border-collapse:collapse;width:100%;margin:8px 0;page-break-inside:auto}
th,td{border:1px solid #c1c7d0;padding:5px 7px;text-align:left;vertical-align:top;height:14px}
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
