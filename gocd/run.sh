#!/usr/bin/env bash
# Entry point for GoCD tasks: creates a venv (cached in the agent's working dir) and runs the CLI.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python -m jira_release "$@"
