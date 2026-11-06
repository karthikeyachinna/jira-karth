#!/usr/bin/env bash
# Build and (re)start N local GoCD agents in Docker so the space pipelines run in parallel.
# Normally called by gocd/setup.py with values from gocd/gocd.yaml:
#   GOCD_URL           GoCD URL from this machine            (gocd.url)
#   AGENT_SERVER_URL   GoCD URL as seen from the agents      (gocd.agent_url)
#   AGENT_ENVIRONMENT  environment the agents auto-join      (gocd.environment)
#   gocd/agent/start.sh 3
set -euo pipefail
cd "$(dirname "$0")"
COUNT="${1:-3}"
GOCD="${GOCD_URL:-http://localhost:8153/go}"
AGENT_SERVER_URL="${AGENT_SERVER_URL:-http://host.docker.internal:8153/go}"
AGENT_ENVIRONMENT="${AGENT_ENVIRONMENT:-jira-release}"

RUNNING=$(docker ps -q --filter 'name=^jira-gocd-agent-' | wc -l)
if [ "$RUNNING" -eq "$COUNT" ] && [ -z "${FORCE:-}" ]; then
  echo "  $RUNNING agent container(s) already running - left as is (FORCE=1 to rebuild)"
  exit 0
fi

ENV_FILE="$(cd ../.. && pwd)/.env"
AUTH=()
if [ -f "$ENV_FILE" ]; then   # login on? use the service account from .env
  U=$(grep -E '^GOCD_USER=' "$ENV_FILE" | cut -d= -f2- || true)
  P=$(grep -E '^GOCD_PASSWORD=' "$ENV_FILE" | cut -d= -f2- || true)
  if [ -n "$U" ] && [ -n "$P" ]; then AUTH=(-u "$U:$P"); fi
fi
KEY=$(curl -fsS "${AUTH[@]}" -H 'Accept: application/xml' "$GOCD/api/admin/config.xml" \
      | grep -o 'agentAutoRegisterKey="[^"]*"' | cut -d'"' -f2 || true)
[ -n "$KEY" ] || echo "! could not read the auto-register key; enable agents in the GoCD UI"

docker build -q -t jira-gocd-agent:local . >/dev/null
docker ps -aq --filter 'name=^jira-gocd-agent' | xargs -r docker rm -f >/dev/null

for i in $(seq 1 "$COUNT"); do
  docker run -d --name "jira-gocd-agent-$i" --hostname "jira-agent-$i" --restart unless-stopped \
    --add-host=host.docker.internal:host-gateway \
    -e GO_SERVER_URL="$AGENT_SERVER_URL" \
    -e AGENT_AUTO_REGISTER_KEY="$KEY" \
    -e AGENT_AUTO_REGISTER_ENVIRONMENTS="$AGENT_ENVIRONMENT" \
    -e AGENT_AUTO_REGISTER_HOSTNAME="jira-agent-$i" \
    jira-gocd-agent:local >/dev/null
  echo "  started jira-gocd-agent-$i -> $AGENT_SERVER_URL ($AGENT_ENVIRONMENT)"
done
