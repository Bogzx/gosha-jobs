# Backups

> ## ⚠️ Not running in production yet
>
> The mechanism is **verified, the deployment is not.** `scripts/backup/drill.sh`
> runs the whole loop on throwaway containers: the app's real schema, a
> `pg_dump`, a restic snapshot, a restore into a fresh Postgres with
> identical row counts, and decryption of the restored CVs. It also checks that a
> broken backup fails instead of reporting success. CI repeats it on every
> change to the backup code (`.github/workflows/backup-drill.yml`).
>
> What no drill can do is prove *your* remote, *your* credentials and
> *your* data. Until an operator has completed
> [Step 6 — test a restore](#step-6--test-a-restore) against the
> production repository, assume there are **no backups**.

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
the output (`BACKUP_ONCE=1` runs a single cycle and exits with its status):

```bash
cd ~/gosha
docker compose -f docker-compose.prod.yml --profile backup build backup
docker compose -f docker-compose.prod.yml --profile backup run --rm \
  -e BACKUP_ONCE=1 backup
echo "exit status: $?"
```

Expect `Dump OK (… bytes)` with a plausible size, restic reporting added
files, `Backup complete`, and exit status 0. Anything else is a failure:
the script exits non-zero and does not log `Backup complete` or ping the
healthcheck URL.

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

`restore-drill.sh` (shipped in the backup image) restores the newest
snapshot inside a throwaway container, loads the dump into a scratch
Postgres *inside that same container*, and prints every table's row count
next to the live database's. Nothing outside the container is written:

```bash
docker compose -f docker-compose.prod.yml --profile backup run --rm \
  --entrypoint /bin/sh backup /usr/local/bin/restore-drill.sh
```

It ends with `RESTORE DRILL PASSED` (exit 0) when the snapshot loads, no
table that has rows live came back empty, no live table is missing and
the CV files came back. Restored counts slightly below live are normal:
production kept writing after the snapshot.

Then prove the restored CVs open with the key in `.env`. The backup holds
ciphertext, and the key is not in it:

```bash
mkdir -p /tmp/gosha-restore && chmod 700 /tmp/gosha-restore
docker compose -f docker-compose.prod.yml --profile backup run --rm \
  -v /tmp/gosha-restore:/restore-out \
  --entrypoint /bin/sh backup /usr/local/bin/restore-drill.sh
# -u 0: the scratch directory is private to your host user, and the
# restored files keep their original owner and mode.
docker compose -f docker-compose.prod.yml run --rm --no-deps -u 0 \
  -v /tmp/gosha-restore:/restore-out:ro \
  bot python scripts/backup/verify_cvs.py /restore-out/data/cvs
sudo rm -rf /tmp/gosha-restore     # restored files are root-owned
```

`verify_cvs.py` prints counts only (`N CV file(s) decrypt, 0 do not`),
never CV text.

### Step 7 — put a reminder in the calendar

Re-run Step 6 **every quarter**. Backup systems rot silently: a rotated
credential, a full bucket, a schema change. The restore is the only thing
that proves any of it still works.

## What is still missing

Even with this running, these are open:

- **No monitoring of backup age.** The dead-man-switch URL is optional and
  unset by default. Configure one — a stopped backup container is
  otherwise indistinguishable from a working one.
- **`CV_ENCRYPTION_KEY` must be backed up too, separately.** CVs are now
  encrypted at rest (`data/cvs/<uid>.enc`, gosha/cv_crypto.py), so a
  restored `data/cvs` is unreadable without the key that wrote it.
- **No off-site copy of `.env`.** The backups are useless without
  `RESTIC_PASSWORD`, and the stack will not boot without the Discord and
  database secrets. Store them separately and deliberately.
- **The drill does not prove the production remote.** It proves the
  scripts and the image. Step 6 against the real repository is still the
  only proof that the data on that remote can be restored.
