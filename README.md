# jira-karth — Jira release notes on GoCD

One GoCD pipeline per Jira space. Each one writes that space's release notes with AI (OpenAI),
renders them to HTML and PDF, and after a manual approval publishes them to Confluence.

```
                    ┌─▶ jira-space-DEV   ─┐
                    ├─▶ jira-space-QS    ─┤
   jira-connect ────┼─▶ jira-space-SCR   ─┼──▶ jira-approvals (manual) ──▶ Confluence
                    ├─▶ jira-space-SCRUM ─┤
                    └─▶ jira-space-SUP   ─┘
```

| Pipeline | What it does |
|---|---|
| `jira-connect` | Connects to Jira and lists the spaces switched on with `JIRA_WORKSPACES_<KEY>=YES` (e.g. `JIRA_WORKSPACES_DEV=YES`, `JIRA_WORKSPACES_SCRUM=NONE`). Creates or updates one pipeline per space, and deletes pipelines for spaces that were removed. |
| `jira-space-<KEY>` | Picks the space's next unreleased fixVersion and its tickets, has OpenAI write the notes, then builds the HTML and PDF. Skips the space if it has no release or no tickets. |
| `jira-approvals` | Waits for all the space pipelines. When you click ▶, it publishes every space's notes to Confluence with the PDF attached. |

GoCD groups: **jira** → `jira-connect` · **jira-spaces** → `jira-space-*` · **jira-approval** → `jira-approvals`.

All pipelines are defined in Python (`jira_release/gocd.py`) and created through the GoCD API.

## Setup on a GoCD server (`gocd/gocd.yaml`)

`gocd/gocd.yaml` holds everything that differs between GoCD servers: server URL, agent URL,
environment and group names, agents, repo, Jira site and spaces, release, region, AI model,
Confluence, and login. `.env` holds **secrets only**.

```bash
# .env (git-ignored)
JIRA_TOKEN=...
OPENAI_API_KEY=...
# GOCD_USER / GOCD_PASSWORD / GOCD_PASSWORD_<LOGIN> are generated on the first run

gocd/bootstrap.sh                               # configure the server in gocd/gocd.yaml
GOCD_CONFIG=gocd/prod.yaml gocd/bootstrap.sh    # another GoCD domain, from its own YAML
```

`bootstrap.sh` sets up Python and is safe to re-run. It:

1. turns login on (`gocd/access.yaml`)
2. sets the environment variables (secrets are stored encrypted)
3. starts the agents and adds them to the environment
4. creates every pipeline

For a GoCD server on another machine:

- set `gocd.url` and `gocd.agent_url` to its address
- set `agents.docker: false` if it already has agents
- set `gocd.server_container: none`, then copy the generated `output/passwords.properties` to
  the server

## Run

In GoCD, click ▶ on **jira-connect**. The space pipelines start on their own. Each one has a
**release-notes** tab and a PDF under its artifacts. Then click ▶ on **jira-approvals**
to publish to Confluence.

## Local commands

```bash
gocd/run.sh check                       # Jira login works?
gocd/run.sh discover                    # spaces + the release each will build
WORKSPACE=SCRUM gocd/run.sh generate    # notes for one space -> output/SCRUM/
```

## Users and access (`gocd/access.yaml`)

`gocd/access.yaml` defines GoCD access: users (**Admin → Users**), system admins, roles,
pipeline group permissions, and who may approve **jira-approvals**. Edit it, then run:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python gocd/auth/setup_auth.py --dry-run   # preview
.venv/bin/python gocd/auth/setup_auth.py             # apply (first run turns login on)
python3 gocd/setup.py                                # re-sync pipelines with the approval roles
```

- **Passwords:** never in the YAML. Each user's password is in `.env` as
  `GOCD_PASSWORD_<LOGIN>`; the service account's is `GOCD_USER` / `GOCD_PASSWORD`. A missing
  password is generated into `.env` and never printed.
- **What GoCD stores:** only bcrypt hashes.
- **The YAML is the source of truth:** a user missing from it is disabled, and a role missing
  from it is removed.
- **Locked out?** `gocd/auth/disable_auth.sh` turns login off again.
