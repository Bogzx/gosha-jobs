#!/bin/sh
# End-to-end backup drill on throwaway containers.
#
# Seeds a scratch Postgres (the app's real schema, via scripts/seed_dev.py)
# and an encrypted CV directory, backs both up with the production backup
# image (Dockerfile.backup + run-backup.sh) into a local restic repository,
# restores with scripts/backup/restore-drill.sh, requires every row count
# to match, and decrypts the restored CVs (scripts/backup/verify_cvs.py).
# Then it breaks the backup on purpose and requires the cycle to fail.
#
#   sh scripts/backup/drill.sh          # needs docker + the Python deps
#
# Safe next to a running deployment: every container, network and image
# is named gosha-backup-drill*, data lives in a temp dir, and the scratch
# Postgres is published on 127.0.0.1 only. CI runs this on every change to
# the backup code (.github/workflows/backup-drill.yml).

set -eu
cd "$(dirname "$0")/../.."

NAME=gosha-backup-drill
IMAGE="$NAME:latest"
PYTHON="${PYTHON:-python3}"
PORT="${DRILL_PG_PORT:-$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
WORK="$(mktemp -d)"
RESTIC_PW=drill-restic-password

log() { echo "[drill] $*"; }

cleanup() {
  docker rm -f "$NAME-pg" >/dev/null 2>&1 || true
  docker network rm "$NAME" >/dev/null 2>&1 || true
  # Restored files are root-owned (written inside the container).
  docker run --rm --entrypoint rm -v "$WORK:/w" "$IMAGE" -rf /w/repo /w/out \
    >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

backup_container() {  # extra `docker run` args, then optional command
  docker run --rm --network "$NAME" \
    -e RESTIC_REPOSITORY=/repo -e RESTIC_PASSWORD="$RESTIC_PW" \
    -e PGHOST="$NAME-pg" -e PGUSER=gosha -e PGDATABASE=gosha -e PGPASSWORD=drill \
    -v "$WORK/repo:/repo" "$@"
}

log "building the backup image"
docker build -q -f Dockerfile.backup -t "$IMAGE" . >/dev/null

log "starting a scratch Postgres on 127.0.0.1:$PORT"
docker network create "$NAME" >/dev/null
docker run -d --name "$NAME-pg" --network "$NAME" -p "127.0.0.1:$PORT:5432" \
  -e POSTGRES_USER=gosha -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=gosha \
  postgres:16-alpine >/dev/null
tries=0
# TCP, not the socket: the image's init-time server listens on the socket only.
until docker exec "$NAME-pg" pg_isready -h 127.0.0.1 -U gosha -d gosha >/dev/null 2>&1; do
  tries=$((tries + 1)); [ "$tries" -lt 60 ] || { log "Postgres did not start"; exit 1; }
  sleep 1
done

log "seeding the app schema, demo rows and encrypted CVs"
KEY="$($PYTHON -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
mkdir -p "$WORK/data/cvs" "$WORK/repo" "$WORK/out"
CV_ENCRYPTION_KEY="$KEY" "$PYTHON" - "postgresql+asyncpg://gosha:drill@127.0.0.1:$PORT/gosha" \
  "$WORK/data/cvs" <<'PY'
import asyncio
import sys
from pathlib import Path

sys.path[:0] = [".", "scripts"]
import gosha.cover_letter as cover_letter

# Point CV storage at the drill's temp dir: never the checkout's data/.
cover_letter.CV_DIR = Path(sys.argv[2])
import seed_dev

asyncio.run(seed_dev.main())                       # reads sys.argv[1]
cover_letter.save_cv(999, "Second CV, so the drill restores more than one.")
PY

log "backup: one cycle with the production script"
backup_container -e BACKUP_ONCE=1 -v "$WORK/data:/data:ro" "$IMAGE"

log "restore drill (row counts must match exactly)"
backup_container -e DRILL_EXPECT_EXACT=1 -v "$WORK/data:/data:ro" -v "$WORK/out:/restore-out" \
  --entrypoint /bin/sh "$IMAGE" /usr/local/bin/restore-drill.sh

log "restored CVs must decrypt with the key"
CV_ENCRYPTION_KEY="$KEY" "$PYTHON" scripts/backup/verify_cvs.py "$WORK/out/data/cvs"

expect_failed_cycle() {  # $1 = what is broken; rest = extra docker run args
  what="$1"; shift
  if backup_container -e BACKUP_ONCE=1 "$@" "$IMAGE" > "$WORK/broken.log" 2>&1; then
    cat "$WORK/broken.log"; log "FAILED: $what, yet the cycle exited 0"; exit 1
  fi
  if grep -q "Backup complete" "$WORK/broken.log"; then
    cat "$WORK/broken.log"; log "FAILED: $what, yet it logged 'Backup complete'"; exit 1
  fi
  log "  $what -> $(grep -m1 ERROR "$WORK/broken.log" | sed 's/^\[[^]]*\] //')"
}

log "broken backups must fail, not report success"
expect_failed_cycle "no /data mount"
# A restic that fails only on `backup`, ahead of the real one on PATH.
printf '#!/bin/sh\n[ "$1" = backup ] && { echo "simulated restic backup failure" >&2; exit 1; }\nexec /usr/bin/restic "$@"\n' \
  > "$WORK/restic-shim" && chmod +x "$WORK/restic-shim"
expect_failed_cycle "restic backup fails" -v "$WORK/data:/data:ro" \
  -v "$WORK/restic-shim:/usr/local/bin/restic:ro"

log "BACKUP DRILL PASSED"
