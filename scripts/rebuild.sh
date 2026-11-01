#!/bin/bash
# Rebuild tags, features, CSVs, demo and report from export/snapshot.db.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="$HOME/.conda/envs/threads-scraper/bin/python"
DB=export/snapshot.db; T=topics/th_kh_border_en.json
step() { echo "=== $(date -Is) $*"; }
step tag;      "$PY" -u threads_collector.py --db $DB tag $T | tail -2
step features; "$PY" -u features.py --db $DB --topic $T | tail -4
step csv;      "$PY" -u scripts/export_csv.py --db $DB
step demo;     "$PY" -u scripts/make_demo.py --db $DB | tail -3
step stats;    "$PY" -u scripts/report_stats.py --db $DB > export/demo/report_stats.txt
step report;   "$PY" -u scripts/make_report.py --db $DB | tail -5
step done
