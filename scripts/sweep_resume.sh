#!/usr/bin/env bash
# After a sweep stopped on repeated failures: one test lookup on a known-good account.
# If it succeeds, resume the sweep; if not, stay stopped (possible blocking).
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${THREADS_PY:-$HOME/.conda/envs/threads-scraper/bin/python}"
LOG="logs/$(date +%F).log"
echo "=== $(date -Is) sweep resume check: test lookup on @mosseri" | tee -a "$LOG"
OUT=$("$PY" -u threads_collector.py location mosseri 2>&1); echo "$OUT" | tee -a "$LOG"
if echo "$OUT" | grep -q "@mosseri: United States"; then
  echo "### test lookup ok - resuming sweep" | tee -a "$LOG"
  exec scripts/sweep.sh "$@"
fi
echo "### test lookup failed - sweep stays stopped (possible blocking); check again later" | tee -a "$LOG"
exit 5
