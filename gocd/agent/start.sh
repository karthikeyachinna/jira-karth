#!/usr/bin/env bash
# Build and (re)start a local GoCD agent container connected to the GoCD server on this host.
set -euo pipefail
cd "$(dirname "$0")"
docker build -t jira-gocd-agent:local .
docker rm -f jira-gocd-agent >/dev/null 2>&1 || true
docker run -d --name jira-gocd-agent --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -e GO_SERVER_URL=http://host.docker.internal:8153/go \
  jira-gocd-agent:local
echo "Agent started. It appears under Agents in GoCD; run 'python3 gocd/setup.py' to enable it."
