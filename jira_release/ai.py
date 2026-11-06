"""AI agent that writes the narrative parts of the release notes with OpenAI.

Ticket tables are built from Jira data in code; the model writes the prose (overview, themes,
deploy tasks, readiness, post-deploy). The house style is defined in code below (STYLE_GUIDE),
so no reference file is needed at runtime.
"""
from __future__ import annotations

import json
from collections import defaultdict

from .config import Settings

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "headline_themes", "change_implementation", "database_changes",
                 "readiness_warnings", "post_deployment"],
    "properties": {
        "overview": {"type": "string",
                     "description": "2-4 sentence overview of the release."},
        "headline_themes": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "summary", "issue_keys"],
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "issue_keys": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "change_implementation": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["task", "description", "workflow", "duration", "implementer"],
                "properties": {k: {"type": "string"} for k in
                               ("task", "description", "workflow", "duration", "implementer")},
            },
        },
        "database_changes": {"type": "string"},
        "readiness_warnings": {"type": "array", "items": {"type": "string"}},
        "post_deployment": {"type": "array", "items": {"type": "string"}},
    },
}

STYLE_GUIDE = """House style for release notes (the layout the PDF renderer prints):

1. Overview - 2-4 sentences: what the release delivers, how many tickets, and whether it is a DRAFT
   (not every ticket is in a ready status) or final.
2. Headline Themes - 4-8 themes. Each is a short noun phrase followed by one or two sentences on
   what changed, from the user's point of view. Order them by impact, largest first. Example:
     "Subscriptions - Portal and mobile app can cancel and edit subscriptions; reminder and
      timezone fixes."
     "Code quality - Fixes from the nightly code review: memory cap, duplicated markup, dead code."
   Use "Platform & Other" for leftovers and "Security & infrastructure" for CVE or infra work.
3. Change Implementation - ordered deploy tasks Task-1, Task-2, ... Deployment order matters:
   data-migration scripts first, then back-end services, then front-end apps, then mobile builds,
   then independent infra tasks. Each task has: description (what is deployed and from which
   branch), workflow (deploy job name, or "TBD"), duration (or "TBD"), implementer (default
   "@DevOps Team"; mobile builds "Mobile Team").
4. Database Changes - list migration scripts or schema changes mentioned in tickets; otherwise
   "To be confirmed when the release branch is cut."
5. Release Readiness - Scope Warnings - one bullet per finding, always citing ticket keys:
   - "Not yet ready - <status> (<n>): KEY-1, KEY-2" grouped by status
   - "Blocked: KEY-1 (<summary>). Unblock or move to the next release."
   - "Still in development: KEY-1 (<status>)"
   - "Unassigned and not ready: KEY-1, KEY-2"
6. Post-Deployment Activities - 3-6 imperative bullets: monitoring report the morning after,
   confirm key user flows from the themes, watch new jobs or campaigns, record validation and
   change-management (SOX) evidence against the release.

Tone: short factual sentences, present tense, ticket keys cited, no marketing language, no emoji."""

SYSTEM = f"""You write release notes for an engineering release, for DevOps, QA and product readers.

{STYLE_GUIDE}

Rules:
- Put EVERY ticket into exactly one headline theme; issue_keys lists them.
- Only state facts supported by the ticket data. When something is unknown (durations, workflows,
  DB scripts), write "TBD" rather than inventing it.
- A ticket counts as ready when its status is one of the ready_statuses in the input."""


def write_with_openai(settings: Settings, workspace: str, version: dict, issues: list[dict]) -> dict:
    from openai import OpenAI  # imported lazily so `discover` works without the package

    client = OpenAI(api_key=settings.openai_api_key)

    payload = {
        "workspace": workspace,
        "release": {k: version.get(k) for k in ("name", "description", "startDate",
                                                 "releaseDate", "released")},
        "ready_statuses": settings.ready_statuses,
        "tickets": issues,
    }

    print(f"[ai] calling OpenAI {settings.openai_model} with {len(issues)} tickets ...")
    response = client.responses.create(
        model=settings.openai_model,
        instructions=SYSTEM,
        input="Write the release notes content for this release. Jira data:\n\n"
              + json.dumps(payload, indent=1),
        text={"format": {"type": "json_schema", "name": "release_notes",
                         "schema": SCHEMA, "strict": True}},
        max_output_tokens=32000,
        timeout=600,
    )

    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", "unknown")
        raise SystemExit(f"[ai] OpenAI response incomplete ({reason}); release may be too large")
    for item in response.output:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                raise SystemExit(f"[ai] OpenAI declined the request: {part.refusal}")

    u = response.usage
    print(f"[ai] done: model={response.model} in={u.input_tokens} out={u.output_tokens}")
    return json.loads(response.output_text)


def write_without_ai(settings: Settings, workspace: str, version: dict, issues: list[dict]) -> dict:
    """Deterministic fallback (no OPENAI_API_KEY): themes by epic / component."""
    groups: dict[str, list[str]] = defaultdict(list)
    for i in issues:
        groups[i["epic"] or (i["components"][0] if i["components"] else "Other")].append(i["key"])
    ready = {s.lower() for s in settings.ready_statuses}
    not_ready = [i for i in issues if i["status"].lower() not in ready]
    warnings = []
    if not_ready:
        warnings.append(f"{len(not_ready)} of {len(issues)} tickets are not ready: "
                        + ", ".join(f"{i['key']} ({i['status']})" for i in not_ready))
    unassigned = [i["key"] for i in not_ready if i["assignee"] == "Unassigned"]
    if unassigned:
        warnings.append("Unassigned and not ready: " + ", ".join(unassigned))
    return {
        "overview": f"{version.get('name')} for {workspace} contains {len(issues)} tickets.",
        "headline_themes": [{"name": k, "summary": "Grouped by epic / component.", "issue_keys": v}
                            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
        "change_implementation": [{"task": "Task-1", "description": "Deploy release branch",
                                   "workflow": "TBD", "duration": "TBD",
                                   "implementer": "@DevOps Team"}],
        "database_changes": "TBD",
        "readiness_warnings": warnings,
        "post_deployment": ["Run the validation plan and record results."],
    }


def write_content(settings: Settings, workspace: str, version: dict, issues: list[dict]) -> dict:
    if settings.openai_api_key:
        return write_with_openai(settings, workspace, version, issues)
    print("[ai] OPENAI_API_KEY not set - using deterministic grouping (no AI)")
    return write_without_ai(settings, workspace, version, issues)
