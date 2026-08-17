#!/usr/bin/env bash
# Deploy/update the GOSHA platform on the VPS.
#   ./deploy.sh          # pull latest + rebuild + restart + verify
#
# A deploy that finishes is not a deploy that worked. This script now
# records the commit it started from, health-gates the result, and rolls
# back to that commit if the stack does not come up — previously a
# crash-looping image simply stayed deployed until a user complained.
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.prod.yml"
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/api/v1/health}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-180}"

if [ ! -f .env ]; then
  echo "ERROR: .env missing — cp .env.example .env and fill in secrets first." >&2
  exit 1
fi

log() { echo "[deploy] $*"; }

health_ok() {
  # Probe from inside the api container: no curl on the host required, and
  # it tests the same path the container healthcheck uses.
  $COMPOSE exec -T api python scripts/healthcheck.py api >/dev/null 2>&1
}

wait_for_health() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    if health_ok; then
      return 0
    fi
    sleep 5
  done
  return 1
}

# reset, not pull: a deploy clone tracks origin/main exactly, even across
# history rewrites or force pushes
git fetch origin main
PREVIOUS_COMMIT="$(git rev-parse HEAD)"
log "current commit ${PREVIOUS_COMMIT}"

git reset --hard origin/main
NEW_COMMIT="$(git rev-parse HEAD)"

if [ "$PREVIOUS_COMMIT" = "$NEW_COMMIT" ]; then
  log "already at origin/main (${NEW_COMMIT}) — rebuilding anyway"
fi

log "building and restarting"
$COMPOSE up -d --build

log "waiting up to ${HEALTH_TIMEOUT}s for the API to report healthy"
if wait_for_health; then
  log "healthy at ${NEW_COMMIT}"
  docker image prune -f
  echo
  $COMPOSE ps
  log "Deployed. Logs: $COMPOSE logs -f --tail=50"
  exit 0
fi

echo >&2
echo "[deploy] HEALTH CHECK FAILED after ${HEALTH_TIMEOUT}s — rolling back" >&2
$COMPOSE logs --tail=80 api >&2 || true

if [ "$PREVIOUS_COMMIT" = "$NEW_COMMIT" ]; then
  echo "[deploy] nothing to roll back to (already on the failing commit)." >&2
  echo "[deploy] the stack is DOWN or unhealthy — investigate now." >&2
  exit 1
fi

git reset --hard "$PREVIOUS_COMMIT"
$COMPOSE up -d --build

if wait_for_health; then
  echo "[deploy] rolled back to ${PREVIOUS_COMMIT} and healthy." >&2
else
  echo "[deploy] ROLLBACK ALSO UNHEALTHY — the stack needs a human." >&2
fi
exit 1
