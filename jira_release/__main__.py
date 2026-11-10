"""CLI used by the GoCD jobs.

    python -m jira_release check      # verify Jira credentials
    python -m jira_release discover   # list spaces and the release each one will build
    python -m jira_release sync       # jira-connect job: discover + create one pipeline per space
    python -m jira_release generate   # jira-space-<KEY> pipeline: collect $WORKSPACE's tickets
    python -m jira_release combine    # jira-release-notes-<US|INT> pipeline: AI release notes, all spaces
    python -m jira_release publish    # jira-approvals pipeline: publish it to Confluence
"""
from __future__ import annotations

import json
import os
import re
import sys
from html import escape

from .config import OUTPUT_DIR, Settings
from .jira import JiraClient, pick_release


def target_release() -> str:
    """The release to build: RELEASE on the GoCD environment "jira-release" (inside a GoCD job it
    arrives as an environment variable). Outside GoCD, read the same value via the GoCD API.
    Empty = each space builds its next unreleased version."""
    value = os.environ.get("RELEASE", "").strip()
    if value or os.environ.get("GO_PIPELINE_NAME"):
        return value
    try:
        from .gocd import GoCD
        return GoCD().environment_release()
    except Exception:               # GoCD not reachable: local run without a release
        return ""


def cmd_check(settings: Settings) -> None:
    me = JiraClient(settings).myself()
    print(f"Connected to {settings.jira_base_url} as {me.get('displayName')}")


def cmd_discover(settings: Settings) -> list[dict]:
    jira = JiraClient(settings)
    projects = jira.projects()
    if settings.workspaces is not None:          # only spaces with JIRA_WORKSPACES_<KEY>=YES
        print(f"Spaces switched on: {', '.join(settings.workspaces) or 'none'}")
        projects = [p for p in projects if p["key"].upper() in settings.workspaces]
        missing = set(settings.workspaces) - {p["key"].upper() for p in projects}
        if missing:
            print(f"WARNING: spaces not visible to this token: {', '.join(sorted(missing))}")

    target = target_release()
    print(f"RELEASE: {target or '(not set - using each space next unreleased version)'}\n")
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
    target = target_release()
    if target and not any(c["current_release"] for c in catalog):
        # nothing to build: cancel (skip) jira-connect, so no space pipeline is triggered and
        # the existing pipelines are left as they are
        print(f"\nRELEASE={target!r} is not a fixVersion in any Jira space - skipping this run. "
              "Create it in Jira or change RELEASE in GoCD (Admin > Environments > jira-release).")
        if not _cancel_self():
            raise SystemExit(1)   # outside GoCD: still signal that nothing was done
        return
    GoCD().sync(catalog)


def _badge(text: str) -> str:
    return (f'<span style="background:#0052cc;color:#fff;font-weight:bold;padding:3px 10px;'
            f'border-radius:4px;font-size:1.1em">{escape(text)}</span>')


def _status_page(out_dir, message: str) -> None:
    """Leave a file behind: GoCD fails a job whose artifact path matches nothing."""
    (out_dir / "release_notes.html").write_text(
        f"<!doctype html><html><body><p>{escape(message)}</p></body></html>")
    print(message)


def _cancel_self() -> bool:
    """Cancel the GoCD stage this job runs in, so the run shows the cancelled (skip) sign.
    Returns False when not running inside GoCD."""
    import time
    from .gocd import GoCD

    if not GoCD().cancel_own_stage():
        return False
    print("[gocd] cancelled this stage (skip)", flush=True)
    time.sleep(60)              # GoCD stops the job; this only waits for that to happen
    return True


def cmd_generate(settings: Settings) -> None:
    from . import render

    if not settings.workspace:
        raise SystemExit("WORKSPACE is not set (each space job sets it)")
    out = OUTPUT_DIR / settings.workspace
    out.mkdir(parents=True, exist_ok=True)

    # A space without the release PASSES with 0 tickets (no space.json) instead of cancelling:
    # every space stays wired to its region's notes pipeline, and the fan-in always uses this
    # run's (empty) result rather than an older passed run with stale tickets.
    skip = os.environ.get("SKIP_REASON", "")
    if skip:
        return _status_page(out, f"{settings.workspace}: {skip} - 0 tickets.")

    jira = JiraClient(settings)
    if settings.release:
        version = jira.find_version(settings.workspace, settings.release)
        if not version:
            return _status_page(out, f"{settings.workspace}: release {settings.release} is not "
                                     "a fixVersion in this space - 0 tickets.")
    else:
        version = pick_release(jira.versions(settings.workspace, status="unreleased"))
        if not version:
            return _status_page(out, f"{settings.workspace}: no unreleased release - 0 tickets.")
    release = version["name"]

    issues = jira.release_issues(settings.workspace, release)
    print(f"[jira] {len(issues)} tickets in {settings.workspace} / {release}")
    if not issues:
        return _status_page(out, f"{settings.workspace} / {release}: no tickets on this "
                                 "fixVersion - skipped.")

    # collect only - the AI runs once over ALL spaces in `combine`
    (out / "space.json").write_text(json.dumps(
        {"workspace": settings.workspace, "version": version, "issues": issues}, indent=2))
    rows = "".join(f"<tr><td>{escape(i['key'])}</td><td>{escape(i['type'])}</td>"
                   f"<td>{escape(i['status'])}</td><td>{escape(i['summary'])}</td></tr>"
                   for i in issues)
    (out / "release_notes.html").write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{render.CSS}</style></head>"
        f"<body><p>{_badge(release)}</p><h2>{escape(settings.workspace)}: {len(issues)} tickets "
        "collected</h2><p>They are merged with the other spaces and written up by AI in the "
        "<b>jira-release-notes-US / -INT</b> pipelines.</p><table><thead><tr><th>Key</th><th>Type</th>"
        f"<th>Status</th><th>Summary</th></tr></thead><tbody>{rows}</tbody></table></body></html>")
    print(f"[done] collected {len(issues)} tickets for {settings.workspace} / {release}")


def cmd_combine(settings: Settings) -> None:
    """jira-release-notes-<region> pipeline: merge every space's tickets, then one AI pass and
    one document for NOTES_REGION (set on the pipeline). Without it (local runs): one document
    per region switched on (RELEASE_REGION_US / RELEASE_REGION_INT = YES)."""
    from .config import region_spaces, release_regions

    spaces = [json.loads(p.read_text()) for p in sorted(OUTPUT_DIR.glob("*/space.json"))]
    pinned = os.environ.get("NOTES_REGION", "").strip().upper()
    regions = [pinned] if pinned else release_regions()
    out = OUTPUT_DIR / "combined"
    out.mkdir(parents=True, exist_ok=True)
    if not regions:
        for region in ("US", "INT"):                   # a page for every tab
            (out / region).mkdir(exist_ok=True)
            _status_page(out / region, "No region is switched on (RELEASE_REGION_US / "
                                       "RELEASE_REGION_INT = YES) - nothing to write.")
        return print("No region is switched on - nothing to write.")

    print(f"[combine] regions switched on: {', '.join(regions)}")
    written = 0
    for region in regions:
        keys = region_spaces(region, [s["workspace"] for s in spaces])   # RELEASE_SPACES_<R>
        mine = [s for s in spaces if s["workspace"] in keys and s["issues"]]
        if not mine:
            (out / region).mkdir(exist_ok=True)
            _status_page(out / region, f"{region}: no tickets on this release in its spaces "
                                       f"(RELEASE_SPACES_{region}) - nothing to write.")
            continue
        _combine_region(settings, region, mine, out / region)
        written += 1
    if not written:   # passes (no cancel) so jira-approvals never falls back to an older run
        print("No release notes written - nothing for jira-approvals to publish.")


def _combine_region(settings: Settings, region: str, spaces: list[dict], out) -> None:
    from . import ai, render

    out.mkdir(parents=True, exist_ok=True)
    version = spaces[0]["version"]
    release = version["name"]
    issues = [i for s in spaces for i in s["issues"]]
    names = ", ".join(s["workspace"] for s in spaces)
    print(f"[combine] {release} ({region} layout): {len(issues)} tickets from {names}")

    content = ai.write_content(settings, region, names, version, issues)
    kept = sum(len(t["issue_keys"]) for t in content["headline_themes"])
    print(f"[ai] {kept} tickets in {len(content['headline_themes'])} themes, "
          f"{len(content.get('excluded', []))} filtered out")

    title = render.title_for(region, release)
    existing = [r for r in os.environ.get("EXISTING_RELEASES", "").split(",") if r and r != "none"]
    body = render.build_body(region, names, version, issues, content, settings.ready_statuses,
                             existing_releases=existing)
    (out / "content.json").write_text(json.dumps(
        {"version": version, "issues": issues, "content": content}, indent=2))
    (out / "body.xhtml").write_text(body)
    html_path = out / "release_notes.html"           # fixed name -> GoCD custom tab
    html_path.write_text(render.build_page(title, body))
    pdf_path = out / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', title).strip('-')}.pdf"
    has_pdf = render.html_to_pdf(html_path, pdf_path)
    (out / "meta.json").write_text(json.dumps(
        {"title": title, "region": region, "pdf": pdf_path.name if has_pdf else None}))
    print(f"[done] {title} -> {out}")


def cmd_publish(settings: Settings) -> None:
    metas = sorted(OUTPUT_DIR.glob("combined/*/meta.json"))   # one per region
    if not metas:
        print("Nothing to publish - no combined release notes were produced.")
        return
    from . import confluence

    for meta_path in metas:
        meta = json.loads(meta_path.read_text())
        region = meta.get("region") or meta_path.parent.name
        space_key, parent_id = settings.confluence_target(region)
        if not space_key:
            print(f"CONFLUENCE_SPACE_KEY_{region} not set - {meta['title']!r} is not published.")
            continue
        print(f"[confluence] {region}: space {space_key}, parent {parent_id or '(space home)'}")
        folder = meta_path.parent
        pdf = folder / meta["pdf"] if meta.get("pdf") else None
        confluence.publish(settings, meta["title"], (folder / "body.xhtml").read_text(), pdf,
                           space_key, parent_id)


def main() -> None:
    commands = {"check": cmd_check, "discover": cmd_discover, "sync": cmd_sync,
                "generate": cmd_generate, "combine": cmd_combine, "publish": cmd_publish}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]](Settings.load())


if __name__ == "__main__":
    main()
