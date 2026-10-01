#!/bin/sh
# Restore drill: prove the newest snapshot can actually be restored.
#
# Runs inside the backup image (postgres:16 + restic), entrypoint
# overridden, with the same environment as the backup service:
#
#   docker compose -f docker-compose.prod.yml --profile backup run --rm \
#     --entrypoint /bin/sh backup /usr/local/bin/restore-drill.sh
#
# 1. restic restores the newest `gosha` snapshot into a scratch directory
#    inside this container;
# 2. a throwaway Postgres server, also inside this container (own data
#    directory, Unix socket only), loads the dump with pg_restore;
# 3. every table's row count is printed next to the live database's
#    (PGHOST etc., read-only queries), and the CV files are counted.
#
# Nothing outside this container is written, except an optional copy of
# the restored data/ directory to /restore-out when that is mounted (for
# scripts/backup/verify_cvs.py, which checks the CVs decrypt). Exit 0 and
# "RESTORE DRILL PASSED" means: the snapshot exists, the dump loads, no
# table that has rows live came back empty, and CV files came back.
#
# DRILL_EXPECT_EXACT=1 additionally requires restored == live counts; the
# end-to-end test (scripts/backup/drill.sh) sets it, because nothing writes
# between its backup and its restore. Production keeps writing, so there
# restored <= live is normal.

set -eu

WORK=/tmp/restore-drill
PGWORK=/tmp/restore-drill-pg     # scratch server; kept apart so the restored
SOCKET_DIR=$PGWORK/socket        # files keep their original owner and mode
SCRATCH_DB=gosha_restore

log() {
  echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

die() {
  log "RESTORE DRILL FAILED: $*"
  exit 1
}

[ -n "${RESTIC_REPOSITORY:-}" ] && [ -n "${RESTIC_PASSWORD:-}" ] \
  || die "RESTIC_REPOSITORY and RESTIC_PASSWORD must be set"

rm -rf "$WORK" "$PGWORK"
mkdir -p "$WORK" "$SOCKET_DIR"

log "Newest snapshot:"
restic snapshots --tag gosha --latest 1 || die "cannot list snapshots"

log "Restoring it into $WORK"
restic restore latest --tag gosha --target "$WORK" || die "restic restore failed"

DUMP="$WORK/tmp/gosha-backup/gosha.dump"
[ -s "$DUMP" ] || die "snapshot holds no database dump at /tmp/gosha-backup/gosha.dump"

# A scratch server owned by the image's postgres user (initdb refuses to
# run as root), listening on a private socket only.
chown -R postgres:postgres "$PGWORK"
su-exec postgres initdb -D "$PGWORK/pgdata" -U drill --auth=trust --no-locale -E UTF8 \
  >/dev/null || die "initdb failed"
su-exec postgres pg_ctl -D "$PGWORK/pgdata" -l "$PGWORK/pg.log" -w \
  -o "-c listen_addresses='' -k $SOCKET_DIR" start >/dev/null \
  || die "scratch Postgres did not start (see $PGWORK/pg.log)"
trap 'su-exec postgres pg_ctl -D "$PGWORK/pgdata" -m fast stop >/dev/null 2>&1 || true' EXIT

scratch() {
  psql -h "$SOCKET_DIR" -U drill -v ON_ERROR_STOP=1 -At "$@"
}

scratch -d postgres -c "CREATE DATABASE $SCRATCH_DB" >/dev/null
log "Loading the dump into the scratch server"
pg_restore -h "$SOCKET_DIR" -U drill -d "$SCRATCH_DB" --no-owner --no-privileges \
  --exit-on-error "$DUMP" || die "pg_restore failed"

TABLES_SQL="SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name"

count_rows() {  # $1 = table; remaining args select the server
  table="$1"; shift
  psql -v ON_ERROR_STOP=1 -At "$@" -c "SELECT count(*) FROM public.\"$table\""
}

live_available=1
psql -At -c "SELECT 1" >/dev/null 2>&1 || live_available=0
[ "$live_available" = 1 ] || log "Live database not reachable: printing restored counts only"

problems=0
printf '%-28s %12s %12s\n' "table" "live" "restored"
for table in $(scratch -d "$SCRATCH_DB" -c "$TABLES_SQL"); do
  restored=$(count_rows "$table" -h "$SOCKET_DIR" -U drill -d "$SCRATCH_DB")
  live="-"
  if [ "$live_available" = 1 ]; then
    live=$(count_rows "$table" 2>/dev/null || echo "missing")
  fi
  printf '%-28s %12s %12s\n' "$table" "$live" "$restored"
  case "$live" in
    -|missing) ;;
    *)
      if [ "$live" -gt 0 ] && [ "$restored" -eq 0 ]; then
        log "PROBLEM: $table has $live rows live but none in the backup"
        problems=$((problems + 1))
      elif [ "${DRILL_EXPECT_EXACT:-0}" = "1" ] && [ "$live" -ne "$restored" ]; then
        log "PROBLEM: $table: live $live != restored $restored"
        problems=$((problems + 1))
      fi
      ;;
  esac
done

if [ "$live_available" = 1 ]; then
  for table in $(psql -At -c "$TABLES_SQL"); do
    if [ -z "$(scratch -d "$SCRATCH_DB" -c "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='$table'")" ]; then
      log "PROBLEM: live table $table is missing from the backup"
      problems=$((problems + 1))
    fi
  done
fi

cv_files=$(find "$WORK/data/cvs" -type f -name '*.enc' 2>/dev/null | wc -l | tr -d ' ')
log "CV files restored: $cv_files (encrypted; scripts/backup/verify_cvs.py checks they decrypt)"
if [ -d /data/cvs ]; then
  cv_live=$(find /data/cvs -type f -name '*.enc' | wc -l | tr -d ' ')
  log "CV files live: $cv_live"
  if [ "$cv_live" -gt 0 ] && [ "$cv_files" -eq 0 ]; then
    log "PROBLEM: CV files exist live but none came back"
    problems=$((problems + 1))
  fi
fi

if [ -d /restore-out ]; then
  rm -rf /restore-out/data
  cp -a "$WORK/data" /restore-out/data
  log "Copied the restored data/ directory to /restore-out/data"
fi

[ "$problems" -eq 0 ] || die "$problems problem(s) above"
rm -rf "$WORK/tmp"  # the dump; the scratch server goes with the container
log "RESTORE DRILL PASSED"
