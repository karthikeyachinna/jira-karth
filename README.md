# jira-karth — Jira release notes pipeline (GoCD)

Pipeline-as-code for GoCD. Pick a Jira **workspace** (project) and **release** (fixVersion).
An AI agent (OpenAI) writes the release notes in the house style defined in `jira_release/ai.py`, renders them to a
PDF and, optionally, publishes them to Confluence.

```
jira-release-notes
├── stage connect-jira   → job discover-workspaces   (check token, list workspaces + unreleased releases)
└── stage release-notes  → job generate-release-notes (Jira tickets → OpenAI → HTML/PDF → Confluence)
```

## How to use it

1. **Run** the pipeline normally. Stage 1 lists every workspace and its releases (see the job's
   *workspaces* tab). Stage 2 does nothing because no workspace is selected yet.
2. **Trigger with options**, then set `WORKSPACE=EVO` and `RELEASE=release-26.14`. Stage 1
   checks your selection and stage 2 generates the notes. The output is in the job's
   *release-notes* tab and its artifacts (`.pdf`, `.html`, `.content.json`).

## One-time setup

```bash
cp .env.example .env               # fill in JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN, OPENAI_API_KEY …
gocd/agent/start.sh                # local GoCD agent (Docker) with python3 + Chrome
git push                           # GoCD reads .gocd/*.gocd.yaml from GitHub
python3 gocd/setup.py              # registers the config repo and creates the "jira-release" environment
```

`setup.py` stores `JIRA_TOKEN` and `OPENAI_API_KEY` as **secure** GoCD variables. `.env` and
`*.pdf` are git-ignored because this repo is public. Re-run `setup.py` whenever `.env` changes.

The **agent** needs `python3` (with venv), plus Chrome or Chromium for the PDF. If no `OPENAI_API_KEY` is set, the notes are built without AI and tickets are
grouped by epic or component.

## Run locally

```bash
WORKSPACE=EVO RELEASE=release-26.14 gocd/run.sh generate   # writes to output/
```
