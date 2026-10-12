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
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-180}"
# Once the API is healthy, the bot must stay up this long without Docker
# restarting it (see bot_stays_up).
BOT_SETTLE="${DEPLOY_BOT_SETTLE:-60}"
# Container logs of a failed deploy are written here, on the VPS. This
# script's output is the Actions log of a PUBLIC repo, and the API's log
# is an access log: visitor IP addresses.
LOG_DIR="${DEPLOY_LOG_DIR:-$HOME/.local/state/gosha-deploy}"
# The bot and api run as this uid (Dockerfile: useradd --uid 1000 app).
APP_UID=1000

if [ ! -f .env ]; then
  echo "ERROR: .env missing — cp .env.example .env and fill in secrets first." >&2
  exit 1
fi

# Both services refuse to start without it (gosha/cv_crypto.py). Failing
# here beats a build, a failed health gate and a rollback.
if ! grep -Eq '^CV_ENCRYPTION_KEY=.+' .env; then
  echo "ERROR: CV_ENCRYPTION_KEY is not set in .env — see .env.example." >&2
  echo '  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"' >&2
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

bot_state() {
  # "<status> <restart count>" of the bot container, e.g. "running 0".
  local id
  id="$($COMPOSE ps -a -q bot 2>/dev/null)" || return 1
  [ -n "$id" ] || return 1
  docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$id" 2>/dev/null
}

# The API gate says nothing about the bot. A bot that dies on boot (bad
# import, bad config, revoked token) is restarted by Docker forever, and
# the deploy used to report healthy regardless. Its own healthcheck cannot
# gate here: start_period is 300s (first boot downloads the model). So it
# must stay `running`, with an unchanged RestartCount, for BOT_SETTLE
# seconds; Docker's restart backoff never waits that long without showing
# `restarting` or bumping the count.
bot_stays_up() {
  local state status first count deadline=$((SECONDS + BOT_SETTLE))
  state="$(bot_state)" || { echo "[deploy] no bot container" >&2; return 1; }
  read -r status first <<<"$state"
  while :; do
    state="$(bot_state)" || { echo "[deploy] the bot container disappeared" >&2; return 1; }
    read -r status count <<<"$state"
    if [ "$status" != running ] || [ "$count" != "$first" ]; then
      echo "[deploy] bot is ${status} with ${count} restart(s), ${first} when the API came up — crash loop" >&2
      return 1
    fi
    [ $SECONDS -lt $deadline ] || return 0
    sleep 5
  done
}

stack_healthy() {
  log "waiting up to ${HEALTH_TIMEOUT}s for the API to report healthy"
  if ! wait_for_health; then
    echo "[deploy] API not healthy after ${HEALTH_TIMEOUT}s" >&2
    return 1
  fi
  log "API healthy; the bot must now stay up for ${BOT_SETTLE}s"
  bot_stays_up
}

save_failure_logs() {
  # Only the path is printed (see LOG_DIR). Read it on the VPS.
  local file
  mkdir -p "$LOG_DIR" && chmod 700 "$LOG_DIR" || return 0
  file="$LOG_DIR/deploy-$(date -u +%Y%m%dT%H%M%SZ)-${NEW_COMMIT:0:12}.log"
  ( umask 077
    { $COMPOSE ps -a; $COMPOSE logs --no-color --tail=300 api bot; } >"$file" 2>&1
  ) || true
  echo "[deploy] container logs (api, bot) saved on the VPS: ${file/#$HOME/\~}" >&2
}

# Bind-mount sources. Docker creates a missing one as root:root, and the
# app runs as uid $APP_UID, so on a fresh host the bot could not write a
# CV, its heartbeat or a pinned SSH host key into ./data. Only directories
# created here are chowned: an existing ./data is production state.
prepare_bind_mounts() {
  local dir
  for dir in data keys; do
    [ -d "$dir" ] && continue
    mkdir -p "$dir"
    [ "$(stat -c %u "$dir")" = "$APP_UID" ] && continue
    if chown "$APP_UID:$APP_UID" "$dir" 2>/dev/null \
        || sudo -n chown "$APP_UID:$APP_UID" "$dir" 2>/dev/null; then
      log "created ./$dir, owned by uid $APP_UID"
    else
      echo "[deploy] WARNING: created ./$dir as uid $(id -u), but the containers run as uid $APP_UID." >&2
      echo "[deploy]   Fix once, in this checkout: sudo chown $APP_UID:$APP_UID $dir" >&2
    fi
  done
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

prepare_bind_mounts

# --pull: without it the cached python/node/caddy base layers, and the
# postgres image, were reused forever and their security fixes never
# shipped. Pull or build failure (a registry hiccup included) stops here,
# before anything restarts: the running containers keep serving the
# previous commit, and the checkout goes back to match them.
log "pulling base images and building"
if ! $COMPOSE build --pull || ! $COMPOSE pull -q postgres; then
  echo "[deploy] BUILD OR PULL FAILED — nothing was restarted; still serving ${PREVIOUS_COMMIT}." >&2
  git reset --hard "$PREVIOUS_COMMIT" >/dev/null
  exit 1
fi

log "restarting"
# A failed `up` goes through the health gate and rollback below rather
# than exiting here with the stack half-replaced.
UP_OK=1
$COMPOSE up -d || UP_OK=0

if [ "$UP_OK" = 1 ] && stack_healthy; then
  log "healthy at ${NEW_COMMIT}"
  docker image prune -f
  echo
  $COMPOSE ps
  log "Deployed. Logs: $COMPOSE logs -f --tail=50"
  exit 0
fi

echo >&2
echo "[deploy] DEPLOY UNHEALTHY at ${NEW_COMMIT} — rolling back" >&2
save_failure_logs

if [ "$PREVIOUS_COMMIT" = "$NEW_COMMIT" ]; then
  echo "[deploy] nothing to roll back to (already on the failing commit)." >&2
  echo "[deploy] the stack is DOWN or unhealthy — investigate now." >&2
  exit 1
fi

git reset --hard "$PREVIOUS_COMMIT"
$COMPOSE up -d --build

if stack_healthy; then
  echo "[deploy] rolled back to ${PREVIOUS_COMMIT} and healthy." >&2
else
  echo "[deploy] ROLLBACK ALSO UNHEALTHY — the stack needs a human." >&2
  save_failure_logs
fi
exit 1
