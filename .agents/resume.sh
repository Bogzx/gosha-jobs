#!/usr/bin/env bash
# Respawn the GOSHA agents in herdr panes and hand each its handoff file.
# Run from the repo root, inside a herdr pane:  .agents/resume.sh [agent ...]
# With no arguments, starts every agent that has a handoff (except the lead,
# which is assumed to be the pane running this).
set -euo pipefail

test "${HERDR_ENV:-}" = 1 || { echo "Run this inside herdr." >&2; exit 1; }
cd "$(git rev-parse --show-toplevel)"
ROOT=$PWD
CLAUDE_ARGS=(--dangerously-skip-permissions --model opus)

if [ $# -gt 0 ]; then
  agents=("$@")
else
  agents=()
  for f in .agents/handoff/gosha-*.md; do
    name=$(basename "$f" .md)
    [ "$name" = gosha-lead ] || agents+=("$name")
  done
fi

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

anchor="$HERDR_PANE_ID"
direction=right
for name in "${agents[@]}"; do
  handoff=".agents/handoff/$name.md"
  [ -f "$handoff" ] || { echo "skip $name: no $handoff" >&2; continue; }
  pane=$(herdr pane split --pane "$anchor" --direction "$direction" --cwd "$ROOT" --no-focus \
         | json "d['result']['pane']['pane_id']")
  # Further agents stack under the first one instead of narrowing columns.
  anchor=$pane; direction=down
  herdr agent start "$name" --kind claude --pane "$pane" --timeout 90000 -- "${CLAUDE_ARGS[@]}" \
    >/dev/null 2>&1 || true
  # First start in a new folder asks whether to trust it: the repo is ours.
  # The dialog's cursor starts on "No, exit": move to "Yes" before Enter.
  if herdr agent read "$name" 2>/dev/null | grep -q -e "trust this folder" -e "No, exit"; then
    herdr agent send-keys "$name" down >/dev/null
    herdr agent send-keys "$name" enter >/dev/null
  fi
  herdr agent wait "$name" --until idle --timeout 60000 >/dev/null 2>&1 || true
  herdr agent prompt "$name" "You are $name, one of several Claude agents coordinated in herdr by the orchestrator gosha-lead, continuing work started on another machine. Repo: $ROOT (branch wip/agents-2026-10-16). Read .agents/README.md (rules: never touch the live deployment, no commits to main, DM-only alerts) and then your handoff .agents/handoff/$name.md, which is your only memory of the previous session. Re-verify the 'Done' items quickly (run the listed tests), then continue from 'In progress' and 'Remaining TODO'. Respect file ownership. When you finish a unit of work, update your handoff file in place so it stays current, and tell gosha-lead via: herdr agent prompt gosha-lead '<agent>: <summary>'." \
    >/dev/null 2>&1 || echo "prompt to $name not confirmed; check its pane" >&2
  echo "started $name in $pane"
done
