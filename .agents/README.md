# Multi-agent work in progress (herdr)

Started 2026-10-16 on a laptop, moved to the deployment server to continue.
Goal from the owner: make GOSHA the most accurate job-finding platform, and
verify that it works as intended end to end.

Everything an agent needs is in this folder and in the branch
`wip/agents-2026-10-16`. Chat history does not travel: **the handoff files
are the source of truth.**

## Layout

| Agent | Area | Handoff |
|---|---|---|
| `gosha-lead` | Orchestrator: assigns work, reviews diffs, runs the full suite, commits | [handoff/gosha-lead.md](handoff/gosha-lead.md) |
| `gosha-scrape` | Sourcing: board adapters, JobSpy, dedup, field accuracy | [handoff/gosha-scrape.md](handoff/gosha-scrape.md) |
| `gosha-pipe` | Filters, match flow, Discord delivery, dead links | [handoff/gosha-pipe.md](handoff/gosha-pipe.md) |
| `gosha-rank` | Feed ranking: hybrid embedding + TF-IDF + seniority, eval | [handoff/gosha-rank.md](handoff/gosha-rank.md) |
| `gosha-label` | Held-out eval set in `eval/heldout/` | [handoff/gosha-label.md](handoff/gosha-label.md) |
| `gosha-health` | Scrape-failure alerts, DM-only to the owner | [handoff/gosha-health.md](handoff/gosha-health.md) |

File ownership is listed in each handoff. Two agents never edit the same
file; a change in someone else's file is requested through
`herdr agent prompt <owner> '...'`.

## Rules for every agent

- **Never work inside the live deployment directory**, and never restart,
  rebuild or redeploy the running containers. Work in a separate clone
  (below). Production data is read-only unless the owner says otherwise.
- No commits to `main`, no pushes that trigger the deploy workflow
  (`.github/workflows/ci.yml` deploys on push to `main`). The lead commits
  to `wip/agents-2026-10-16`.
- Do not send real Discord messages or post to channels while testing.
- Discord ops alerts go **only as a DM to the owner** (@Bogdanila, by
  numeric user ID in `ALERT_DM_USER_IDS`), never to a channel, and can be
  switched off.
- Ranking changes are validated on both `eval/postings.jsonl` and
  `eval/heldout/`; never tune on the held-out set.

## Live checks against job boards: run them in the prod image

The agents' `.venv` is uv's standalone CPython (3.11.16, bundled OpenSSL 3.5.9).
Prod runs Debian `python:3.11-slim` (CPython 3.11.17, OpenSSL 3.5.7). BestJobs'
Google front end refuses the venv's TLS handshake and accepts prod's. On 2026-10-16
13:11–13:12 UTC, the same request, the same code and the same IP gave **200 from the
prod image and 403 from the venv**. So a board measured from the venv is not evidence
of what prod sees. Run live checks in a throwaway container from the prod image instead:

```bash
# Only with gosha-lead's OK, never while a prod cycle runs (see the handoffs' timing rule).
docker run --rm -i --name gosha-agents-probe --network bridge --entrypoint python \
  -v "$PWD":/w:ro -w /w -e PYTHONPATH=/w -e PYTHONDONTWRITEBYTECODE=1 \
  gosha-bot:latest - < /path/to/your_script.py > /path/to/output.txt
```

- No `--env-file`, no secrets, no database, and no volume other than the read-only code mount.
- Never touch the running `gosha-bot`, `gosha-api`, `gosha-postgres` or `gosha-caddy` containers.
- The script comes in on stdin and its output goes to stdout. The mount is read-only, so it cannot write files.
- The image has `main`'s installed packages. Branch code that needs a new dependency will not import there.
- Print `sys.version` and `ssl.OPENSSL_VERSION` with every result, so it can be attributed.

## Resume on the server

```bash
# 1. A working clone next to, not inside, the deployment
git clone https://github.com/Bogzx/gosha-jobs.git ~/gosha-agents && cd ~/gosha-agents
git checkout wip/agents-2026-10-16

# 2. Python 3.11 venv with CPU torch (same as CI's eval job)
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -c constraints.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match \
  "torch==2.14.0+cpu"
uv pip install --python .venv/bin/python -r requirements-dev.txt -c constraints.txt scikit-learn
.venv/bin/python -m pytest -q        # baseline: see handoff/gosha-lead.md
# aarch64 server: sklearn's libgomp needs preloading or 4 semantic tests skip
# export LD_PRELOAD=$PWD/.venv/lib/python3.11/site-packages/scikit_learn.libs/libgomp-a49a47f9.so.1.0.0

# 3. Inside herdr on the server: start the lead, which starts the rest
.agents/resume.sh            # or: .agents/resume.sh gosha-rank gosha-pipe  (a subset)
```

`resume.sh` opens one pane per agent in the current herdr tab, starts
Claude Code in each and hands it its handoff file. Start the lead first
(it is the agent that reads all handoffs and reviews), or run the script
from the lead's pane.
