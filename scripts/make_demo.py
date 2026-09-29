#!/usr/bin/env python3
"""
make_demo.py - small demo package for a historical topic: a posts-per-month chart and a
few CSVs, restricted to the topic's study period.

  python scripts/make_demo.py                                   # threads.db, th_kh_border_en
  python scripts/make_demo.py --db copy.db --out export/demo

Re-tag first if the topic's keywords changed:
  python threads_collector.py tag topics/th_kh_border_en.json

Writes to --out: posts_per_month.png, monthly_on_topic_posts.csv, sample_posts.csv,
top_accounts.csv. Columns that are still empty (e.g. country before location lookups
ran) are left out rather than shown blank.
"""
import argparse
import csv
import json
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Conflict phases inside the study period (UTC dates, end exclusive)
PHASES = [
    ("1 before (May-Jun 2025)", "2025-05-01", "2025-07-01"),
    ("2 July fighting", "2025-07-01", "2025-08-01"),
    ("3 between (Aug-Nov 2025)", "2025-08-01", "2025-12-01"),
    ("4 December fighting", "2025-12-01", "2026-01-01"),
    ("5 after (Jan 2026)", "2026-01-01", "2026-02-01"),
]
FIGHTING = {"2025-07": "July fighting", "2025-12": "December fighting"}
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# chart tokens (light surface)
SURF, INK, INK2, GRID, AXIS, BAND, SERIES = (
    "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#c9c8c3", "#efeeea", "#2a78d6")


def months_between(start: str, end: str):
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y}-{m:02d}" <= end[:7]:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--topic", default="topics/th_kh_border_en.json")
    ap.add_argument("--out", type=Path, default=Path("export/demo"))
    a = ap.parse_args()

    topic = json.loads(Path(a.topic).read_text(encoding="utf-8"))
    name, per = topic["name"], topic["period"]
    db = sqlite3.connect(a.db)
    a.out.mkdir(parents=True, exist_ok=True)

    on = f"""FROM posts p JOIN post_topics t ON t.post_pk = p.pk AND t.topic = '{name}'
             LEFT JOIN users u ON u.pk = p.user_pk
             WHERE t.on_topic = 1
               AND p.taken_at >= strftime('%s', '{per["from"]}')
               AND p.taken_at < strftime('%s', '{per["to"]}', '+1 day')"""

    def dump(fname, sql):
        cur = db.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        keep = [i for i, c in enumerate(cols) if any(r[i] not in (None, "") for r in rows)]
        with open(a.out / fname, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([cols[i] for i in keep])
            w.writerows([[r[i] for i in keep] for r in rows])
        dropped = [c for i, c in enumerate(cols) if i not in keep]
        print(f"{fname}: {len(rows)} rows" + (f" (dropped empty: {', '.join(dropped)})" if dropped else ""))

    # 1. monthly counts inside the study period
    months = months_between(per["from"], per["to"])
    counts = dict(db.execute(f"SELECT strftime('%Y-%m', p.taken_at, 'unixepoch'), COUNT(*) {on} GROUP BY 1"))
    vals = [counts.get(m, 0) for m in months]
    with open(a.out / "monthly_on_topic_posts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "on_topic_posts"])
        w.writerows(zip(months, vals))

    # 2. sample posts: top 8 by likes per phase
    phase = "CASE " + " ".join(
        f"WHEN p.taken_at < strftime('%s','{end}') THEN '{label}'" for label, _, end in PHASES
    ) + " END"
    dump("sample_posts.csv", f"""
      SELECT phase, posted_utc, username, account_country, followers, verified, likes,
             replies, reposts, is_reply, text, url FROM (
        SELECT {phase} AS phase, datetime(p.taken_at, 'unixepoch') AS posted_utc, p.username,
               u.country AS account_country, u.follower_count AS followers,
               u.is_verified AS verified, p.like_count AS likes, p.reply_count AS replies,
               p.repost_count AS reposts, p.is_reply, p.text, p.url,
               ROW_NUMBER() OVER (PARTITION BY {phase} ORDER BY p.like_count DESC) AS rk
        {on})
      WHERE rk <= 8 ORDER BY phase, likes DESC""")

    # 3. most active accounts, with repost-to-like ratio (a simple amplification signal)
    dump("top_accounts.csv", f"""
      SELECT p.username, u.full_name, u.is_verified AS verified, u.follower_count AS followers,
             u.country AS account_country, COUNT(*) AS on_topic_posts,
             SUM(p.is_reply) AS replies, SUM(p.like_count) AS total_likes,
             SUM(p.repost_count) AS total_reposts,
             ROUND(1.0 * SUM(p.repost_count) / NULLIF(SUM(p.like_count), 0), 3) AS repost_like_ratio
      {on} GROUP BY p.username ORDER BY on_topic_posts DESC, total_likes DESC LIMIT 20""")

    # 4. chart
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=200)
    fig.patch.set_facecolor(SURF)
    ax.set_facecolor(SURF)
    x = range(len(months))
    for m in FIGHTING:
        if m in months:
            i = months.index(m)
            ax.axvspan(i - 0.5, i + 0.5, color=BAND, zorder=0, lw=0)
    ax.bar(x, vals, width=0.62, color=SERIES, zorder=2)
    for m, label in FIGHTING.items():
        if m in months:
            i = months.index(m)
            ax.annotate(f"{vals[i]}", (i, vals[i]), xytext=(0, 4), textcoords="offset points",
                        ha="center", va="bottom", fontsize=10, color=INK, fontweight="bold")
            ax.annotate(label, (i, vals[i]), xytext=(0, 20), textcoords="offset points",
                        ha="center", va="bottom", fontsize=9, color=INK2)
    labels = [MONTH_ABBR[int(m[5:]) - 1] + (f"\n{m[:4]}" if i == 0 or m.endswith("-01") else "")
              for i, m in enumerate(months)]
    ax.set_xticks(list(x), labels, fontsize=8.5, color=INK2)
    ax.tick_params(axis="y", labelsize=8.5, colors=INK2, length=0)
    ax.tick_params(axis="x", length=0)
    ax.set_ylim(0, max(vals + [1]) * 1.3)
    ax.yaxis.grid(True, color=GRID, lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color(AXIS)
    total = sum(vals)
    ax.set_title("English Threads posts about the Thailand–Cambodia border conflict",
                 loc="left", fontsize=12, color=INK, pad=22)
    ax.text(0, 1.035, f"On-topic posts per month in the study period "
            f"({per['from']} to {per['to']}), n = {total}, by posting date (UTC)",
            transform=ax.transAxes, fontsize=9, color=INK2)
    fig.tight_layout()
    fig.savefig(a.out / "posts_per_month.png", facecolor=SURF)

    accts = db.execute(f"SELECT COUNT(DISTINCT p.username) {on}").fetchone()[0]
    print(f"study period: {total} on-topic posts from {accts} accounts; "
          + ", ".join(f"{m}={counts.get(m, 0)}" for m in FIGHTING))


if __name__ == "__main__":
    main()
