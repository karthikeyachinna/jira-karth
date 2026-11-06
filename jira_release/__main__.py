"""CLI used by the GoCD jobs.

    python -m jira_release check      # verify Jira credentials
    python -m jira_release discover   # list spaces and the release each one will build
    python -m jira_release sync       # jira-connect job: discover + create one pipeline per space
    python -m jira_release generate   # jira-space-<KEY> pipeline: release notes for $WORKSPACE [$RELEASE]
    python -m jira_release publish    # jira-approvals pipeline: publish every generated space to Confluence
"""
from __future__ import annotations

import json
import re
import sys
from html import escape

from .config import OUTPUT_DIR, Settings
from .jira import JiraClient, pick_release


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

    catalog = []
    for p in sorted(projects, key=lambda p: p["key"]):
        versions = jira.versions(p["key"], status="unreleased")
        nxt = pick_release(versions)
        catalog.append({"workspace": p["key"], "name": p["name"],
                        "next_release": nxt["name"] if nxt else None,
                        "releases": [v["name"] for v in versions]})

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "workspaces.json").write_text(json.dumps(catalog, indent=2))
    lines = ["| Space | Name | Release to build | All unreleased |", "|---|---|---|---|"]
    for c in catalog:
        lines.append(f"| {c['workspace']} | {c['name']} | {c['next_release'] or '—'} | "
                     f"{', '.join(c['releases']) or '—'} |")
    table = "\n".join(lines)
    (OUTPUT_DIR / "workspaces.md").write_text(table + "\n")
    print(table)
    return catalog


def cmd_sync(settings: Settings) -> None:
    from .gocd import GoCD

    catalog = cmd_discover(settings)
    if not catalog:
        raise SystemExit("No Jira spaces found - nothing to fan out")
    GoCD().sync([c["workspace"] for c in catalog])


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
            raise SystemExit(f"Release {settings.release!r} not found in {settings.workspace}")
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

    title = f"Release Notes - {settings.workspace} {release}"
    body = render.build_body(settings.workspace, version, issues, content, settings.ready_statuses)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{settings.workspace}-{release}")

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
