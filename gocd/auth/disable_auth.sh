#!/usr/bin/env bash
# Recovery: turn GoCD login off by removing the <authConfigs> block from the server config.
# Use only if you are locked out. Re-enable with: .venv/bin/python gocd/auth/setup_auth.py
set -euo pipefail
C="${GOCD_SERVER_CONTAINER:-$(docker ps --format '{{.Names}} {{.Image}}' | awk '/gocd\/gocd-server/ {print $1; exit}')}"
[ -n "$C" ] || { echo "GoCD server container not found"; exit 1; }
F=/godata/config/cruise-config.xml
docker exec "$C" cp "$F" "$F.before-disable-auth"
docker exec "$C" sed -i -e '/<authConfigs>/,/<\/authConfigs>/d' "$F"
echo "Removed <authConfigs> from $C:$F (backup: $F.before-disable-auth)."
echo "GoCD reloads the config within a few seconds; login is then off."
