#!/bin/sh
# Prove the client IP the API sees is real and cannot be forged.
#
# Runs the repo's Caddyfile (and deploy/host-caddy.snippet) in throwaway
# caddy:2-alpine containers in front of an echo server standing in for the
# API, and checks the X-Forwarded-For that reaches it:
#
#   standalone (Caddy faces Cloudflare)
#     1. a non-Cloudflare peer forging CF-Connecting-IP / X-Forwarded-For
#        is named by its own address;
#     2. a trusted peer's CF-Connecting-IP is believed (the test network
#        stands in for Cloudflare via CADDY_EXTRA_TRUSTED_PROXIES).
#   behind the host proxy (production layout)
#     3. via "Cloudflare", the client from CF-Connecting-IP reaches the API;
#     4. straight to the host proxy from elsewhere, forged headers are
#        dropped and the peer's own address reaches the API.
#
#   sh scripts/check_client_ip.sh        # needs docker; CI runs it
#
# Layout mirrors production: the visitor and the host proxy talk over a
# non-private "edge" network (198.18.0.0/24, RFC 2544 benchmarking space),
# the host proxy reaches the container Caddy over a private docker network,
# as it does via 127.0.0.1:8090. Everything is named gosha-ipcheck* and
# removed on exit.

set -eu
cd "$(dirname "$0")/.."

NAME=gosha-ipcheck
IMAGE=caddy:2-alpine
FORGED=203.0.113.66
REAL=198.51.100.7
WORK="$(mktemp -d)"
failures=0

cleanup() {
  docker rm -f "$NAME-api" "$NAME-caddy" "$NAME-front" "$NAME-client" "$NAME-visitor" \
    >/dev/null 2>&1 || true
  docker network rm "$NAME" "$NAME-edge" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

log() { echo "[ip-check] $*"; }

expect() {  # $1 = description, $2 = expected, $3 = actual
  if [ "$2" = "$3" ]; then
    log "ok    $1: $3"
  else
    log "FAIL  $1: expected '$2', got '$3'"
    failures=$((failures + 1))
  fi
}

docker network create "$NAME" >/dev/null
docker network create --subnet "${IPCHECK_EDGE_SUBNET:-198.18.0.0/24}" "$NAME-edge" >/dev/null
SUBNET="$(docker network inspect "$NAME" --format '{{(index .IPAM.Config 0).Subnet}}')"
EDGE="$(docker network inspect "$NAME-edge" --format '{{(index .IPAM.Config 0).Subnet}}')"

# The "API": answers with the X-Forwarded-For it received.
printf ':8000 {\n\trespond "{header.X-Forwarded-For}"\n}\n' > "$WORK/echo.Caddyfile"
docker run -d --name "$NAME-api" --network "$NAME" --network-alias api \
  -v "$WORK/echo.Caddyfile:/etc/caddy/Caddyfile:ro" "$IMAGE" >/dev/null

# Long-lived containers to send requests from: one on the private network,
# one (the "visitor") on the edge network only.
docker run -d --name "$NAME-client" --network "$NAME" "$IMAGE" sleep 600 >/dev/null
docker run -d --name "$NAME-visitor" --network "$NAME-edge" "$IMAGE" sleep 600 >/dev/null
ip_of() { docker inspect "$1" --format "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}"; }
CLIENT_IP="$(ip_of "$NAME-client" "$NAME")"
VISITOR_IP="$(ip_of "$NAME-visitor" "$NAME-edge")"

start_caddy() {  # extra -e args for the repo's Caddyfile
  docker rm -f "$NAME-caddy" >/dev/null 2>&1 || true
  docker run -d --name "$NAME-caddy" --network "$NAME" -e CADDY_SITE=:80 "$@" \
    -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" "$IMAGE" >/dev/null
  wait_for "$NAME-client" "$NAME-caddy"
}

wait_for() {  # $1 = client container, $2 = host
  tries=0
  until docker exec "$1" wget -q -O /dev/null "http://$2/api/ping" 2>/dev/null \
      || [ "$tries" -ge 30 ]; do
    tries=$((tries + 1)); sleep 1
  done
}

get() {  # $1 = client container, $2 = host, rest = extra headers
  from="$1"; host="$2"; shift 2
  docker exec "$from" wget -q -O - "$@" "http://$host/api/ping"
}

docker run --rm -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" -e CADDY_SITE=:80 \
  "$IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
  && log "ok    Caddyfile validates" || { log "FAIL  Caddyfile does not validate"; exit 1; }

log "standalone, default trust (Cloudflare ranges only)"
start_caddy
expect "forged CF-Connecting-IP ignored" "$CLIENT_IP" \
  "$(get "$NAME-client" "$NAME-caddy" --header "CF-Connecting-IP: $FORGED" --header "X-Forwarded-For: $FORGED")"

log "standalone, peer trusted (stands in for Cloudflare)"
start_caddy -e CADDY_EXTRA_TRUSTED_PROXIES="$SUBNET"
expect "CF-Connecting-IP believed from a trusted peer" "$REAL" \
  "$(get "$NAME-client" "$NAME-caddy" --header "CF-Connecting-IP: $REAL")"

log "production layout: host proxy -> container Caddy -> API"
start_caddy -e CADDY_EXTRA_TRUSTED_PROXIES=private_ranges -e CADDY_CLIENT_IP_HEADER=X-Forwarded-For
front() {  # $1 = ranges the front proxy treats as Cloudflare
  docker rm -f "$NAME-front" >/dev/null 2>&1 || true
  docker run -d --name "$NAME-front" --network "$NAME-edge" \
    -e GOSHA_HOST=:80 -e GOSHA_UPSTREAM="$NAME-caddy:80" -e CLOUDFLARE_RANGES="$1" \
    -v "$PWD/deploy/host-caddy.snippet:/etc/caddy/Caddyfile:ro" "$IMAGE" >/dev/null
  docker network connect "$NAME" "$NAME-front"
  FRONT_IP="$(ip_of "$NAME-front" "$NAME-edge")"
  wait_for "$NAME-visitor" "$FRONT_IP"
}
front "$EDGE"   # the edge network plays Cloudflare
expect "via Cloudflare: the visitor reaches the API" "$REAL" \
  "$(get "$NAME-visitor" "$FRONT_IP" --header "CF-Connecting-IP: $REAL")"
front "192.0.2.1/32"   # nothing on the edge is Cloudflare now
expect "direct to origin: forged headers dropped" "$VISITOR_IP" \
  "$(get "$NAME-visitor" "$FRONT_IP" --header "CF-Connecting-IP: $FORGED" --header "X-Forwarded-For: $FORGED")"

[ "$failures" -eq 0 ] || { log "$failures check(s) failed"; exit 1; }
log "CLIENT IP CHECK PASSED"
