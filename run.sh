#!/usr/bin/env bash
# One pass of the whole pipeline. Safe to run on a schedule — every step is idempotent and
# picks up where the last run stopped. A lock file makes overlapping runs impossible.
#
#   ./run.sh                 # fetch new videos, transcribe, index, find what is new, read it
#   ./run.sh --no-read       # skip the AI reading pass (no model needed)
#
# Add to cron for every 4 hours:
#   17 */4 * * * /path/to/channel-miner/run.sh >> /path/to/channel-miner/data/run.log 2>&1
set -uo pipefail
cd "$(dirname "$0")"

DATA=$(python3 -c "import sys; sys.path.insert(0,'miner'); from settings import S; print(S.data_dir)")
mkdir -p "$DATA"
LOG="$DATA/run.log"

exec 9>"$DATA/.run.lock"
flock -n 9 || { echo "$(date -u +%FT%TZ) skipped: another run holds the lock" >> "$LOG"; exit 0; }
echo "=== $(date -u +%FT%TZ) start" >> "$LOG"

step() {                       # step <name> <timeout> <command...>
  local name=$1 t=$2; shift 2
  timeout "$t" "$@" >> "$LOG" 2>&1
  echo "--- $name exit=$?" >> "$LOG"
}

step archive   3600 python3 miner/archive.py
step transcribe 3000 python3 miner/transcribe_missing.py --limit 2
step index      900 python3 miner/index.py
step novelty    900 python3 miner/novelty.py --watch

# The reading pass needs two things the minimal setup does not have: a rubric (what you already
# know) and a local model. Without either, skip it and say so — the transcripts, the index and the
# novelty list are the useful part and they need neither.
HAS_RUBRIC=$(python3 -c "import sys; sys.path.insert(0,'miner'); from settings import S; print(1 if S.has_rubric else 0)" 2>/dev/null || echo 0)
if [ "${1:-}" = "--no-read" ]; then
  :
elif [ "$HAS_RUBRIC" != "1" ]; then
  echo "$(date -u +%FT%TZ) read: skipped — no rubric in config.json (see README: 'Going further')" >> "$LOG"
else
  step read      3000 python3 miner/read.py
  step adjudicate 5400 python3 miner/adjudicate.py
fi

# Downloads failing is the one silent killer: the corpus simply stops growing and nothing says so.
FAILS=$(awk '/^=== .* start$/ {n = 0} /audio download FAILED/ {n++} END {print n + 0}' "$LOG")
if [ "${FAILS:-0}" -ge 2 ]; then
  echo "WARNING: $FAILS downloads failed this run — update yt-dlp and check the deno runtime" \
       | tee -a "$LOG"
fi

echo "=== $(date -u +%FT%TZ) done" >> "$LOG"
tail -n 3000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
