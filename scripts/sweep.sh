#!/usr/bin/env bash
# Location sweep: look up the country of every unchecked on-topic author, in batches.
#   scripts/sweep.sh [topic] [batch size] [break minutes]
# Stops when nothing is left, on a login wall (exit 2), on repeated page failures (exit 3),
# or when a batch stops early after 3 failed lookups in a row (possible blocking).
# Keeps lookups in any rolling 24 h under THREADS_DAILY_LOOKUPS (default 800).
# Run it under a sleep blocker, e.g.:
#   systemd-run --user --unit=threads-sweep systemd-inhibit --what=idle:sleep scripts/sweep.sh
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${THREADS_PY:-$HOME/.conda/envs/threads-scraper/bin/python}"
TOPIC="${1:-topics/th_kh_border_en.json}"
BATCH="${2:-100}"
BREAK_MIN="${3:-25}"
# Threads limits "About this profile": about 925 lookups ran fine, then failures came in
# threes, and only cleared after ~24 h. Keep lookups in any rolling 24 h under this budget.
DAILY="${THREADS_DAILY_LOOKUPS:-800}"
DB="${THREADS_DB:-threads.db}"
mkdir -p logs
n=0
while true; do
  while true; do
    used=$(sqlite3 "$DB" "SELECT COUNT(*) FROM visits WHERE kind = 'location' AND started_at > strftime('%s','now') - 86400")
    [ "$((used + BATCH))" -le "$DAILY" ] && break
    echo "--- $(date -Is) daily budget: $used of $DAILY lookups used in the last 24 h; waiting 30 min" | tee -a "logs/$(date +%F).log"
    sleep 1800
  done
  n=$((n + 1))
  LOG="logs/$(date +%F).log"
  OUT=$(mktemp)
  echo "=== $(date -Is) sweep batch $n ($BATCH lookups)" | tee -a "$LOG"
  "$PY" -u threads_collector.py snowball "$TOPIC" --only-locations --max-locations "$BATCH" 2>&1 | tee "$OUT" >> "$LOG"
  code=${PIPESTATUS[0]}
  echo "=== $(date -Is) sweep batch $n exit $code" | tee -a "$LOG"
  if [ "$code" -ne 0 ]; then echo "### sweep stopped: exit $code" | tee -a "$LOG"; rm -f "$OUT"; exit "$code"; fi
  if grep -q "failures in a row" "$OUT"; then
    echo "### sweep stopped: repeated lookup failures (possible blocking)" | tee -a "$LOG"; rm -f "$OUT"; exit 4
  fi
  if grep -q "location sweep: 0 this batch" "$OUT"; then
    echo "### sweep finished: no unchecked on-topic authors left" | tee -a "$LOG"; rm -f "$OUT"; exit 0
  fi
  rm -f "$OUT"
  echo "--- break ${BREAK_MIN} min" | tee -a "$LOG"
  sleep $((BREAK_MIN * 60))
done
