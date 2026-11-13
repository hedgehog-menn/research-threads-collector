#!/usr/bin/env bash
# One scheduled collection round per topic. Logs to logs/YYYY-MM-DD.log.
# Exit code 2 = login wall: run `python threads_collector.py login` again.
# THREADS_TOPICS: space-separated topic files, run one after another.
# THREADS_DB: database file (default threads.db).
# THREADS_ARGS: extra snowball options, e.g. "--max-post-pages 100".
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${THREADS_PY:-$HOME/.conda/envs/threads-scraper/bin/python}"
TOPICS="${THREADS_TOPICS:-topics/th_kh_border_en.json}"
DB="${THREADS_DB:-threads.db}"
read -r -a EXTRA <<<"${THREADS_ARGS:-}"
mkdir -p logs
LOG="logs/$(date +%F).log"
code=0
for topic in $TOPICS; do
  {
    echo "=== $(date -Is) start $topic ($DB)"
    "$PY" -u threads_collector.py --db "$DB" snowball "$topic" "${EXTRA[@]}"
    code=$?
    echo "=== $(date -Is) exit $code"
  } >>"$LOG" 2>&1
  [ "$code" -eq 2 ] && break  # login wall: don't try the next topic
done
exit $code
