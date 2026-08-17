# Backups

> ## ⚠️ THIS IS UNVERIFIED
>
> Everything in this document was written without access to the VPS. The
> `backup` service has **never been built, never been run, and never
> restored from**. Nobody has confirmed that a single byte of GOSHA data
> exists anywhere except on one server.
>
> Until you have completed [Step 6 — test a restore](#step-6--test-a-restore),
> assume there are **no backups**. That is currently the true state of the
> production deployment.

## Why this is the most important item in the repo

The production stack keeps two things that cannot be regenerated:

| What | Where | If it is lost |
|---|---|---|
| Postgres database | named volume `pgdata` (`docker-compose.prod.yml`) | Every user, saved search, application, delivery history and cover letter. Users cannot recreate it. |
| CV files | bind mount `./data/cvs` on the host | Real students' CVs — names, phone numbers, addresses. Personal data we asked them to trust us with. |

A single `docker compose down -v`, a disk failure, or a bad `rm -rf` ends the
project. Everything else in this audit is about degraded quality or
unnecessary risk. This one is about the project simply ceasing to exist,
and taking other people's personal data with it.

Scraped job postings are *not* in that list — they are public data and the
scraper rebuilds them within a cycle.

## What the service does

`Dockerfile.backup` builds `postgres:16-alpine` + `restic`, so one
container has both `pg_dump` (matching the server's major version) and an
encrypting, deduplicating backup client. `scripts/backup/run-backup.sh`
loops:

1. `pg_dump -Fc` the database to a temp file.
2. Refuse to continue if the dump is empty or `pg_restore --list` cannot
   read it. A truncated dump that gets snapshotted anyway is how "we had
   backups" turns into "we had files".
3. `restic backup` the dump plus `/data` (the CV files, mounted
   read-only).
4. `restic forget --keep-daily 30 --prune`.
5. `restic check --read-data-subset=5%` to catch silent remote corruption.
6. Optionally ping a dead-man-switch URL so a backup loop that *stops*
   is as visible as one that fails.

Snapshots are encrypted client-side with `RESTIC_PASSWORD`. The remote
provider never sees plaintext CVs.

## Setup

### Step 1 — provision a remote

Pick anything restic supports. Two cheap options that are not the same
machine as the VPS:

- **Backblaze B2** — create a bucket `gosha-backups` and an application
  key scoped to it. Repository string: `b2:gosha-backups:/`
- **Hetzner Storage Box / any SFTP host** — repository string:
  `sftp:u123456@u123456.your-storagebox.de:/gosha-backups`

Whatever you choose, it must be a **different provider or region** from
the VPS. A backup on the same failure domain is not a backup.

### Step 2 — generate the encryption password

```bash
openssl rand -base64 48
```

Put it in `.env` as `RESTIC_PASSWORD`, **and store a second copy somewhere
that is not this server** — a password manager, a printed page in a
drawer. If the VPS dies and this password died with it, the backups are
unreadable ciphertext and you have lost everything anyway.

### Step 3 — fill in `.env`

```env
RESTIC_REPOSITORY=b2:gosha-backups:/
RESTIC_PASSWORD=<from step 2>

# B2
B2_ACCOUNT_ID=<application key id>
B2_ACCOUNT_KEY=<application key>

# or S3-compatible
# RESTIC_ACCESS_KEY_ID=...
# RESTIC_SECRET_ACCESS_KEY=...

BACKUP_INTERVAL_SECONDS=86400
BACKUP_KEEP_DAILY=30
# Optional: https://healthchecks.io ping URL
BACKUP_HEALTHCHECK_URL=
```

### Step 4 — initialise and run once by hand

Do **not** start the loop first. Run one cycle in the foreground and read
the output:

```bash
cd ~/gosha
docker compose -f docker-compose.prod.yml --profile backup build backup
docker compose -f docker-compose.prod.yml --profile backup run --rm \
  -e BACKUP_INTERVAL_SECONDS=0 backup
```

Expect to see `Dump OK (… bytes)` with a plausible size, then restic
reporting added files, then `Backup complete`. Ctrl-C after the first
cycle.

Common failures at this point:

- `Fatal: unable to open config file` — the repository does not exist yet
  and `restic init` did not run. Run it manually with the same env:
  `docker compose -f docker-compose.prod.yml --profile backup run --rm --entrypoint restic backup init`
- `pg_dump: error: connection to server ... failed` — check
  `POSTGRES_PASSWORD` and that `postgres` is healthy.
- `pg_dump: server version mismatch` — the base image major version in
  `Dockerfile.backup` must match the `postgres:` image in
  `docker-compose.prod.yml`. Both are 16 today; change both together.

### Step 5 — start the loop

```bash
docker compose -f docker-compose.prod.yml --profile backup up -d backup
docker compose -f docker-compose.prod.yml logs -f backup
```

Note `--profile backup`: without it the service is inert, so the rest of
the stack is unaffected if you are not ready.

### Step 6 — test a restore

**This step is the backup.** Everything before it is a hopeful ritual.

List what you have:

```bash
docker compose -f docker-compose.prod.yml --profile backup run --rm \
  --entrypoint restic backup snapshots
```

Restore the newest snapshot into a scratch directory:

```bash
docker compose -f docker-compose.prod.yml --profile backup run --rm \
  --entrypoint restic backup restore latest --target /tmp/restore-test
```

Then prove the dump is real by loading it into a throwaway database:

```bash
docker run --rm -d --name pg-restore-test \
  -e POSTGRES_PASSWORD=test -e POSTGRES_USER=gosha -e POSTGRES_DB=gosha \
  postgres:16-alpine

docker cp /tmp/restore-test/tmp/gosha-backup/gosha.dump pg-restore-test:/tmp/

docker exec pg-restore-test pg_restore -U gosha -d gosha --no-owner /tmp/gosha.dump

docker exec pg-restore-test psql -U gosha -d gosha -c \
  "select count(*) from users; select count(*) from jobs; select count(*) from applications;"

docker rm -f pg-restore-test
```

The counts must be close to production. Also confirm the CV files came
back:

```bash
ls /tmp/restore-test/data/cvs | head
```

Then delete the scratch copy — it contains real CVs:

```bash
rm -rf /tmp/restore-test
```

### Step 7 — put a reminder in the calendar

Re-run Step 6 **every quarter**. Backup systems rot silently: a rotated
credential, a full bucket, a schema change. The restore is the only thing
that proves any of it still works.

## What is still missing

Even with this running, these are open:

- **No monitoring of backup age.** The dead-man-switch URL is optional and
  unset by default. Configure one — a stopped backup container is
  otherwise indistinguishable from a working one.
- **CVs are backed up encrypted but stored unencrypted.** Restic protects
  the copy, not the original on the VPS disk. Encryption at rest for
  `data/cvs` is a separate, still-open item.
- **No off-site copy of `.env`.** The backups are useless without
  `RESTIC_PASSWORD`, and the stack will not boot without the Discord and
  database secrets. Store them separately and deliberately.
- **Nobody has verified any of the above.** Including the person who
  wrote it.
