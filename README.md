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
| `jira-connect` | Connects to Jira and lists the spaces in `JIRA_WORKSPACES`. Creates or updates one pipeline per space, and deletes pipelines for spaces that were removed. |
| `jira-space-<KEY>` | Picks the space's next unreleased fixVersion and its tickets, has OpenAI write the notes, then builds the HTML and PDF. Skips the space if it has no release or no tickets. |
| `jira-approvals` | Waits for all the space pipelines. When you click ▶, it publishes every space's notes to Confluence with the PDF attached. |

GoCD groups: **jira** → `jira-connect` · **jira-spaces** → `jira-space-*` · **jira-approval** → `jira-approvals`.

All pipelines are defined in Python (`jira_release/gocd.py`) and created through the GoCD API.

## Setup (once)

```bash
# .env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN, JIRA_WORKSPACES, OPENAI_API_KEY, CONFLUENCE_*
gocd/agent/start.sh 3          # 3 GoCD agents in Docker (python3 + Chrome) so spaces run in parallel
python3 gocd/setup.py          # secrets → GoCD (encrypted), agents → env, create all pipelines
```

Re-run `python3 gocd/setup.py` after changing `.env`. `.env` and `*.pdf` are git-ignored because
this repo is public.

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

## Login (GoCD users and roles)

```bash
.venv/bin/pip install bcrypt
.venv/bin/python gocd/auth/setup_auth.py   # turns login on; re-run after editing the accounts in .env
python3 gocd/setup.py                      # re-sync the pipelines with the approval gate
```

| `.env` key | Account |
|---|---|
| `GOCD_ADMIN_USER` / `GOCD_ADMIN_PASSWORD` | Human admin (GoCD system admin). Defaults to `karthikeya`. |
| `GOCD_USER` / `GOCD_PASSWORD` | Service account the pipelines use for API calls. Defaults to `gocd-bot`. |
| `GOCD_APPROVERS` | The `release-approvers` role: the only users who can approve **jira-approvals**. |
| `GOCD_USERS` | Extra users. Each one's password is `GOCD_PASSWORD_<NAME>`. |

If a password is missing from `.env`, it is generated and written back to `.env`; it is never printed.
GoCD only stores bcrypt hashes, in `/godata/config/passwords.properties` in the server container.
If you get locked out, `gocd/auth/disable_auth.sh` turns login off again.
