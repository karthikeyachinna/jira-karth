#!/usr/bin/env bash
# One command to configure a GoCD server for the Jira release-notes flow.
#   gocd/bootstrap.sh                                 # settings from gocd/gocd.yaml
#   GOCD_CONFIG=gocd/prod.yaml gocd/bootstrap.sh      # another GoCD domain
# Needs: python3 (+venv), docker (for local agents / login file), secrets in .env:
#   JIRA_TOKEN=...  OPENAI_API_KEY=...   (GoCD passwords are generated into .env)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python -u gocd/setup.py "$@"
