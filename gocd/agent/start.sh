#!/usr/bin/env bash
# Build and (re)start N local GoCD agents (default 3) so the per-space jobs run in parallel.
#   gocd/agent/start.sh        # 3 agents
#   gocd/agent/start.sh 5      # 5 agents (one per Jira space)
# Agents auto-register into the "jira-release" environment using the server's auto-register key.
set -euo pipefail
cd "$(dirname "$0")"
COUNT="${1:-3}"
GOCD="${GOCD_URL:-http://localhost:8153/go}"

ENV_FILE="$(cd .. && cd .. && pwd)/.env"
AUTH=()
if [ -f "$ENV_FILE" ]; then   # login on? use the service account from .env
  U=$(grep -E '^GOCD_USER=' "$ENV_FILE" | cut -d= -f2-); P=$(grep -E '^GOCD_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
  [ -n "$U" ] && [ -n "$P" ] && AUTH=(-u "$U:$P")
fi
KEY=$(curl -fsS "${AUTH[@]}" -H 'Accept: application/xml' "$GOCD/api/admin/config.xml" \
      | grep -o 'agentAutoRegisterKey="[^"]*"' | cut -d'"' -f2 || true)
[ -n "$KEY" ] || echo "! could not read the auto-register key; enable agents in the GoCD UI"

docker build -q -t jira-gocd-agent:local . >/dev/null
docker ps -aq --filter 'name=^jira-gocd-agent' | xargs -r docker rm -f >/dev/null

for i in $(seq 1 "$COUNT"); do
  docker run -d --name "jira-gocd-agent-$i" --hostname "jira-agent-$i" --restart unless-stopped \
    --add-host=host.docker.internal:host-gateway \
    -e GO_SERVER_URL=http://host.docker.internal:8153/go \
    -e AGENT_AUTO_REGISTER_KEY="$KEY" \
    -e AGENT_AUTO_REGISTER_ENVIRONMENTS=jira-release \
    -e AGENT_AUTO_REGISTER_HOSTNAME="jira-agent-$i" \
    jira-gocd-agent:local >/dev/null
  echo "started jira-gocd-agent-$i"
done
