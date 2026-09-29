#!/usr/bin/env bash
# One scheduled collection round for the election topic. Logs to logs/YYYY-MM-DD.log.
# Exit code 2 = login wall: run `python threads_collector.py login` again.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${THREADS_PY:-$HOME/.conda/envs/threads-scraper/bin/python}"
TOPIC="${THREADS_TOPIC:-topics/tw2026_local.json}"
mkdir -p logs
LOG="logs/$(date +%F).log"
{
  echo "=== $(date -Is) start"
  "$PY" threads_collector.py snowball "$TOPIC"
  code=$?
  echo "=== $(date -Is) exit $code"
} >>"$LOG" 2>&1
exit $code
