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

# 3. Inside herdr on the server: start the lead, which starts the rest
.agents/resume.sh            # or: .agents/resume.sh gosha-rank gosha-pipe  (a subset)
```

`resume.sh` opens one pane per agent in the current herdr tab, starts
Claude Code in each and hands it its handoff file. Start the lead first
(it is the agent that reads all handoffs and reviews), or run the script
from the lead's pane.
