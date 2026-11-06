"""CLI used by the GoCD jobs.

    python -m jira_release check      # verify Jira credentials
    python -m jira_release discover   # list spaces and the release each one will build
    python -m jira_release sync       # jira-connect job: discover + create one pipeline per space
    python -m jira_release generate   # jira-space-<KEY> pipeline: release notes for $WORKSPACE [$RELEASE]
    python -m jira_release publish    # jira-approvals pipeline: publish every generated space to Confluence
"""
from __future__ import annotations

import json
import os
import re
import sys
from html import escape

from .config import OUTPUT_DIR, ROOT, Settings
from .jira import JiraClient, pick_release


def release_from_file() -> str:
    """The release written in the RELEASE file (first non-comment line), or ''."""
    path = ROOT / "RELEASE"
    if not path.is_file():
        return ""
    for line in path.read_text().splitlines():
        if line.strip() and not line.strip().startswith("#"):
            return line.strip()
    return ""


def cmd_check(settings: Settings) -> None:
    me = JiraClient(settings).myself()
    print(f"Connected to {settings.jira_base_url} as {me.get('displayName')}")


def cmd_discover(settings: Settings) -> list[dict]:
    jira = JiraClient(settings)
    projects = jira.projects()
    if settings.workspaces:
        projects = [p for p in projects if p["key"].upper() in settings.workspaces]
        missing = set(settings.workspaces) - {p["key"].upper() for p in projects}
        if missing:
            print(f"WARNING: spaces not visible to this token: {', '.join(sorted(missing))}")

    target = release_from_file()
    print(f"RELEASE file: {target or '(empty - using each space next unreleased version)'}\n")
    catalog = []
    for p in sorted(projects, key=lambda p: p["key"]):
        versions = jira.versions(p["key"])                     # released + unreleased
        unreleased = [v for v in versions if not v.get("released") and not v.get("archived")]
        released = sorted((v for v in versions if v.get("released")),
                          key=lambda v: v.get("releaseDate") or "", reverse=True)
        if target:   # the RELEASE file decides; spaces without that fixVersion are skipped
            current = next((v for v in versions if v["name"] == target), None)
        else:
            current = pick_release(unreleased)
        catalog.append({
            "workspace": p["key"], "name": p["name"],
            "target_release": target,
            "label": target or (current["name"] if current else "no-release"),
            "current_release": current["name"] if current else None,
            "upcoming": [v["name"] for v in unreleased if v is not current],
            "released": [v["name"] for v in released],
        })

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "workspaces.json").write_text(json.dumps(catalog, indent=2))
    lines = ["| Space | Name | Current release (building) | Upcoming | Existing (released) |",
             "|---|---|---|---|---|"]
    for c in catalog:
        building = c["current_release"] or (f"not in space ({target})" if target else "—")
        lines.append(f"| {c['workspace']} | {c['name']} | {building} | "
                     f"{', '.join(c['upcoming']) or '—'} | {', '.join(c['released']) or '—'} |")
    table = "\n".join(lines)
    (OUTPUT_DIR / "workspaces.md").write_text(table + "\n")
    from .render import CSS
    rows = "".join(
        f"<tr><td><b>{escape(c['workspace'])}</b></td><td>{escape(c['name'])}</td>"
        f"<td>{_badge(c['current_release']) if c['current_release'] else ('not in this space' if target else '—')}</td>"
        f"<td>{escape(', '.join(c['upcoming']) or '—')}</td>"
        f"<td>{escape(', '.join(c['released']) or '—')}</td></tr>" for c in catalog)
    (OUTPUT_DIR / "workspaces.html").write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
        f"<h2>Jira spaces and releases</h2><p>Release to build: {_badge(target or 'next unreleased per space')}</p><table><thead><tr><th>Space</th><th>Name</th>"
        "<th>Current release (building)</th><th>Upcoming</th><th>Existing (released)</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></body></html>")
    print(table)
    return catalog


def cmd_sync(settings: Settings) -> None:
    from .gocd import GoCD

    catalog = cmd_discover(settings)
    if not catalog:
        raise SystemExit("No Jira spaces found - nothing to fan out")
    target = release_from_file()
    if target and not any(c["current_release"] for c in catalog):
        raise SystemExit(f"RELEASE file says {target!r}, but no Jira space has that fixVersion. "
                         "Create it in Jira or fix the RELEASE file.")
    GoCD().sync(catalog)


def _badge(text: str) -> str:
    return (f'<span style="background:#0052cc;color:#fff;font-weight:bold;padding:3px 10px;'
            f'border-radius:4px;font-size:1.1em">{escape(text)}</span>')


def _status_page(out_dir, message: str) -> None:
    """Leave a file behind: GoCD fails a job whose artifact path matches nothing."""
    (out_dir / "release_notes.html").write_text(
        f"<!doctype html><html><body><p>{escape(message)}</p></body></html>")
    print(message)


def cmd_generate(settings: Settings) -> None:
    from . import ai, render

    if not settings.workspace:
        raise SystemExit("WORKSPACE is not set (each space job sets it)")
    out = OUTPUT_DIR / settings.workspace
    out.mkdir(parents=True, exist_ok=True)

    jira = JiraClient(settings)
    if settings.release:
        version = jira.find_version(settings.workspace, settings.release)
        if not version:
            return _status_page(out, f"{settings.workspace}: release {settings.release} is not a "
                                     "fixVersion in this space - skipped.")
    else:
        version = pick_release(jira.versions(settings.workspace, status="unreleased"))
        if not version:
            return _status_page(out, f"{settings.workspace}: no unreleased release - skipped.")
    release = version["name"]

    issues = jira.release_issues(settings.workspace, release)
    print(f"[jira] {len(issues)} tickets in {settings.workspace} / {release}")
    if not issues:
        return _status_page(out, f"{settings.workspace} / {release}: no tickets on this "
                                 "fixVersion - skipped.")

    content = ai.write_content(settings, settings.workspace, version, issues)

    title = f"{release} — Release Notes — {settings.workspace}"
    existing = [r for r in os.environ.get("EXISTING_RELEASES", "").split(",") if r and r != "none"]
    body = render.build_body(settings.workspace, version, issues, content, settings.ready_statuses,
                             existing_releases=existing)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{release}_{settings.workspace}_release-notes")

    (out / "content.json").write_text(json.dumps(
        {"version": version, "issues": issues, "content": content}, indent=2))
    (out / "body.xhtml").write_text(body)
    html_path = out / "release_notes.html"           # fixed name -> GoCD custom tab
    html_path.write_text(render.build_page(title, body))
    pdf_path = out / f"{slug}.pdf"
    has_pdf = render.html_to_pdf(html_path, pdf_path)
    (out / "meta.json").write_text(json.dumps(
        {"title": title, "pdf": pdf_path.name if has_pdf else None}))
    print(f"[done] {title} -> {out}")


def cmd_publish(settings: Settings) -> None:
    metas = sorted(OUTPUT_DIR.glob("*/meta.json"))
    if not metas:
        print("Nothing to publish - no space produced release notes.")
        return
    if not settings.confluence_space_key:
        print("CONFLUENCE_SPACE_KEY not set - approved, but nothing is published.")
        return
    from . import confluence

    for meta_path in metas:
        meta = json.loads(meta_path.read_text())
        folder = meta_path.parent
        pdf = folder / meta["pdf"] if meta.get("pdf") else None
        confluence.publish(settings, meta["title"], (folder / "body.xhtml").read_text(), pdf)


def main() -> None:
    commands = {"check": cmd_check, "discover": cmd_discover, "sync": cmd_sync,
                "generate": cmd_generate, "publish": cmd_publish}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]](Settings.load())


if __name__ == "__main__":
    main()
