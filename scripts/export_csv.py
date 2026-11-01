#!/usr/bin/env python3
"""
export_csv.py - analysis-ready CSV export of one topic's study period.

  python scripts/export_csv.py                          # threads.db, th_kh_border_en -> export/csv/
  python scripts/export_csv.py --db copy.db --out somewhere/

Run `threads_collector.py tag <topic>` and `features.py --topic <topic>` first.

Files (UTF-8 with BOM so Excel shows Thai/Khmer correctly):
  posts_on_topic.csv   one row per on-topic post in the study period, with author, language,
                       copy-cluster and hashtag columns joined in
  accounts.csv         one row per account that wrote at least one of those posts, with all
                       account-level features
  text_clusters.csv    copy-paste clusters that span 2+ accounts
  hashtags.csv         every hashtag in the collected posts
  hashtag_pairs.csv    account pairs linked by rare hashtags in 2+ separate posts each
Column meanings: see the data dictionary in the dataset report (scripts/make_report.py).
"""
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features import hashtags_of  # noqa: E402

PHASES = [("before", "2025-05-01", "2025-07-01"), ("july_fighting", "2025-07-01", "2025-08-01"),
          ("between", "2025-08-01", "2025-12-01"), ("december_fighting", "2025-12-01", "2026-01-01"),
          ("after", "2026-01-01", "2026-02-01")]


def phase_sql(col: str) -> str:
    return "CASE " + " ".join(
        f"WHEN {col} < strftime('%s','{end}') THEN '{name}'" for name, _, end in PHASES) + " END"


def write(path: Path, cur, extra=None):
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if extra:
        cols, rows = extra(cols, rows)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"{path}: {len(rows)} rows, {len(cols)} columns")
    return cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--topic", default="topics/th_kh_border_en.json")
    ap.add_argument("--out", type=Path, default=Path("export/csv"))
    a = ap.parse_args()
    topic = json.loads(Path(a.topic).read_text(encoding="utf-8"))
    name, per = topic["name"], topic["period"]
    db = sqlite3.connect(a.db)
    a.out.mkdir(parents=True, exist_ok=True)

    in_period = f"""p.taken_at >= strftime('%s','{per["from"]}')
                    AND p.taken_at < strftime('%s','{per["to"]}','+1 day')"""
    posts = db.execute(f"""
        SELECT p.pk AS post_id, p.url, datetime(p.taken_at,'unixepoch') AS posted_utc,
               {phase_sql('p.taken_at')} AS phase,
               p.username, p.user_pk AS user_id, p.text, f.lang,
               p.is_reply, p.found_as, COALESCE(t.match,'keywords') AS on_topic_reason,
               t.matched_keywords, p.reply_to_username, p.root_post_username,
               CASE WHEN p.source LIKE 'post:%' AND p.found_as = 'reply'
                    THEN substr(p.source, 6) END AS thread_page_url,
               p.like_count, p.reply_count, p.repost_count, p.quote_count, p.reshare_count,
               p.media_type, p.link_url, p.quoted_post_pk AS quoted_post_id, p.is_edited, p.ai_label,
               c.cluster_id AS copy_cluster_id, c.n_accounts AS copy_cluster_accounts,
               c.min_gap_s AS copy_cluster_min_gap_s,
               p.source AS found_via, datetime(p.first_scraped_at,'unixepoch') AS first_scraped_utc
        FROM posts p
        JOIN post_topics t ON t.post_pk = p.pk AND t.topic = ? AND t.on_topic = 1
        LEFT JOIN post_features f ON f.post_pk = p.pk
        LEFT JOIN post_text_clusters pc ON pc.post_pk = p.pk
        LEFT JOIN text_clusters c ON c.cluster_id = pc.cluster_id AND c.n_accounts >= 2
        WHERE {in_period}
        ORDER BY p.taken_at""", (name,))

    def add_hashtags(cols, rows):
        i = cols.index("text")
        return cols + ["hashtags"], [list(r) + [json.dumps(sorted(hashtags_of(r[i])), ensure_ascii=False)]
                                     for r in rows]
    write(a.out / "posts_on_topic.csv", posts, add_hashtags)

    write(a.out / "accounts.csv", db.execute(f"""
        WITH mine AS (SELECT p.user_pk, COUNT(*) AS n_on_topic_posts, SUM(p.is_reply) AS n_on_topic_replies
                      FROM posts p JOIN post_topics t ON t.post_pk = p.pk AND t.topic = ? AND t.on_topic = 1
                      WHERE {in_period} GROUP BY p.user_pk)
        SELECT u.pk AS user_id, u.username, u.full_name, u.is_verified, u.follower_count, u.bio,
               u.country AS based_in_country,
               CASE WHEN u.country_checked_at IS NULL THEN 'not_checked'
                    WHEN u.country IS NOT NULL THEN 'country'
                    WHEN u.country_raw LIKE '%Not shared%' THEN 'not_shared'
                    WHEN u.country_raw = 'account_gone' THEN 'account_gone'
                    WHEN u.country_raw LIKE 'ui_error%' THEN 'lookup_failed'
                    ELSE 'no_based_in_row' END AS country_status,
               u.lookup_reason, datetime(u.gone_at,'unixepoch') AS gone_utc,
               f.joined_month, f.account_age_months, f.signup_number, f.signup_over_100m,
               f.joined_in_period, f.months_join_to_next_peak,
               m.n_on_topic_posts, m.n_on_topic_replies, f.n_posts AS n_posts_collected,
               f.top_lang, f.top_lang_share,
               f.tz_offset, f.tz_margin, f.tz_quiet_share, f.tz_n_posts,
               f.bio_country, f.bio_term, f.flag_bio_vs_country, f.flag_tz_vs_country,
               f.n_copy_posts, f.n_copy_partners, f.fastest_copy_gap_s, f.min_signup_gap_to_partner,
               f.n_hashtags, f.n_campaign_tags, f.n_rare_tag_partners,
               datetime(u.first_seen,'unixepoch') AS first_seen_utc,
               datetime(u.country_checked_at,'unixepoch') AS country_checked_utc
        FROM mine m JOIN users u ON u.pk = m.user_pk
        LEFT JOIN user_features f ON f.user_pk = u.pk
        ORDER BY m.n_on_topic_posts DESC""", (name,)))

    write(a.out / "text_clusters.csv", db.execute("""
        SELECT cluster_id, n_posts, n_accounts, usernames, exact,
               datetime(first_at,'unixepoch') AS first_utc, datetime(last_at,'unixepoch') AS last_utc,
               span_hours, min_gap_s, median_gap_s, min_signup_gap, n_with_signup, sample_text
        FROM text_clusters WHERE n_accounts >= 2 ORDER BY n_accounts DESC"""))
    write(a.out / "hashtags.csv", db.execute("""
        SELECT tag, n_accounts, n_posts, is_campaign,
               datetime(first_at,'unixepoch') AS first_utc, datetime(last_at,'unixepoch') AS last_utc
        FROM hashtags ORDER BY n_accounts DESC"""))
    write(a.out / "hashtag_pairs.csv", db.execute("""
        SELECT user_a_pk, user_b_pk, user_a, user_b, n_shared, posts_a, posts_b, n_days, shared_tags
        FROM hashtag_pairs ORDER BY MIN(posts_a, posts_b) DESC, n_days DESC, n_shared DESC"""))


if __name__ == "__main__":
    main()
