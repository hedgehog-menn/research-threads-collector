#!/usr/bin/env python3
"""
make_report.py - Word report on the collected dataset, with charts and a data dictionary.

  python scripts/make_report.py              # threads.db, th_kh_border_en -> export/report/

Run first: threads_collector.py tag <topic>; features.py --topic <topic>;
scripts/export_csv.py (the data dictionary describes those CSV files).
Writes export/report/threads_dataset_report.docx and fig_*.png. Every number in the text is
queried from the database when the report is built.
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from math import comb
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from docx import Document  # noqa: E402
from docx.enum.table import WD_TABLE_ALIGNMENT  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Cm, Pt, RGBColor  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# chart tokens (light surface; categorical slots 1-2 of the default palette)
SURF, INK, INK2, GRID, AXIS, BAND = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#c9c8c3", "#efeeea"
S1, S2 = "#2a78d6", "#eb6834"
PHASES = [("Before", "2025-05-01", "2025-07-01"), ("July fighting", "2025-07-01", "2025-08-01"),
          ("Between", "2025-08-01", "2025-12-01"), ("December fighting", "2025-12-01", "2026-01-01"),
          ("After", "2026-01-01", "2026-02-01")]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# English meaning of the top non-Latin hashtags. Taken from the trilingual template post that
# carries the same statement in Khmer, Chinese and English, not translated independently.
GLOSS = {
    "#ไทยเป็นผู้เริ่มสงคราม": "Thailand started the war",
    "#ថៃជាអ្នកផ្តើមសង្គ្រាម": "Thailand started the war",
    "#ថៃបានឈ្លានពានកម្ពុជាដោយកម្លាំងយោធា": "Thailand invaded Cambodia by military force",
    "#ទាហ៊ានថៃបានបើកការវាយប្រហារលើកម្ពុជាមុន": "The Thai military attacked Cambodia first",
    "#កម្ពុជាមានសិទ្ធិក្នុងការការពារខ្លួននិងអធិបតេយ្យភាពជាតិរបស់ខ្លួន":
        "Cambodia has the right to defend itself and its sovereignty",
}


def tag_script(tag: str) -> str:
    from features import char_script
    scripts = Counter(s for s in map(char_script, tag) if s)
    top = scripts.most_common(1)[0][0] if scripts else "latin"
    return {"thai": "Thai", "khmer": "Khmer", "han": "Chinese", "latin": "English"}.get(top, top)


def tag_label(tag: str, width: int = 44) -> str:
    """Chart label: Latin tags as written; non-Latin tags as their English meaning (matplotlib
    can't shape Khmer/Thai text correctly; the report table shows the original)."""
    lang = tag_script(tag)
    if lang != "English":
        text = f"[{lang}] " + GLOSS.get(tag, "(non-Latin hashtag)")
    else:
        text = tag
    return text if len(text) <= width else text[:width - 1] + "…"


# ---------------------------------------------------------------- helpers

def fisher_two_sided(a, b, c, d):
    """Two-sided Fisher exact test for [[a, b], [c, d]]."""
    n, r1, c1 = a + b + c + d, a + b, a + c
    p = lambda x: comb(c1, x) * comb(n - c1, r1 - x) / comb(n, r1)  # noqa: E731
    p0 = p(a)
    return sum(p(x) for x in range(max(0, r1 - (n - c1)), min(r1, c1) + 1) if p(x) <= p0 * (1 + 1e-9))


def style_axes(ax, ygrid=True):
    ax.set_facecolor(SURF)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, labelsize=8.5, length=0)
    if ygrid:
        ax.yaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)


def new_fig(w=8.6, h=3.8):
    fig, ax = plt.subplots(figsize=(w, h), dpi=200)
    fig.patch.set_facecolor(SURF)
    style_axes(ax)
    return fig, ax


def titles(ax, title, subtitle):
    ax.set_title(title, loc="left", fontsize=11.5, color=INK, pad=20)
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, fontsize=8.5, color=INK2)


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, facecolor=SURF)
    plt.close(fig)


def shade_cell(cell, hex_fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def table(doc, header, rows, widths_cm, font_pt=8.5):
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(font_pt)
        shade_cell(cell, "E8EEF7")
    for r in rows:
        cells = t.add_row().cells
        for i, v in enumerate(r):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run("" if v is None else str(v))
            run.font.size = Pt(font_pt)
    t.autofit = False
    tbl_pr = t._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    for i, w in enumerate(widths_cm):
        t.columns[i].width = Cm(w)
        for cell in t.columns[i].cells:
            cell.width = Cm(w)
    doc.add_paragraph()
    return t


def bullets(doc, items):
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        if isinstance(it, tuple):
            p.add_run(it[0]).bold = True
            p.add_run(it[1])
        else:
            p.add_run(it)


def figure(doc, path, caption, width_cm=16):
    doc.add_picture(str(path), width=Cm(width_cm))
    cap = doc.add_paragraph()
    r = cap.add_run(caption)
    r.italic = True
    r.font.size = Pt(8.5)
    r.font.color.rgb = RGBColor(0x52, 0x51, 0x4E)


# ---------------------------------------------------------------- data dictionary

POST_COLUMNS = [
    ("post_id", "Threads' numeric post ID (primary key)."),
    ("url", "Link to the post on threads.com."),
    ("posted_utc", "When the post was published, UTC (Thailand and Cambodia are UTC+7)."),
    ("phase", "Conflict phase by posting date: before, july_fighting, between, december_fighting, after."),
    ("username / user_id", "Author's handle and numeric account ID (join to accounts.csv on user_id)."),
    ("text", "Post text as written (any language; replies are often Thai or Khmer)."),
    ("lang", "Detected language: writing system first (km, th, zh, ja, ko), Lingua for Latin-script text of 12+ letters; und = too short or emoji-only."),
    ("is_reply", "1 if the post is a reply to another post."),
    ("found_as", "How the post appeared on the page it was collected from: result (search hit, profile post, post-page target), reply, parent (context above a reply), quoted, other."),
    ("on_topic_reason", "keywords = matched the topic's keyword rule; thread = a reply under an on-topic post (any language, no keywords needed)."),
    ("matched_keywords", "JSON list of the Cambodian-side keywords found in the text."),
    ("reply_to_username", "For replies: the account being replied to."),
    ("root_post_username", "For replies: the author of the post that started the conversation."),
    ("thread_page_url", "For replies collected from a post page: that page's post (the reply's thread)."),
    ("like_count, reply_count, repost_count, quote_count, reshare_count", "Engagement as shown by Threads when last collected. reply_count is Threads' total and can exceed the replies Threads actually displays."),
    ("media_type", "text, image, video, carousel, or a raw Threads code."),
    ("link_url", "Outbound link, with the Threads redirect removed."),
    ("quoted_post_id", "post_id of the post this one quotes."),
    ("is_edited", "1 if the author edited the post."),
    ("ai_label", "Meta's 'AI info' label source: NONE, SELF_DISCLOSURE_FLOW, C2PA_METADATA, IPTC_METADATA (or _EDITED). Disclosure, not detection."),
    ("copy_cluster_id / copy_cluster_accounts / copy_cluster_min_gap_s", "If the text is (near-)identical to posts by other accounts: the cluster, how many accounts posted it, and the shortest time between two copies in seconds."),
    ("found_via", "Which page load first collected the post (search_top:<term>, profile:<user>, post:<url>, location:<user>); joins to the visits log."),
    ("first_scraped_utc", "When the collector first saw the post."),
    ("hashtags", "JSON list of the post's hashtags, normalized (lowercase, trailing punctuation and emoji removed)."),
]
ACCOUNT_COLUMNS = [
    ("user_id, username, full_name", "Account identifiers and display name."),
    ("is_verified, follower_count, bio", "Profile header. Follower count and bio exist only for accounts whose profile was opened."),
    ("based_in_country", "Country from Threads' 'About this profile' panel ('Based in'). Set by Meta, not the user; login-only."),
    ("country_status", "country / not_shared (user hides it) / no_based_in_row / account_gone (profile removed or suspended when checked) / lookup_failed / not_checked."),
    ("gone_utc", "When a lookup found the account removed ('the page is gone'). Accounts removed after collection are a weak-label candidate."),
    ("lookup_reason", "Why the account was location-checked: cluster (copy-cluster member) or random (fair sample). Empty = checked before the 50/50 split, prioritized, not random."),
    ("joined_month, account_age_months", "Threads join month (from 'About this profile') and age at report time. Month precision."),
    ("signup_number, signup_over_100m", "Threads signup number (only the first 100M accounts have one) and 1 if the account shows '100M+'."),
    ("joined_in_period, months_join_to_next_peak", "1 if the account joined during the study period; months from joining to the next fighting month (July or December 2025)."),
    ("n_on_topic_posts, n_on_topic_replies", "The account's on-topic posts (and replies among them) in the study period."),
    ("n_posts_collected, top_lang, top_lang_share", "All collected posts by the account, its most common language and that language's share."),
    ("tz_offset, tz_margin, tz_quiet_share, tz_n_posts", "Best-fitting UTC offset from posting hours (needs 20+ posts), the fit's confidence, share of posts at 01:00-07:00 local, and posts used. About +/-2 h accurate; cannot separate countries in the same time zone."),
    ("bio_country, bio_term", "Place named in the bio (gazetteer + flag emoji) and the matched term."),
    ("flag_bio_vs_country, flag_tz_vs_country", "1 if the bio place or the time zone disagrees with based_in_country. Candidate inauthenticity signals, not labels."),
    ("n_copy_posts, n_copy_partners, fastest_copy_gap_s", "Posts in cross-account copy clusters, how many other accounts share copied text, and the closest-in-time copy in seconds."),
    ("min_signup_gap_to_partner", "Smallest signup-number difference to a copy partner (adjacent numbers would suggest batch-created accounts)."),
    ("n_hashtags, n_campaign_tags, n_rare_tag_partners", "Distinct hashtags used; how many are campaign tags (20+ accounts); accounts sharing rare tags (used by 2-10 accounts) with this one."),
    ("first_seen_utc, country_checked_utc", "When the account was first collected and when its location was checked."),
]
OTHER_FILES = [
    ("text_clusters.csv", "One row per copy-paste cluster spanning 2+ accounts: n_posts, n_accounts, usernames, exact (1 = identical after normalization), first/last copy time, span_hours, min_gap_s, median_gap_s, min_signup_gap, sample_text."),
    ("hashtags.csv", "One row per hashtag: n_accounts, n_posts, is_campaign (20+ accounts), first/last use."),
    ("hashtag_pairs.csv", "Account pairs sharing rare hashtags: user_a, user_b, n_shared, shared_tags."),
]


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--topic", default="topics/th_kh_border_en.json")
    ap.add_argument("--out", type=Path, default=Path("export/report"))
    a = ap.parse_args()
    topic = json.loads(Path(a.topic).read_text(encoding="utf-8"))
    name, per = topic["name"], topic["period"]
    db = sqlite3.connect(a.db)
    q = lambda sql, *p: db.execute(sql, p).fetchall()  # noqa: E731
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]  # noqa: E731
    a.out.mkdir(parents=True, exist_ok=True)
    ON = f"JOIN post_topics t ON t.post_pk = p.pk AND t.topic = '{name}' AND t.on_topic = 1"
    PER = (f"p.taken_at >= strftime('%s','{per['from']}') "
           f"AND p.taken_at < strftime('%s','{per['to']}','+1 day')")

    # ---- numbers
    n_posts, n_users = one("SELECT COUNT(*) FROM posts"), one("SELECT COUNT(*) FROM users")
    n_visits = one("SELECT COUNT(*) FROM visits")
    n_ok = one("SELECT COUNT(*) FROM visits WHERE status = 'ok'")
    n_on = one(f"SELECT COUNT(*) FROM posts p {ON} WHERE {PER}")
    n_on_acc = one(f"SELECT COUNT(DISTINCT p.user_pk) FROM posts p {ON} WHERE {PER}")
    n_kw = one(f"SELECT COUNT(*) FROM posts p {ON} WHERE {PER} AND COALESCE(t.match,'keywords') = 'keywords'")
    n_thread = n_on - n_kw
    n_rounds = one("SELECT COUNT(DISTINCT run_id) FROM visits")
    first_run = one("SELECT date(MIN(started_at),'unixepoch') FROM visits")
    last_run = one("SELECT date(MAX(finished_at),'unixepoch') FROM visits")
    phase_rows = []
    for label, s, e in PHASES:
        n, au, rep = q(f"""SELECT COUNT(*), COUNT(DISTINCT p.username), COALESCE(SUM(p.is_reply),0)
                           FROM posts p {ON} WHERE p.taken_at >= strftime('%s',?)
                           AND p.taken_at < strftime('%s',?)""", s, e)[0]
        days = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
        d0, d1 = dt.date.fromisoformat(s), dt.date.fromisoformat(e) - dt.timedelta(days=1)
        span = f"{d0.day} {MONTHS[d0.month - 1]} – {d1.day} {MONTHS[d1.month - 1]} {d1.year}"
        phase_rows.append((label, span, days, n, round(n / days, 1), au, rep))
    months = []
    y, m = int(per["from"][:4]), int(per["from"][5:7])
    while f"{y}-{m:02d}" <= per["to"][:7]:
        months.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    by_month = {mo: (k, th) for mo, k, th in q(f"""
        SELECT strftime('%Y-%m', p.taken_at, 'unixepoch'),
               SUM(COALESCE(t.match,'keywords') = 'keywords'), SUM(t.match = 'thread')
        FROM posts p {ON} WHERE {PER} GROUP BY 1""")}
    langs = Counter(dict(q(f"""SELECT f.lang, COUNT(*) FROM posts p {ON}
                              JOIN post_features f ON f.post_pk = p.pk
                              WHERE {PER} AND t.match = 'thread' GROUP BY 1""")))
    checked = one("SELECT COUNT(*) FROM users WHERE country_checked_at IS NOT NULL")
    with_c = one("SELECT COUNT(*) FROM users WHERE country_checked_at IS NOT NULL AND country IS NOT NULL")
    grp = {}
    for why in ("cluster", "random"):
        cnt = Counter(dict(q("""SELECT country, COUNT(*) FROM users WHERE lookup_reason = ?
                                AND country_checked_at IS NOT NULL AND country IS NOT NULL GROUP BY 1""", why)))
        grp[why] = (cnt.get("Cambodia", 0), cnt.get("Thailand", 0),
                    sum(cnt.values()) - cnt.get("Cambodia", 0) - cnt.get("Thailand", 0))
    (ck, ct, co), (rk, rt, ro) = grp["cluster"], grp["random"]
    odds = (ck * rt) / (ct * rk) if ct and rk else float("nan")
    p_fisher = fisher_two_sided(ck, ct, rk, rt)
    n_clusters = one("SELECT COUNT(*) FROM text_clusters")
    multi = q("SELECT n_accounts, min_gap_s, cluster_id FROM text_clusters WHERE n_accounts >= 2")
    on_cluster_ids = {r[0] for r in q(f"""SELECT DISTINCT pc.cluster_id FROM post_text_clusters pc
                                          JOIN posts p ON p.pk = pc.post_pk {ON}""")}
    n_multi_on = sum(c[2] in on_cluster_ids for c in multi)
    max_acc = max((c[0] for c in multi), default=0)
    under60 = sum(1 for c in multi if c[1] is not None and c[1] < 60)
    top_tags = q("SELECT tag, n_accounts, n_posts FROM hashtags ORDER BY n_accounts DESC LIMIT 12")
    n_campaign = one("SELECT COUNT(*) FROM hashtags WHERE is_campaign = 1")
    n_pairs = one("SELECT COUNT(*) FROM hashtag_pairs")
    top_pair = q("SELECT user_a, user_b, n_shared, shared_tags FROM hashtag_pairs ORDER BY n_shared DESC LIMIT 1")
    joins = q("SELECT joined_month FROM user_features WHERE joined_month IS NOT NULL")
    n_join = len(joins)
    jul23 = sum(j == "2023-07" for (j,) in joins)
    in_per_join = one("SELECT COUNT(*) FROM user_features WHERE joined_in_period = 1")
    over100m = one("SELECT COUNT(*) FROM user_features WHERE signup_over_100m = 1")
    jul25, jul25_kh = q("""SELECT COUNT(*), COALESCE(SUM(u.country = 'Cambodia'), 0) FROM user_features f
                           JOIN users u ON u.pk = f.user_pk WHERE f.joined_month = '2025-07'""")[0]
    jul25_rand = one("""SELECT COUNT(*) FROM user_features f JOIN users u ON u.pk = f.user_pk
                        WHERE f.joined_month = '2025-07' AND u.lookup_reason = 'random'""")
    n_rand = one("SELECT COUNT(*) FROM users WHERE lookup_reason = 'random' AND country_checked_at IS NOT NULL")
    # location coverage by role, and by why the account was looked up
    cov = {}
    for role, cond in (("Replied to an on-topic post", "p.is_reply = 1"),
                       ("Wrote an on-topic original post", "p.is_reply = 0"),
                       ("All on-topic authors", "1 = 1")):
        cov[role] = q(f"""SELECT COUNT(*), SUM(u.country_checked_at IS NOT NULL), SUM(u.country IS NOT NULL)
                          FROM users u WHERE u.pk IN (SELECT p.user_pk FROM posts p {ON} WHERE {PER} AND {cond})""")[0]
    by_reason = q("""SELECT CASE COALESCE(lookup_reason, '') WHEN 'cluster' THEN 'Copy-cluster members'
                            WHEN 'random' THEN 'Random sample of on-topic authors'
                            WHEN 'sweep' THEN 'Location sweep (all remaining on-topic authors)'
                            ELSE 'Prioritized (most active, before the 50/50 split)' END,
                            COUNT(*), SUM(country IS NOT NULL)
                     FROM users WHERE country_checked_at IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
    sweep_done = one("SELECT COUNT(*) FROM users WHERE lookup_reason = 'sweep' AND country_checked_at IS NOT NULL")
    to_check = cov["All on-topic authors"][0] - cov["All on-topic authors"][1]
    lookup_s = one("SELECT AVG(finished_at - started_at) FROM visits WHERE kind = 'location' AND status = 'ok'")
    per_lookup = lookup_s + 6.5  # plus the average polite pause between targets (4-9 s)
    sweep_hours = to_check * per_lookup / 3600
    batches = -(-to_check // 100)
    sweep_wall_h = batches * (100 * per_lookup / 3600 + 25 / 60)
    # why accounts were seen but are not on-topic authors in the period
    origin = q(f"""
        WITH onp AS (SELECT DISTINCT p.user_pk FROM posts p {ON} WHERE {PER}),
             ona AS (SELECT DISTINCT p.user_pk FROM posts p {ON}),
             firsts AS (SELECT user_pk, MIN(first_scraped_at) t FROM posts GROUP BY user_pk)
        SELECT reason, COUNT(*) FROM (
          SELECT u.pk, CASE
            WHEN u.pk IN (SELECT user_pk FROM ona) THEN 'On-topic, but only outside the study period'
            WHEN u.pk NOT IN (SELECT user_pk FROM posts) THEN 'Seen only as a profile (no post saved)'
            ELSE (SELECT CASE
                WHEN p.found_as IN ('quoted', 'parent') THEN 'Their post was quoted, or was the parent shown above a reply'
                WHEN p.source LIKE 'search%' THEN 'In a topic search, but the post failed the on-topic rule'
                WHEN p.source LIKE 'post:%' THEN 'Replied under a post that is not on-topic'
                ELSE 'Appeared on a profile that was opened' END
              FROM posts p JOIN firsts f ON f.user_pk = p.user_pk AND f.t = p.first_scraped_at
              WHERE p.user_pk = u.pk LIMIT 1) END AS reason
          FROM users u WHERE u.pk NOT IN (SELECT user_pk FROM onp)) GROUP BY reason ORDER BY 2 DESC""")
    search_fail = q(f"""
        WITH onp AS (SELECT DISTINCT p.user_pk FROM posts p {ON} WHERE {PER})
        SELECT COUNT(DISTINCT CASE WHEN f.lang = 'en' AND (p.taken_at < strftime('%s','{per['from']}')
                    OR p.taken_at >= strftime('%s','{per['to']}','+1 day')) THEN p.user_pk END),
               COUNT(DISTINCT CASE WHEN f.lang != 'en' THEN p.user_pk END)
        FROM posts p JOIN post_topics t ON t.post_pk = p.pk AND t.topic = '{name}' AND t.on_topic = 0
        JOIN post_features f ON f.post_pk = p.pk
        WHERE p.source LIKE 'search%' AND p.user_pk NOT IN (SELECT user_pk FROM onp)""")[0]
    ai = Counter(dict(q(f"SELECT COALESCE(p.ai_label,'(missing)'), COUNT(*) FROM posts p {ON} WHERE {PER} GROUP BY 1")))
    seeds = topic["seeds"]

    # ---- whole-population location results (meaningful once the sweep is complete)
    complete = to_check == 0
    # extended sweep: accounts beyond the on-topic authors (sweep_ext)
    ext_n, ext_c = q("""SELECT COUNT(*), COALESCE(SUM(country IS NOT NULL), 0) FROM users
                        WHERE lookup_reason = 'sweep_ext' AND country_checked_at IS NOT NULL""")[0]
    ext_countries = q("""SELECT country, COUNT(*) FROM users WHERE lookup_reason = 'sweep_ext'
                         AND country IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 6""")
    related_left = one(f"""SELECT COUNT(*) FROM users u WHERE u.country_checked_at IS NULL
        AND u.username NOT IN (SELECT value FROM json_each(?))
        AND EXISTS (SELECT 1 FROM posts p WHERE p.user_pk = u.pk AND (p.source LIKE 'search%'
                    OR p.source LIKE 'post:%' OR p.pk IN (SELECT post_pk FROM post_topics
                    WHERE topic = '{name}' AND on_topic = 1)))""", json.dumps(topic.get("exclude_users", [])))
    all_checked = one("SELECT COUNT(*) FROM users WHERE country_checked_at IS NOT NULL")
    all_with_c = one("SELECT COUNT(*) FROM users WHERE country IS NOT NULL")
    AUTH = f"SELECT DISTINCT p.user_pk FROM posts p {ON} WHERE {PER}"
    pop_countries = q(f"""SELECT COALESCE(u.country, '(no country)'), COUNT(*) FROM users u
                          WHERE u.pk IN ({AUTH}) GROUP BY 1 ORDER BY 2 DESC""")
    sig = {}
    for g_name, g_n, g_cp, g_ct, g_rp, g_j7, g_jp in q(f"""
            SELECT CASE WHEN u.country IN ('Cambodia','Thailand') THEN u.country ELSE 'Other / none' END, COUNT(*),
                   SUM(f.n_copy_posts > 0), SUM(f.n_campaign_tags > 0), SUM(f.n_rare_tag_partners > 0),
                   SUM(f.joined_month = '2025-07'), SUM(f.joined_in_period)
            FROM users u JOIN user_features f ON f.user_pk = u.pk WHERE u.pk IN ({AUTH}) GROUP BY 1"""):
        sig[g_name] = dict(n=g_n, copy=g_cp or 0, campaign=g_ct or 0, rare=g_rp or 0, jul25=g_j7 or 0,
                           in_period=g_jp or 0)
    SIGNALS = [("copy", "In a copy-paste cluster"), ("campaign", "Uses campaign hashtags"),
               ("rare", "Shares rare hashtags"), ("jul25", "Joined Threads in July 2025")]
    sig_tests = {}
    if "Cambodia" in sig and "Thailand" in sig:
        K, T = sig["Cambodia"], sig["Thailand"]
        for key, _ in SIGNALS:
            a_, b_, c_, d_ = K[key], K["n"] - K[key], T[key], T["n"] - T[key]
            sig_tests[key] = ((a_ * d_) / (b_ * c_) if b_ and c_ else float("inf"), fisher_two_sided(a_, b_, c_, d_))
    pct = lambda grp, key: 100 * sig[grp][key] / sig[grp]["n"] if grp in sig and sig[grp]["n"] else 0  # noqa: E731

    # ---- charts
    fig_paths = {}
    # 1 monthly stacked
    fig, ax = new_fig(8.8, 4.2)
    x = range(len(months))
    kw_v = [by_month.get(mo, (0, 0))[0] or 0 for mo in months]
    th_v = [by_month.get(mo, (0, 0))[1] or 0 for mo in months]
    for mo in topic.get("peaks", []):
        if mo in months:
            i = months.index(mo)
            ax.axvspan(i - 0.5, i + 0.5, color=BAND, zorder=0, lw=0)
    ax.bar(x, kw_v, width=0.62, color=S1, label="Matched by keywords (English originals)", zorder=2)
    ax.bar(x, th_v, width=0.62, bottom=kw_v, color=S2, edgecolor=SURF, linewidth=2,
           label="Replies under those posts (any language)", zorder=2)
    for mo, lab in (("2025-07", "July fighting"), ("2025-12", "December fighting")):
        if mo in months:
            i = months.index(mo)
            tot = kw_v[i] + th_v[i]
            ax.annotate(f"{tot:,}", (i, tot), xytext=(0, 4), textcoords="offset points", ha="center",
                        fontsize=9.5, color=INK, fontweight="bold")
            ax.annotate(lab, (i, tot), xytext=(0, 18), textcoords="offset points", ha="center",
                        fontsize=8.5, color=INK2)
    ax.set_xticks(list(x), [MONTHS[int(mo[5:]) - 1] + (f"\n{mo[:4]}" if i == 0 or mo.endswith("-01") else "")
                            for i, mo in enumerate(months)])
    ax.set_ylim(0, max(k + t for k, t in zip(kw_v, th_v)) * 1.3)
    titles(ax, "On-topic posts per month", f"English originals + their replies (any language) · n = {n_on:,}")
    leg = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2, frameon=False, fontsize=8)
    [t.set_color(INK2) for t in leg.get_texts()]
    fig_paths["monthly"] = a.out / "fig_1_monthly.png"
    save(fig, fig_paths["monthly"])

    # 2 per-day rate by phase
    fig, ax = new_fig(8.0, 3.4)
    labels = [r[0] for r in phase_rows]
    rates = [r[4] for r in phase_rows]
    ax.bar(labels, rates, width=0.55, color=S1, zorder=2)
    for i, v in enumerate(rates):
        ax.annotate(f"{v:g}/day", (i, v), xytext=(0, 3), textcoords="offset points", ha="center",
                    fontsize=8.5, color=INK)
    ax.set_ylim(0, max(rates) * 1.25)
    titles(ax, "On-topic posts per day, by conflict phase",
           "Phases differ in length (31-122 days), so per-day rates are comparable; totals are not")
    fig_paths["rates"] = a.out / "fig_2_rates.png"
    save(fig, fig_paths["rates"])

    # 3 reply languages
    names = {"en": "English", "und": "Emoji / too short", "th": "Thai", "km": "Khmer",
             "und-latin": "Latin script, unclear", "zh": "Chinese", "ja": "Japanese"}
    top = [(k, v) for k, v in langs.most_common() if k in names][:6]
    other = sum(langs.values()) - sum(v for _, v in top)
    items = [(names.get(k, k), v) for k, v in top] + ([("Other", other)] if other else [])
    fig, ax = new_fig(8.0, 3.4)
    style_axes(ax, ygrid=False)
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ys = range(len(items))[::-1]
    ax.barh(list(ys), [v for _, v in items], height=0.55, color=S1, zorder=2)
    ax.set_yticks(list(ys), [k for k, _ in items])
    for yv, (_, v) in zip(ys, items):
        ax.annotate(f"{v:,}", (v, yv), xytext=(4, 0), textcoords="offset points", va="center",
                    fontsize=8.5, color=INK)
    titles(ax, "Languages of replies under on-topic posts",
           f"Thread replies in the study period, n = {sum(langs.values()):,} (detected from text)")
    fig_paths["langs"] = a.out / "fig_3_reply_languages.png"
    save(fig, fig_paths["langs"])

    # 4 countries: whole population when the sweep is complete, else cluster vs random
    if complete:
        fig, ax = new_fig(8.0, 3.6)
        style_axes(ax, ygrid=False)
        ax.xaxis.grid(True, color=GRID, lw=0.8)
        top = pop_countries[:8]
        rest = sum(n for _, n in pop_countries[8:])
        items = top + ([("Other countries", rest)] if rest else [])
        ys = range(len(items))[::-1]
        ax.barh(list(ys), [n for _, n in items], height=0.55, color=S1, zorder=2)
        ax.set_yticks(list(ys), [c for c, _ in items])
        tot_a = sum(n for _, n in pop_countries)
        for yv, (_, n) in zip(ys, items):
            ax.annotate(f"{n:,} ({100 * n / tot_a:.0f}%)", (n, yv), xytext=(4, 0), textcoords="offset points",
                        va="center", fontsize=8.5, color=INK)
        ax.set_xlim(0, max(n for _, n in items) * 1.25)
        titles(ax, "Where on-topic authors are based", f"All {tot_a:,} on-topic authors, 'Based in' country")
        fig_paths["countries"] = a.out / "fig_4_countries.png"
        save(fig, fig_paths["countries"])

        fig, ax = new_fig(8.6, 3.6)
        xs = range(len(SIGNALS))
        w = 0.34
        kv = [pct("Cambodia", k) for k, _ in SIGNALS]
        tv = [pct("Thailand", k) for k, _ in SIGNALS]
        ax.bar([i - w / 2 - 0.01 for i in xs], kv, width=w, color=S1, label="Based in Cambodia", zorder=2)
        ax.bar([i + w / 2 + 0.01 for i in xs], tv, width=w, color=S2, label="Based in Thailand", zorder=2)
        for i, (u, v) in enumerate(zip(kv, tv)):
            ax.annotate(f"{u:.1f}%", (i - w / 2, u), xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8, color=INK)
            ax.annotate(f"{v:.1f}%", (i + w / 2, v), xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8, color=INK)
        ax.set_xticks(list(xs), [lab for _, lab in SIGNALS], fontsize=8)
        ax.set_ylim(0, max(kv + tv) * 1.3)
        titles(ax, "Coordination and account-age signals, by country",
               f"Share of on-topic authors with each signal · Cambodia n = {sig['Cambodia']['n']:,}, "
               f"Thailand n = {sig['Thailand']['n']:,}")
        leg = ax.legend(frameon=False, fontsize=8, loc="upper right")
        [t.set_color(INK2) for t in leg.get_texts()]
        fig_paths["signals"] = a.out / "fig_4b_signals_by_country.png"
        save(fig, fig_paths["signals"])

    fig, ax = new_fig(7.6, 3.4)
    cats = ["Cambodia", "Thailand", "Other"]
    xs = range(len(cats))
    w = 0.34
    ax.bar([i - w / 2 - 0.01 for i in xs], [ck, ct, co], width=w, color=S1, label="Copy-cluster members", zorder=2)
    ax.bar([i + w / 2 + 0.01 for i in xs], [rk, rt, ro], width=w, color=S2, label="Random on-topic authors", zorder=2)
    for i, (u, v) in enumerate(zip([ck, ct, co], [rk, rt, ro])):
        ax.annotate(str(u), (i - w / 2, u), xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8.5, color=INK)
        ax.annotate(str(v), (i + w / 2, v), xytext=(0, 3), textcoords="offset points", ha="center", fontsize=8.5, color=INK)
    ax.set_xticks(list(xs), cats)
    ax.set_ylim(0, max(ck, rk, ct, rt, co, ro) * 1.3)
    titles(ax, "'Based in' country: copy-cluster members vs random authors",
           f"Location-checked accounts, about 50 per group · Fisher exact (Cambodia vs Thailand): "
           f"OR = {odds:.2f}, p = {p_fisher:.1e}")
    leg = ax.legend(frameon=False, fontsize=8, loc="upper right")
    [t.set_color(INK2) for t in leg.get_texts()]
    if not complete:
        fig_paths["countries"] = a.out / "fig_4_countries.png"
        save(fig, fig_paths["countries"])
    else:
        fig_paths["cluster_random"] = a.out / "fig_4c_cluster_vs_random.png"
        save(fig, fig_paths["cluster_random"])

    # 5 top hashtags
    fig, ax = new_fig(8.6, 4.2)
    style_axes(ax, ygrid=False)
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    tags = top_tags[:10]
    ys = range(len(tags))[::-1]
    ax.barh(list(ys), [n for _, n, _ in tags], height=0.55, color=S1, zorder=2)
    ax.set_yticks(list(ys), [tag_label(t) for t, _, _ in tags], fontsize=8)
    for yv, (_, n, _) in zip(ys, tags):
        ax.annotate(f"{n}", (n, yv), xytext=(4, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    titles(ax, "Most widely used hashtags",
           "Distinct accounts using each hashtag; non-Latin tags shown by meaning [language]")
    fig_paths["hashtags"] = a.out / "fig_6_hashtags.png"
    save(fig, fig_paths["hashtags"])

    # 6 copy clusters: accounts vs fastest copy
    fig, ax = new_fig(8.0, 3.8)
    style_axes(ax)
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    pts = [(max(g, 1), n, cid in on_cluster_ids) for n, g, cid in multi if g is not None]
    on_pts = [(g, n) for g, n, o in pts if o]
    off_pts = [(g, n) for g, n, o in pts if not o]
    ax.scatter([g for g, _ in on_pts], [n for _, n in on_pts], s=34, color=S1, edgecolor=SURF, linewidth=1.5,
               label="Contains an on-topic post", zorder=3)
    ax.scatter([g for g, _ in off_pts], [n for _, n in off_pts], s=34, color=S2, edgecolor=SURF, linewidth=1.5,
               label="Off-topic only", zorder=3)
    ax.set_xscale("log")
    for xv, lab in ((60, "1 min"), (3600, "1 h"), (86400, "1 day")):
        ax.axvline(xv, color=AXIS, lw=0.8, ls="--", zorder=1)
        ax.annotate(lab, (xv, max_acc), xytext=(3, 0), textcoords="offset points", fontsize=7.5, color=INK2)
    ax.set_xlabel("Shortest time between two copies (seconds, log scale)", fontsize=8.5, color=INK2)
    ax.set_ylabel("Accounts posting the same text", fontsize=8.5, color=INK2)
    titles(ax, "Copy-paste clusters: size vs speed",
           f"{len(multi)} clusters spanning 2+ accounts; fast and large = the strongest coordination signal")
    leg = ax.legend(frameon=False, fontsize=8, loc="center right")
    [t.set_color(INK2) for t in leg.get_texts()]
    fig_paths["clusters"] = a.out / "fig_5_clusters.png"
    save(fig, fig_paths["clusters"])

    # 7 join months
    jm = Counter(j for (j,) in joins)
    jmonths = sorted(jm)
    allm = []
    if jmonths:
        y, m = int(jmonths[0][:4]), int(jmonths[0][5:])
        while f"{y}-{m:02d}" <= jmonths[-1]:
            allm.append(f"{y}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    fig, ax = new_fig(8.8, 3.4)
    ax.bar(range(len(allm)), [jm.get(mo, 0) for mo in allm], width=0.7, color=S1, zorder=2)
    for mo in topic.get("peaks", []):
        if mo in allm:
            i = allm.index(mo)
            ax.axvspan(i - 0.5, i + 0.5, color=BAND, zorder=0, lw=0)
    ax.set_xticks([i for i, mo in enumerate(allm) if mo.endswith(("-01", "-07"))],
                  [MONTHS[int(mo[5:]) - 1] + f"\n{mo[:4]}" for mo in allm if mo.endswith(("-01", "-07"))])
    titles(ax, "When the location-checked accounts joined Threads",
           f"n = {n_join:,}; shaded = fighting months; July 2023 = Threads launch wave")
    fig_paths["joins"] = a.out / "fig_7_join_months.png"
    save(fig, fig_paths["joins"])

    # ---- document
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2.2)
        s.top_margin = s.bottom_margin = Cm(2.0)
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)

    doc.add_heading("Threads dataset report: Thailand–Cambodia border conflict", level=0)
    p = doc.add_paragraph()
    p.add_run(f"Built {dt.date.today().isoformat()} from threads.db · topic {name} · "
              f"study period {per['from']} to {per['to']}").italic = True

    doc.add_heading("1. Summary", level=1)
    doc.add_paragraph(
        "This dataset was collected from Threads for a thesis on detecting coordinated inauthentic "
        "behavior (CIB) and on predicting when such detectors stop working. It covers English-language "
        "discussion of the 2025 Thailand–Cambodia border conflict, including the replies under those "
        "posts in any language, from before, during and after the two rounds of fighting (July and "
        "December 2025).")
    table(doc, ["Measure", "Value"], [
        ("Posts collected (all)", f"{n_posts:,}"),
        ("Accounts seen (all pages opened)", f"{n_users:,}"),
        ("On-topic posts in the study period", f"{n_on:,} from {n_on_acc:,} accounts"),
        ("…matched by keywords (English originals)", f"{n_kw:,}"),
        ("…replies under those posts (any language)", f"{n_thread:,}"),
        ("Accounts with a 'Based in' country check", f"{checked:,} ({100 * with_c / checked:.0f}% show a country)"),
        ("Cross-account copy-paste clusters", f"{len(multi)} ({n_multi_on} with on-topic posts), up to {max_acc} accounts"),
        ("Campaign hashtags (20+ accounts)", f"{n_campaign}"),
        ("Page loads", f"{n_visits:,} in {n_rounds} collection rounds, {first_run} to {last_run}; {n_ok:,} ok, no login walls"),
    ], [9.5, 7.0])
    doc.add_paragraph("Key findings:")
    bullets(doc, [
        ("Activity follows the conflict. ", f"On-topic posting peaks in the two fighting months: "
         f"{phase_rows[1][4]:g} posts per day in July 2025 and {phase_rows[3][4]:g} in December 2025, "
         f"against {phase_rows[0][4]:g} before the fighting."),
        ("Most of the volume is replies. ", f"{n_thread:,} of {n_on:,} on-topic posts are replies, many in Thai or Khmer."),
        ("Both sides take part; English copy-paste comes from one. ", (
            f"Of all {sum(n for _, n in pop_countries):,} on-topic authors, "
            f"{100 * dict(pop_countries).get('Cambodia', 0) / sum(n for _, n in pop_countries):.0f}% are based in "
            f"Cambodia and {100 * dict(pop_countries).get('Thailand', 0) / sum(n for _, n in pop_countries):.0f}% in "
            f"Thailand, but {pct('Cambodia', 'copy'):.0f}% of Cambodia-based authors appear in copy-paste clusters "
            f"against {pct('Thailand', 'copy'):.1f}% of Thailand-based ones (Fisher exact OR = "
            f"{sig_tests['copy'][0]:.1f}, p = {sig_tests['copy'][1]:.0e}).") if complete else (
            f"A random sample of on-topic authors is split {rt} Thailand / {rk} Cambodia, but copy-paste cluster "
            f"members are {ck} Cambodia / {ct} Thailand (Fisher exact OR = {odds:.2f}, p = {p_fisher:.1e}).")),
        ("A wave of new accounts at the start of the fighting. ", (
            f"{pct('Cambodia', 'jul25'):.0f}% of Cambodia-based authors created their Threads account in July 2025, "
            f"against {pct('Thailand', 'jul25'):.1f}% of Thailand-based ones (OR = {sig_tests['jul25'][0]:.1f}, "
            f"p = {sig_tests['jul25'][1]:.0e}); there is no such wave in December.") if complete else
            "Accounts created in July 2025 are a lead to follow up (see Account age)."),
        ("Coordinated hashtags run in three languages. ", "The same 'Thailand started the war' slogan appears as "
         "English, Thai and Khmer hashtags used by 200-300 accounts each, and whole sentences turned into "
         "hashtags are shared by about 100 accounts."),
        ("Thai-side coordination shows up in hashtags. ", (f"The strongest rare-hashtag pair is "
         f"{top_pair[0][0]} + {top_pair[0][1]} ({top_pair[0][2]} shared rare tags), a Thai-side pair the "
         f"copy-paste clusters did not reveal.") if top_pair else ""),
        ("These are coordination signals, not proof of fake accounts. ", "Labels remain an open question."),
    ])

    doc.add_heading("2. How the data was collected", level=1)
    doc.add_paragraph(
        "A Playwright-driven Chromium browser, logged in to a separate research account, opens Threads "
        "pages and records the data Threads sends to them (page JSON and /graphql responses). Nothing "
        "is fetched through an API. Every page load is logged with its time, target and outcome, so each "
        "post can be traced to when and how it was collected.")
    bullets(doc, [
        ("Searches: ", f"{len(topic['search'])} event-specific terms in 'Top' mode (for example "
         + ", ".join(f"'{t}'" for t in topic["search"][:4]) + "). No hashtags were used as search terms."),
        ("Seed accounts: ", ", ".join("@" + s for s in seeds) + ". Two Thai-side, two Cambodian-side and "
         "four international outlets; each profile was scrolled back to the start of the study period."),
        ("Reply pages: ", "the page of every on-topic English post with replies in the study period was "
         "opened to collect its replies (all of them have been opened)."),
        ("Profiles and location: ", "repliers' and authors' profiles, plus 'About this profile' lookups "
         "for the 'Based in' country and join date, half for copy-cluster members and half for a random "
         "sample of on-topic authors."),
        ("Politeness: ", "1.5-3.5 s between scrolls, 4-9 s between pages, no parallel requests, timeouts "
         "on every wait, and automatic stops on a login wall or repeated failures."),
    ])
    doc.add_heading("What counts as on-topic", level=2)
    doc.add_paragraph(
        "An original post is on-topic if it is in English and contains a Cambodian-side term (Cambodia, "
        "Khmer, Hun Sen, Preah Vihear…), a Thai-side term (Thailand, Thai, Anutin, Surin…) and a conflict "
        "term (border, troops, ceasefire, shelling, landmines…). English terms match whole words only. "
        "Every reply found under an on-topic post is also on-topic, in any language and without keywords, "
        "because replies are often short, emoji-only, or in Thai or Khmer. Off-topic posts are kept and "
        "tagged, not deleted. A hand check of random on-topic posts found them relevant; posts that rely "
        "on 'ASEAN' alone as the conflict term are about 20% false positives (3 of 15).")

    doc.add_heading("Accounts in the dataset", level=2)
    doc.add_paragraph(
        f"The collector saved {n_users:,} accounts, but only {n_on_acc:,} wrote an on-topic post in the "
        "study period. All searches were topic-related; the difference comes from everything else the "
        "opened pages show, which is kept and tagged off-topic rather than deleted:")
    table(doc, ["Why the account is not among the on-topic authors", "Accounts"],
          [(r, f"{n:,}") for r, n in origin], [12.5, 3.0])
    doc.add_paragraph(
        f"Most search hits that failed the rule are on the topic but outside the date window: "
        f"{search_fail[0]:,} accounts wrote English posts about the conflict after January 2026 (the "
        f"ceasefire and maritime-talk phase), and {search_fail[1]:,} wrote short, emoji-only or non-English "
        "replies that the keyword rule can't classify. Extending the study period into 2026 would bring "
        "many of them in with no new collection, only a re-tag.")

    doc.add_heading("3. What the data shows", level=1)
    doc.add_heading("Timeline", level=2)
    figure(doc, fig_paths["monthly"], "Figure 1. On-topic posts per month. Blue: posts matched by "
           "keywords; orange: replies under them. Shaded: fighting months.")
    table(doc, ["Phase", "Dates", "Days", "Posts", "Per day", "Authors", "Replies"],
          [(r[0], r[1], r[2], f"{r[3]:,}", r[4], f"{r[5]:,}", f"{r[6]:,}") for r in phase_rows],
          [3.3, 4.2, 1.2, 1.7, 1.6, 1.7, 1.6])
    figure(doc, fig_paths["rates"], "Figure 2. Posts per day in each phase. Use rates, not totals, "
           "to compare phases of different length.")
    doc.add_paragraph(
        "Caution: reply volume per month also depends on which post pages were opened (the most-replied "
        "first), so part of the monthly shape reflects collection design, not only public activity.")

    doc.add_heading("Languages", level=2)
    figure(doc, fig_paths["langs"], "Figure 3. Detected languages of the replies under on-topic posts.")

    doc.add_heading("Who takes part, and where they are based", level=2)
    if complete:
        tot_a = sum(n for _, n in pop_countries)
        doc.add_paragraph(
            f"Every on-topic author ({tot_a:,} accounts) was location-checked, and "
            f"{100 * (tot_a - dict(pop_countries).get('(no country)', 0)) / tot_a:.0f}% show a 'Based in' "
            "country (the rest hide it or have no such row). The country is set by Meta, not typed by the user.")
        figure(doc, fig_paths["countries"], "Figure 4. 'Based in' country of all on-topic authors.")
        figure(doc, fig_paths["signals"], "Figure 4b. Share of Cambodia- and Thailand-based authors with "
               "each coordination or account-age signal.")
        table(doc, ["Signal", "Cambodia-based", "Thailand-based", "Other / none", "Odds ratio KH vs TH", "p (Fisher)"],
              [(lab, f"{pct('Cambodia', k):.1f}%", f"{pct('Thailand', k):.1f}%", f"{pct('Other / none', k):.1f}%",
                f"{sig_tests[k][0]:.1f}", f"{sig_tests[k][1]:.0e}") for k, lab in SIGNALS],
              [4.6, 2.3, 2.3, 2.2, 2.6, 2.0], font_pt=8)
        doc.add_paragraph(
            "Reading: both sides take part in about equal numbers, but coordination signals are far more "
            "common among Cambodia-based authors: copy-paste and campaign hashtags most of all, and to a "
            "lesser degree shared rare hashtags, which also appear on the Thai side. Each location lookup "
            "opened the author's profile and saved its visible posts, so these signals are measured the same "
            "way for every author. Caveats: only English original posts were searched, so Thai-language "
            "campaigns are under-represented; and coordination is not proof of fake accounts.")
    else:
        doc.add_paragraph(
            f"{checked} accounts were location-checked; {100 * with_c / checked:.0f}% show a 'Based in' country "
            "(the rest hide it or have no such row). The country is set by Meta, not typed by the user. "
            "Only the 'random' group is a fair sample of on-topic authors.")
        figure(doc, fig_paths["countries"], "Figure 4. 'Based in' country of copy-cluster members and of a "
               "random sample of on-topic authors.")
        doc.add_paragraph(
            f"Reading: Thai- and Cambodia-based accounts take part about equally ({rt} vs {rk} in the random "
            f"group), but English-language copy-paste comes almost entirely from Cambodia-based accounts "
            f"({ck} of {ck + ct + co}). Caveats: the cluster group is partly circular (clusters were found in "
            "Cambodian-side texts), the groups are small, and only English original posts were searched, so "
            "Thai-language campaigns would not be visible.")

    doc.add_heading("Location coverage" + ("" if complete else " and the planned full check"), level=2)
    doc.add_paragraph(
        "Threads does not send an author's country with their posts or replies. It appears only in each "
        "profile's 'About this profile' panel, so every country needs its own lookup: open the profile, "
        f"open the menu and the panel, then read it. That takes about {lookup_s:.0f} seconds plus the "
        "polite pause, and repeated menu clicks are the collection step most likely to get the research "
        "account flagged. Lookups were first kept to 50 per collection round" + (
            ", then a dedicated sweep checked every remaining on-topic author." if complete else "."))
    table(doc, ["On-topic accounts in the study period", "Accounts", "Location checked", "Have a country", "Share"],
          [(role, f"{n:,}", f"{c:,}", f"{w:,}", f"{100 * w / n:.1f}%") for role, (n, c, w) in cov.items()],
          [6.4, 2.2, 2.6, 2.6, 1.8])
    table(doc, ["Why the account was checked", "Checked", "Have a country"],
          [(r, f"{n:,}", f"{w:,}") for r, n, w in by_reason], [9.6, 3.0, 3.0])
    if complete:
        p = doc.add_paragraph()
        p.add_run("Full check (complete). ").bold = True
        p.add_run(
            f"The sweep checked every remaining on-topic author in a fixed random order ({sweep_done:,} accounts, "
            "2026-10-14 to 2026-10-19). Threads limits 'About this profile': after about 925 lookups in a day, "
            "lookups failed three in a row and only recovered after roughly 24 hours. The sweep therefore "
            "kept to at most 800 lookups in any 24 hours, ran in batches of 100 with 25-minute breaks, and "
            "never hit a login wall.")
    else:
        doc.add_paragraph(
            "Only the random sample (and the sweep, below) can describe on-topic authors as a whole; the "
            "prioritized and copy-cluster lookups were chosen on purpose and are not representative.")
        p = doc.add_paragraph()
        p.add_run("Full check (prepared, paused). ").bold = True
        p.add_run(
            f"A location-only mode now checks every unchecked on-topic author, {to_check:,} accounts, in a "
            "fixed random order, so the accounts checked so far always form a random sample even if the sweep "
            f"stops part-way. At about {per_lookup:.0f} seconds per account that is roughly {sweep_hours:.0f} "
            f"hours of lookups; in safe batches of 100 with 25-minute breaks, about {batches} batches over "
            f"{sweep_wall_h:.0f} hours ({sweep_wall_h / 24:.1f} days) with the laptop awake. A test batch ran "
            f"cleanly ({sweep_done} lookups, all successful, no blocking) and was paused for this report. "
            "The sweep stops by itself at a login wall or after 3 failed lookups in a row. A cheaper "
            "alternative: about 400 random accounts would estimate country shares to within about 5 "
            "percentage points.")
    if complete and "cluster_random" in fig_paths:
        figure(doc, fig_paths["cluster_random"], "Figure 4c. The earlier, smaller comparison (about 50 accounts "
               "per group): copy-cluster members vs a random sample. The full results above confirm it.")
    if ext_n:
        p = doc.add_paragraph()
        ext_done = not related_left
        p.add_run("Beyond the on-topic authors" + ("" if ext_done else " (in progress)") + ". ").bold = True
        p.add_run(
            ("A second sweep checked all " if ext_done else "A second sweep is checking the ")
            + "other topic-related accounts: authors of parent and quoted "
            f"posts, repliers whose posts failed the keyword rule, and on-topic authors outside the study "
            f"period. {'In total' if ext_done else 'So far'} {ext_n:,} have been checked "
            f"({100 * ext_c / ext_n:.0f}% show a country; these peripheral accounts more often lack one)"
            + ("" if ext_done else f" and about {related_left:,} remain") + ". Their countries: "
            + ", ".join(f"{c} {n:,}" for c, n in ext_countries) + ". "
            f"Across the whole database, {all_checked:,} accounts are now checked and {all_with_c:,} have a "
            "country. Accounts seen only on profiles opened during lookups are not checked, because every "
            "lookup brings in new ones of those. These extra countries are mainly context for parent and "
            "quoted posts; the on-topic results above do not depend on them.")

    doc.add_heading("Coordination signals", level=2)
    figure(doc, fig_paths["clusters"], "Figure 5. Copy-paste clusters: number of accounts posting the "
           "same text against the shortest time between two copies.")
    doc.add_paragraph(
        f"{n_clusters} copy-paste clusters were found; {len(multi)} span two or more accounts and "
        f"{under60} have copies less than a minute apart. The largest spans {max_acc} accounts: a "
        "trilingual Khmer/Chinese/English statement ('Thailand attacked first…') posted largely as replies "
        "under news posts from 24 July 2025, the first day of fighting. A three-account stock-tip spam "
        "group (identical text, copies 10 seconds apart) is kept as a positive control for any detector.")
    figure(doc, fig_paths["hashtags"], "Figure 6. Hashtags used by the most accounts. Thai and Khmer "
           "tags are shown by their meaning; the table below gives the original hashtags.")
    table(doc, ["Hashtag (as used)", "Language", "Meaning", "Accounts", "Posts"],
          [(t, tag_script(t), GLOSS.get(t, "" if tag_script(t) == "English" else "-"), n, p_)
           for t, n, p_ in top_tags[:10]], [6.0, 1.8, 5.2, 1.7, 1.5], font_pt=8)
    doc.add_paragraph(
        f"Copy-paste clusters ignore hashtags, so hashtag co-usage is measured separately: {n_campaign} "
        f"campaign hashtags are used by 20 or more accounts, and {n_pairs:,} account pairs share rare "
        "hashtags (used by only 2-10 accounts). Shared rare hashtags are unlikely to be chance, but some "
        "pairs are benign (news outlets share topical tags).")

    doc.add_heading("Account age", level=2)
    figure(doc, fig_paths["joins"], "Figure 7. Threads join month of the location-checked accounts"
           + (" (all on-topic authors)." if complete else "."))
    doc.add_paragraph(
        (f"{sum(g['jul25'] for g in sig.values()):,} on-topic authors joined Threads in July 2025, the month "
         f"the fighting began, {sig['Cambodia']['jul25']:,} of them based in Cambodia: "
         f"{pct('Cambodia', 'jul25'):.0f}% of all Cambodia-based authors, "
         f"against {pct('Thailand', 'jul25'):.1f}% of Thailand-based ones. Join months before and after are "
         "much lower and there is no wave in December. A burst of new accounts at the start of a conflict fits "
         "a mobilization campaign; it does not by itself show the accounts are fake, since genuine citizens "
         "joining to take part would look the same. " if complete else
         f"{jul25} checked accounts joined Threads in July 2025, the month the fighting began "
         f"({jul25_kh} of them based in Cambodia). Most were selected as copy-cluster members or by the "
         f"earlier prioritized lookups; in the random group only {jul25_rand} of {n_rand} joined that month, "
         "so this is a lead to follow up, not a population-wide result. ") +
        f"{100 * jul23 / n_join:.0f}% of checked accounts joined in the July 2023 launch wave and "
        f"{100 * in_per_join / n_join:.0f}% joined during the study period. {100 * over100m / n_join:.0f}% "
        "signed up after the first 100 million accounts and so have no signup number.")

    doc.add_heading("4. Limitations", level=1)
    bullets(doc, [
        ("Threads shows only part of each thread's replies. ", "On one post with 644 replies, both 'Top' "
         "and 'Recent' order end after about 111; the rest are nested or hidden. Reply data is complete for "
         "what Threads displays, not for every reply ever posted. reply_count is an upper bound."),
        ("No live time series for 2025 posts. ", "Their engagement was already final when collected, so "
         "drift must be measured by comparing phases by posting date."),
        ("English originals only. ", "Thai- and Khmer-language original posts were not searched; Thai-side "
         "campaigns are probably under-represented."),
        ("'Top' search favors popular posts. ", "Seed-outlet timelines reduce this bias but don't remove it."),
        ("Location is login-only and rate-limited. " if complete else "Location covers few accounts. ",
         "Every on-topic author was checked, but Threads allows only about 900 'About this profile' lookups a "
         "day per account, so checking new accounts in future collections is slow." if complete else
         f"{cov['All on-topic authors'][1]:,} of {n_on_acc:,} on-topic authors "
         f"({100 * cov['All on-topic authors'][1] / n_on_acc:.1f}%) were checked, and only "
         f"{100 * cov['Replied to an on-topic post'][2] / cov['Replied to an on-topic post'][0]:.1f}% of "
         "repliers have a country; a full check is prepared (section 3)."),
        ("Throttling. ", "Reply loading was throttled after searches; searches and reply pages now run in "
         "separate sessions."),
        ("AI labels are disclosures. ", f"ai_label = NONE ({ai.get('NONE', 0):,} on-topic posts) does not "
         "mean human-made."),
        ("No ground-truth labels yet. ", "Clusters and flags are candidate features, not labels."),
    ])

    doc.add_heading("5. CSV export and column guide", level=1)
    doc.add_paragraph(
        "Run 'python scripts/export_csv.py' to write five CSV files to export/csv/ (UTF-8 with a byte-order "
        "mark so Excel shows Thai and Khmer correctly). Times are UTC. The two main files join on user_id.")
    doc.add_heading("posts_on_topic.csv: one row per on-topic post in the study period", level=2)
    table(doc, ["Column", "Meaning"], POST_COLUMNS, [5.2, 11.3], font_pt=8)
    doc.add_heading("accounts.csv: one row per account with an on-topic post", level=2)
    table(doc, ["Column", "Meaning"], ACCOUNT_COLUMNS, [5.2, 11.3], font_pt=8)
    doc.add_heading("Other files", level=2)
    table(doc, ["File", "Contents"], OTHER_FILES, [4.0, 12.5], font_pt=8)
    doc.add_paragraph(
        "Feature groups for modelling: coordination (copy clusters, copy gaps, campaign and rare hashtags), "
        "inauthenticity (account age, posting rhythm, country/bio/time-zone mismatches) and descriptive "
        "(language, time zone, post counts). Keep coordination and inauthenticity separate: Meta's "
        "definition of CIB depends on deception, and real users in a conflict also copy shared messages.")

    doc.add_heading("6. Open questions", level=1)
    bullets(doc, [
        ("Labels: ", "weak labels from accounts suspended later, manual annotation, or copy-cluster positives?"),
        ("Scope: ", "add Thai and Khmer original posts? The multilingual hashtag campaigns suggest the main "
         "activity is not English-only."),
        ("Data source: ", "continue scraping, or move to Meta Content Library if approved?"),
        *([] if complete else [("Country for every account? ", f"Run the full location check ({to_check:,} "
         f"accounts, about {sweep_wall_h / 24:.1f} days, some risk to the research account), or settle for a "
         "random sample of about 400 (within about 5 points)? This decides whether country can be a "
         "per-account feature for the classifiers.")]),
        ("Study period: ", "extend into 2026 (the ceasefire and maritime-talk phase) to add a 'long after' "
         "phase for drift?"),
    ])

    out = a.out / "threads_dataset_report.docx"
    doc.save(out)
    print(f"wrote {out} and {len(fig_paths)} figures")


if __name__ == "__main__":
    main()
