#!/usr/bin/env python3
"""
report_stats.py - collection summary for a progress report.

  python scripts/report_stats.py                       # threads.db, th_kh_border_en
  python scripts/report_stats.py --db copy.db --topic topics/other.json

Run `tag` and `features.py --topic ...` first so on-topic flags and copy clusters are
current. Prints plain-text sections; nothing is written.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features import joined_month, signup_number  # noqa: E402


def pct(n, d):
    return f"{n} ({100 * n / d:.1f}%)" if d else f"{n} (-)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--topic", default="topics/th_kh_border_en.json")
    a = ap.parse_args()
    topic = json.loads(Path(a.topic).read_text(encoding="utf-8"))
    name = topic["name"]
    db = sqlite3.connect(a.db)
    q = lambda sql, *p: db.execute(sql, p).fetchall()  # noqa: E731
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]  # noqa: E731
    has = lambda t: bool(one("SELECT COUNT(*) FROM sqlite_master WHERE name=?", t))  # noqa: E731
    ON = f"JOIN post_topics t ON t.post_pk = p.pk AND t.topic = '{name}' AND t.on_topic = 1"

    print(f"# Collection summary: {a.db}, topic {name}\n")

    print("## 1. Row counts")
    total = one("SELECT COUNT(*) FROM posts")
    print(f"posts (all): {total}")
    print(f"  on-topic: {one(f'SELECT COUNT(*) FROM posts p {ON}')}")
    print(f"  replies (is_reply=1): {one('SELECT COUNT(*) FROM posts WHERE is_reply=1')}")
    for fa, n in q("SELECT found_as, COUNT(*) FROM posts GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  found_as={fa}: {n}")
    for t in ("users", "post_snapshots", "visits", "text_clusters"):
        print(f"{t}: {one(f'SELECT COUNT(*) FROM {t}') if has(t) else 'not built (run features.py)'}")

    print("\n## 2. On-topic posts")
    lo, hi, authors = q(f"""SELECT datetime(MIN(p.taken_at),'unixepoch'), datetime(MAX(p.taken_at),'unixepoch'),
                                   COUNT(DISTINCT p.username) FROM posts p {ON}""")[0]
    print(f"taken_at range: {lo} .. {hi} UTC; distinct authors: {authors}")
    per = topic.get("period")
    if per:
        n_in = one(f"""SELECT COUNT(*) FROM posts p {ON} WHERE p.taken_at >= strftime('%s', ?)
                       AND p.taken_at < strftime('%s', ?, '+1 day')""", per["from"], per["to"])
        print(f"inside study period {per['from']}..{per['to']}: {n_in}")
    phases = [("before (May-Jun 2025)", "2025-05-01", "2025-07-01"),
              ("July fighting", "2025-07-01", "2025-08-01"),
              ("between (Aug-Nov 2025)", "2025-08-01", "2025-12-01"),
              ("December fighting", "2025-12-01", "2026-01-01"),
              ("after (Jan 2026)", "2026-01-01", "2026-02-01")]
    for label, s, e in phases:
        n, au, rep = q(f"""SELECT COUNT(*), COUNT(DISTINCT p.username), SUM(p.is_reply) FROM posts p {ON}
                           WHERE p.taken_at >= strftime('%s', ?) AND p.taken_at < strftime('%s', ?)""", s, e)[0]
        print(f"  {label:<24} posts {n:>5}  authors {au:>5}  replies {rep or 0:>5}")

    print("\n## 3. Search terms and on-topic rule")
    print(f"search mode(s): {topic.get('search_modes', ['recent'])}")
    print(f"search terms ({len(topic['search'])}): {', '.join(topic['search'])}")
    hashtags = [w for w in topic["search"] + topic["keywords"] if w.startswith("#")]
    print(f"hashtag terms: {hashtags or 'none'}")
    groups = topic.get("require_any") or []
    if groups and isinstance(groups[0], str):
        groups = [groups]
    print(f"rule: one of {len(topic['keywords'])} keywords"
          + "".join(f" AND one of {len(g)} group-{i + 1} terms" for i, g in enumerate(groups))
          + f"; language in {topic.get('languages')}; whole-word match for English terms")
    print(f"seeds: {', '.join(topic['seeds'])}")

    print("\n## 4. Location ('Based in')")
    def loc(where, label):
        rows = q(f"SELECT country, country_raw FROM users u WHERE country_checked_at IS NOT NULL {where}")
        n = len(rows)
        has_c = sum(1 for c, _ in rows if c)
        hidden = sum(1 for c, r in rows if not c and r and "Not shared" in r)
        err = sum(1 for c, r in rows if not c and r and r.startswith("ui_error"))
        no_row = n - has_c - hidden - err
        print(f"{label}: checked {n}; country {pct(has_c, n)}; 'Not shared' {pct(hidden, n)}; "
              f"no Based-in row {pct(no_row, n)}; ui_error {pct(err, n)}")
    loc("", "all checked users")
    loc(f"AND u.pk IN (SELECT p.user_pk FROM posts p {ON})", "on-topic authors")
    print("top countries (on-topic authors):")
    for c, n in q(f"""SELECT country, COUNT(*) FROM users u WHERE country IS NOT NULL
                      AND u.pk IN (SELECT p.user_pk FROM posts p {ON})
                      GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""):
        print(f"  {c}: {n}")

    print("\n## 5. Throughput and blocking")
    for kind, n, seen, secs in q("""SELECT kind, COUNT(*), SUM(seen_posts), SUM(finished_at - started_at)
                                    FROM visits GROUP BY 1 ORDER BY 1"""):
        rate = f"{3600 * seen / secs:.0f} posts/h" if secs and seen else "-"
        print(f"  {kind:<11} loads {n:>4}  page time {secs / 3600:5.2f} h  seen {seen or 0:>6}  {rate}")
    secs, seen = q("SELECT SUM(finished_at - started_at), SUM(seen_posts) FROM visits")[0]
    span = one("SELECT MAX(finished_at) - MIN(started_at) FROM visits")
    print(f"total page time {secs / 3600:.2f} h (wall clock incl. pauses {span / 3600:.2f} h); "
          f"{3600 * seen / secs:.0f} posts seen per page-hour")
    print("statuses:", ", ".join(f"{s}={n}" for s, n in q("SELECT status, COUNT(*) FROM visits GROUP BY 1")))

    print("\n## 6. Join dates of looked-up accounts")
    def joins(where, label):
        raws = [r for (r,) in q(f"SELECT country_raw FROM users u WHERE country_raw LIKE '%Joined%' {where}")]
        months = [joined_month(r) for r in raws]
        months = [m for m in months if m]
        n = len(months)
        jul23 = sum(m == "2023-07" for m in months)
        in_per = sum(per and per["from"][:7] <= m <= per["to"][:7] for m in months) if per else 0
        over = sum(signup_number(r)[1] == 1 for r in raws)
        print(f"{label}: {n} with join date; July 2023 {pct(jul23, n)}; "
              f"joined inside study period {pct(in_per, n)}; signup 100M+ {pct(over, n)}")
    joins("", "all looked-up")
    joins(f"AND u.pk IN (SELECT p.user_pk FROM posts p {ON})", "on-topic authors")

    print("\n## 7. Copy clusters")
    if not has("text_clusters"):
        print("not built (run features.py)")
        return
    multi = q("SELECT cluster_id, n_accounts, n_posts, min_gap_s, span_hours, min_signup_gap FROM text_clusters WHERE n_accounts >= 2")
    on_ids = {c for (c,) in q(f"""SELECT DISTINCT c.cluster_id FROM post_text_clusters c
                                   JOIN posts p ON p.pk = c.post_pk {ON}""")}
    print(f"clusters: {one('SELECT COUNT(*) FROM text_clusters')} total, {len(multi)} spanning 2+ accounts, "
          f"{sum(c[0] in on_ids for c in multi)} of those contain an on-topic post")
    if multi:
        gaps = sorted(c[3] for c in multi if c[3] is not None)
        print(f"accounts per cluster: {min(c[1] for c in multi)}..{max(c[1] for c in multi)}; "
              f"min gap between copies: {gaps[0]} s .. {gaps[-1]} s; "
              f"under 60 s: {sum(g < 60 for g in gaps)}, under 1 h: {sum(g < 3600 for g in gaps)}")
        sg = [c[5] for c in multi if c[5] is not None]
        print(f"clusters with a signup-number gap: {len(sg)}" + (f" (smallest {min(sg)})" if sg else ""))


if __name__ == "__main__":
    main()
