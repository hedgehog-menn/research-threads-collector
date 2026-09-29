#!/usr/bin/env bash
# One scheduled collection round per topic. Logs to logs/YYYY-MM-DD.log.
# Exit code 2 = login wall: run `python threads_collector.py login` again.
# THREADS_TOPICS: space-separated topic files, run one after another.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${THREADS_PY:-$HOME/.conda/envs/threads-scraper/bin/python}"
TOPICS="${THREADS_TOPICS:-topics/tw2026_local_en.json}"
mkdir -p logs
LOG="logs/$(date +%F).log"
code=0
for topic in $TOPICS; do
  {
    echo "=== $(date -Is) start $topic"
    "$PY" threads_collector.py snowball "$topic"
    code=$?
    echo "=== $(date -Is) exit $code"
  } >>"$LOG" 2>&1
  [ "$code" -eq 2 ] && break  # login wall: don't try the next topic
done
exit $code
