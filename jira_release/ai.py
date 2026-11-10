"""AI agent that filters and groups ALL spaces' tickets into one release notes document.

The layout follows the sample release notes for each region switched on (RELEASE_REGION_<US|INT>=YES):
  US  -> "Release Notes - Sprint <n>"            (EE-Release Notes - Sprint 26.14 sample)
  INT -> "Evolus International - <release>"      (EE-Evolus International - release-26.14.2)
Ticket tables come from Jira in code; the model fills the narrative sections. A section the
tickets give no evidence for is returned EMPTY and rendered blank - never guessed.
"""
from __future__ import annotations

import json
from collections import defaultdict

from .config import Settings

_STR = {"type": "string"}


def _rows(*fields: str) -> dict:
    return {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": list(fields),
        "properties": {f: _STR for f in fields}}}


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "headline_themes", "excluded", "change_implementation",
                 "db_changes", "readiness_warnings", "validation_plan", "post_deployment"],
    "properties": {
        "overview": _STR,
        "headline_themes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "summary", "issue_keys"],
            "properties": {"name": _STR, "summary": _STR,
                           "issue_keys": {"type": "array", "items": _STR}}}},
        "excluded": _rows("key", "reason"),
        "change_implementation": _rows("task", "description", "workflow", "duration",
                                       "implementer"),
        "db_changes": _rows("ticket", "file_path", "command"),
        "readiness_warnings": {"type": "array", "items": _STR},
        "validation_plan": _rows("step", "expected"),
        "post_deployment": _rows("ticket", "description"),
    },
}

COMMON = """Filtering (you decide what belongs in the release notes):
- Group tickets by WHAT CHANGED, across projects - a theme may mix keys from several Jira spaces.
  Never make one theme per project.
- Put into `excluded` (one-line reason) tickets that do not belong in release notes: sub-tasks or
  duplicates of another listed ticket, test / spike / placeholder tickets, tickets with no
  meaningful summary, Cancelled / Won't Do tickets.
- Every ticket key appears exactly once: in one headline theme OR in excluded.

Evidence rule - FOLLOW STRICTLY:
- Fill a section only with facts found in the ticket data (summary, description, labels,
  components, status, assignee, release dates).
- If the tickets give no evidence for a section, return it EMPTY ("" or []). It is rendered
  blank for a human to complete. Never write "TBD", never invent workflows, durations, file
  paths, commands, validators, contacts or ticket numbers.
- A ticket counts as ready when its status is one of the ready_statuses in the input.
Tone: short factual sentences, present tense, ticket keys cited, no marketing language, no emoji."""

STYLE = {
    "US": """Layout: US release notes ("Release Notes - Sprint <n>").
- overview: 2-4 sentences: what the release delivers, ticket count, DRAFT if not all are ready.
- headline_themes: 4-8 themes, largest first. name = short noun phrase ("Subscriptions",
  "Promos & Offers", "Code quality (nightly review)", "Platform & Other"); summary = one or two
  sentences on what changed for users, e.g. "Portal, Monarch and Practice App can cancel and
  edit subscriptions; reminders, order blocks and timezone fixes."
- change_implementation: ordered Task-1..n - data-migration scripts first, then back-end
  services, then front-end apps, then mobile builds, then independent infra tasks; only tasks
  the tickets imply. implementer "@DevOps Team" (mobile builds "Mobile Team") when a task exists.
- db_changes: migration scripts / schema changes named in tickets (ticket, file_path, command).
- readiness_warnings: one bullet per finding citing keys - "Not yet ready - <status> (<n>):
  KEY-1, KEY-2"; "Blocked: KEY (<summary>). Unblock or move to the next release.";
  "Still in development: KEY (<status>)"; "Unassigned and not ready: KEY-1, KEY-2".
- validation_plan: one row per user-facing flow from the themes (step, expected result).
- post_deployment: 3-6 follow-ups the tickets call for (ticket may be "").""",
    "INT": """Layout: International release notes ("Evolus International - <release>").
- overview: one sentence on the release (used as the intro line).
- headline_themes: the Change Summary - 1-4 themes such as "Platform & Infrastructure"; summary
  = a short paragraph describing the technical change and its effect for the affected market
  (e.g. AU, DE). These become "List of Changes - <theme>" tables.
- change_implementation: deploy tasks Task-1..n in order (base image, backend, regional
  portals) - only what the tickets imply; implementer "@DevOps Team" when a task exists.
- db_changes: rows (ticket, file_path, command) only if tickets name DB scripts.
- readiness_warnings: [] unless tickets are blocked or not ready (then cite keys).
- validation_plan: rows only for flows the tickets describe.
- post_deployment: rows (ticket, description) only if tickets call for follow-up work.""",
}


def system_prompt(region: str) -> str:
    return (f"You write ONE consolidated release notes document for an engineering release that "
            f"spans several Jira spaces, for DevOps, QA and product readers.\n\n"
            f"{STYLE[region]}\n\n{COMMON}")


def write_with_openai(settings: Settings, region: str, workspace: str, version: dict,
                      issues: list[dict]) -> dict:
    from openai import OpenAI  # imported lazily so `discover` works without the package

    client = OpenAI(api_key=settings.openai_api_key)
    payload = {
        "region": region,
        "spaces": workspace,
        "release": {k: version.get(k) for k in ("name", "description", "startDate",
                                                 "releaseDate", "released")},
        "ready_statuses": settings.ready_statuses,
        "tickets": issues,
    }
    print(f"[ai] calling OpenAI {settings.openai_model} ({region} layout) with {len(issues)} "
          f"tickets from {len({i['project'] for i in issues})} space(s) ...")
    response = client.responses.create(
        model=settings.openai_model,
        instructions=system_prompt(region),
        input="Analyse these tickets and write the release notes content. Jira data:\n\n"
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


def write_without_ai(settings: Settings, region: str, workspace: str, version: dict,
                     issues: list[dict]) -> dict:
    """Deterministic fallback (no OPENAI_API_KEY): themes by epic / component, rest blank."""
    groups: dict[str, list[str]] = defaultdict(list)
    for i in issues:
        groups[i["epic"] or (i["components"][0] if i["components"] else "Other")].append(i["key"])
    ready = {s.lower() for s in settings.ready_statuses}
    not_ready = [i for i in issues if i["status"].lower() not in ready]
    warnings = []
    if not_ready:
        warnings.append(f"{len(not_ready)} of {len(issues)} tickets are not ready: "
                        + ", ".join(f"{i['key']} ({i['status']})" for i in not_ready))
    return {
        "overview": f"{version.get('name')} contains {len(issues)} tickets from {workspace}.",
        "headline_themes": [{"name": k, "summary": "", "issue_keys": v}
                            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
        "excluded": [], "change_implementation": [], "db_changes": [],
        "readiness_warnings": warnings, "validation_plan": [], "post_deployment": [],
    }


def write_content(settings: Settings, region: str, workspace: str, version: dict,
                  issues: list[dict]) -> dict:
    if settings.openai_api_key:
        return write_with_openai(settings, region, workspace, version, issues)
    print("[ai] OPENAI_API_KEY not set - using deterministic grouping (no AI)")
    return write_without_ai(settings, region, workspace, version, issues)
