#!/bin/sh
# Nightly encrypted off-box backup of everything that cannot be rebuilt:
# the Postgres database and the CV files on the data mount.
#
# ─────────────────────────────────────────────────────────────────────────
# UNVERIFIED. This script has never been executed against a real restic
# repository. It was written from the documented behaviour of pg_dump and
# restic; the operator must provision the remote, run it once by hand, and
# TEST A RESTORE before treating the data as safe. See docs/BACKUPS.md.
# ─────────────────────────────────────────────────────────────────────────
#
# Environment (set in .env, consumed via docker-compose.prod.yml):
#   RESTIC_REPOSITORY        where snapshots go (s3:/b2:/sftp:/rest:)
#   RESTIC_PASSWORD          encryption key — LOSING IT LOSES THE BACKUPS
#   PGHOST/PGUSER/PGDATABASE/PGPASSWORD
#   BACKUP_INTERVAL_SECONDS  default 86400
#   BACKUP_KEEP_DAILY        default 30
#   BACKUP_HEALTHCHECK_URL   optional: pinged on success (dead-man switch)

set -eu

INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-30}"
DUMP_DIR=/tmp/gosha-backup
DATA_DIR=/data

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

# docker-compose.prod.yml passes these through with a plain :- default rather
# than :?, because Compose interpolates every service at file-parse time —
# including profile-gated ones — so a :? there would abort `docker compose up`
# on any host that has no backup remote configured. The check therefore lives
# here, where it only runs when the backup service actually starts.
if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD:-}" ]; then
  log "FATAL: RESTIC_REPOSITORY and RESTIC_PASSWORD must both be set in .env."
  log "Refusing to run: an unconfigured backup that exits 0 is worse than no"
  log "backup at all, because it looks like it worked. See docs/BACKUPS.md."
  exit 1
fi

fail() {
  log "ERROR: $*"
  # A backup that fails quietly is worse than no backup, because it looks
  # like one. Surface it, then keep the loop alive so a transient network
  # problem does not disable backups until someone notices the container.
  return 1
}

init_repo_if_needed() {
  if restic snapshots --last >/dev/null 2>&1; then
    return 0
  fi
  log "Repository not initialised — running restic init"
  restic init
}

run_once() {
  rm -rf "$DUMP_DIR"
  mkdir -p "$DUMP_DIR"

  # -Fc is the custom format: compressed, and restorable selectively with
  # pg_restore. --no-owner/--no-privileges so a restore into a fresh
  # container with a differently-named role still works.
  log "Dumping database ${PGDATABASE} from ${PGHOST}"
  pg_dump -Fc --no-owner --no-privileges -f "$DUMP_DIR/gosha.dump" \
    || fail "pg_dump failed"

  # Sanity-check the dump before it is snapshotted. An empty or truncated
  # dump is the classic "we had backups" outcome.
  if [ ! -s "$DUMP_DIR/gosha.dump" ]; then
    fail "dump is empty — refusing to snapshot"
    return 1
  fi
  pg_restore --list "$DUMP_DIR/gosha.dump" >/dev/null \
    || fail "dump is unreadable by pg_restore — refusing to snapshot"

  size=$(wc -c < "$DUMP_DIR/gosha.dump")
  log "Dump OK (${size} bytes)"

  log "Snapshotting database + CV files"
  restic backup \
    --tag gosha \
    --host gosha-prod \
    "$DUMP_DIR/gosha.dump" \
    "$DATA_DIR" \
    --exclude "$DATA_DIR/*.heartbeat" \
    --exclude "$DATA_DIR/**/__pycache__" \
    || fail "restic backup failed"

  log "Pruning to the last ${KEEP_DAILY} daily snapshots"
  restic forget --tag gosha --keep-daily "$KEEP_DAILY" --prune \
    || log "WARNING: prune failed (snapshots are still safe, disk will grow)"

  # Verifying a random 5% of the data catches silent corruption in the
  # remote long before a restore does.
  restic check --read-data-subset=5% \
    || log "WARNING: restic check reported problems — investigate NOW"

  rm -rf "$DUMP_DIR"

  if [ -n "${BACKUP_HEALTHCHECK_URL:-}" ]; then
    wget -q -O /dev/null --timeout=10 "$BACKUP_HEALTHCHECK_URL" \
      || log "WARNING: could not ping healthcheck URL"
  fi

  log "Backup complete"
}

log "gosha backup loop starting (interval ${INTERVAL}s, keep ${KEEP_DAILY} daily)"
init_repo_if_needed || log "WARNING: repo init failed; will retry next cycle"

while true; do
  if run_once; then
    :
  else
    log "Backup cycle FAILED — will retry in ${INTERVAL}s"
  fi
  sleep "$INTERVAL"
done
