#!/usr/bin/env bash
# One-time SQLite -> Postgres migration, run on the production server.
#   I_MEAN_IT=1 bash scripts/vps_migrate.sh
# WARNING: wipes the Postgres tables first (RESTART IDENTITY CASCADE) —
# only meant for the initial cutover from an old SQLite install. Run it
# against a live database and every user, CV link and application is gone.
#
# set -e: without it a failed migration still printed counts and "looked"
# done, and a failed `cd` ran the TRUNCATE from wherever you stood.
set -euo pipefail

if [ "${I_MEAN_IT:-}" != "1" ]; then
  echo "This TRUNCATEs every table in the production database, then imports" >&2
  echo "data/jobs.db. It is only for the first cutover from SQLite." >&2
  echo "Back up first (docs/BACKUPS.md), then: I_MEAN_IT=1 bash $0" >&2
  exit 1
fi

cd ~/gosha
# -f2-, not -f2: a base64 password can contain '='.
PGPASS=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)
[ -n "$PGPASS" ] || { echo "POSTGRES_PASSWORD is empty in .env" >&2; exit 1; }
[ -f data/jobs.db ] || { echo "data/jobs.db not found: nothing to migrate" >&2; exit 1; }

echo "--- wiping destination tables ---"
docker exec gosha-postgres psql -v ON_ERROR_STOP=1 -U gosha -d gosha -c "TRUNCATE users, jobs, subscriptions, user_jobs, applications, cover_letters, events, outbox RESTART IDENTITY CASCADE;"
echo "--- migrating ---"
# Full output, no `| tail`: the error is usually in the part tail cut off.
docker compose -f docker-compose.prod.yml run --rm -e PYTHONPATH=/app bot \
  python scripts/migrate_sqlite_to_postgres.py \
  sqlite+aiosqlite:///data/jobs.db \
  "postgresql+asyncpg://gosha:${PGPASS}@postgres:5432/gosha"
echo "--- post-migration counts ---"
docker exec gosha-postgres psql -v ON_ERROR_STOP=1 -U gosha -d gosha -c "select (select count(*) from users) as users, (select count(*) from subscriptions) as subs, (select count(*) from jobs) as jobs, (select count(*) from user_jobs) as deliveries, (select count(*) from applications) as apps, (select count(*) from cover_letters) as letters;"
