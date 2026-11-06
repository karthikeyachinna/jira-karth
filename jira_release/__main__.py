"""CLI used by the GoCD jobs.

    python -m jira_release check      # verify Jira credentials
    python -m jira_release discover   # Job 1: list workspaces (projects) and their releases
    python -m jira_release generate   # Job 2: release notes for $WORKSPACE / $RELEASE
"""
from __future__ import annotations

import json
import re
import sys

from .config import OUTPUT_DIR, Settings
from .jira import JiraClient


def cmd_check(settings: Settings) -> None:
    me = JiraClient(settings).myself()
    print(f"Connected to {settings.jira_base_url} as {me.get('displayName')}")


def cmd_discover(settings: Settings) -> None:
    jira = JiraClient(settings)
    projects = jira.projects()
    if settings.workspaces:
        projects = [p for p in projects if p["key"].upper() in settings.workspaces]
        missing = set(settings.workspaces) - {p["key"].upper() for p in projects}
        if missing:
            print(f"WARNING: workspaces not visible to this token: {', '.join(sorted(missing))}")

    catalog = []
    for p in projects:
        versions = jira.versions(p["key"], status="unreleased")
        catalog.append({
            "workspace": p["key"], "name": p["name"],
            "releases": [{"name": v["name"], "releaseDate": v.get("releaseDate"),
                          "released": v.get("released", False)} for v in versions],
        })

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "workspaces.json").write_text(json.dumps(catalog, indent=2))
    lines = ["| Workspace | Name | Unreleased releases |", "|---|---|---|"]
    for c in catalog:
        rel = ", ".join(r["name"] for r in c["releases"]) or "—"
        lines.append(f"| {c['workspace']} | {c['name']} | {rel} |")
    table = "\n".join(lines)
    (OUTPUT_DIR / "workspaces.md").write_text(table + "\n")
    print(table)

    if settings.workspace:
        match = next((c for c in catalog if c["workspace"] == settings.workspace), None)
        if not match:
            raise SystemExit(f"WORKSPACE={settings.workspace} is not one of the workspaces above")
        print(f"\nSelected WORKSPACE={settings.workspace} RELEASE={settings.release or '(not set)'}")
    else:
        print("\nNo WORKSPACE selected. Use 'Trigger with options' and set WORKSPACE and RELEASE "
              "to generate release notes.")


def cmd_generate(settings: Settings) -> None:
    from . import ai, render

    OUTPUT_DIR.mkdir(exist_ok=True)
    if not settings.workspace or not settings.release:
        msg = ("WORKSPACE / RELEASE not set - discovery-only run, nothing to generate.\n"
               "Use 'Trigger with options' and set WORKSPACE and RELEASE.")
        print(msg)
        # GoCD fails the job if the artifact rule matches nothing, so always leave a file behind
        (OUTPUT_DIR / "release_notes.html").write_text(
            f"<!doctype html><html><body><p>{msg.replace(chr(10), '<br/>')}</p></body></html>")
        return

    jira = JiraClient(settings)
    version = jira.find_version(settings.workspace, settings.release)
    if not version:
        names = [v["name"] for v in jira.versions(settings.workspace)]
        raise SystemExit(f"Release {settings.release!r} not found in {settings.workspace}. "
                         f"Available: {', '.join(names) or 'none'}")

    issues = jira.release_issues(settings.workspace, settings.release)
    print(f"[jira] {len(issues)} tickets in {settings.workspace} / {settings.release}")
    if not issues:
        raise SystemExit("No tickets on this fixVersion - nothing to write.")

    content = ai.write_content(settings, settings.workspace, version, issues)

    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{settings.workspace}-{settings.release}")
    (OUTPUT_DIR / f"{slug}.content.json").write_text(json.dumps(
        {"version": version, "issues": issues, "content": content}, indent=2))

    title = f"Release Notes - {settings.workspace} {settings.release}"
    body = render.build_body(settings.workspace, version, issues, content, settings.ready_statuses)
    html_path = OUTPUT_DIR / "release_notes.html"   # fixed name -> GoCD custom tab
    html_path.write_text(render.build_page(title, body))
    print(f"[html] wrote {html_path}")

    pdf_path = OUTPUT_DIR / f"{slug}.pdf"
    has_pdf = render.html_to_pdf(html_path, pdf_path)

    if settings.confluence_space_key:
        from . import confluence
        confluence.publish(settings, title, body, pdf_path if has_pdf else None)
    else:
        print("[confluence] CONFLUENCE_SPACE_KEY not set - skipping publish")


def main() -> None:
    commands = {"check": cmd_check, "discover": cmd_discover, "generate": cmd_generate}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]](Settings.load())


if __name__ == "__main__":
    main()
