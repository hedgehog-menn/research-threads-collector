#!/usr/bin/env python3
"""
features.py - offline location-proxy features for collected Threads data.

Complements users.country ("Based in", often hidden) with three independent signals,
and flags where they disagree. The flags are candidate CIB features, not labels.

  post_features       per post: language, writing system, Traditional/Simplified char counts
  user_features       per user: Simplified share, dominant language, active-hours UTC offset,
                      self-declared location in bio, mismatch flags, copy-paste counts
  text_clusters       groups of posts with the same or nearly the same text (copy-paste),
                      with how many accounts posted them and over what time span
  post_text_clusters  which cluster each duplicated post belongs to
  hashtags            each hashtag: how many accounts/posts use it, first/last use
  hashtag_pairs       account pairs that each used RARE hashtags (used by 2-10 accounts) in
                      2+ separate posts, not counting copies or quotes of each other

Usage:
  python features.py                    # reads/writes threads.db
  python features.py --db threads_test.db --csv features_out/

Re-running recomputes everything (tables are replaced). Each row carries
FEATURES_VERSION and computed_at, so exports stay reproducible.
"""
import argparse
import csv
import json
import math
import re
import sqlite3
import statistics
import time
import unicodedata
from collections import Counter
from pathlib import Path

import hanzidentifier as hz
from lingua import LanguageDetectorBuilder

FEATURES_VERSION = "7"

MIN_LATIN_LETTERS = 12  # below this, Latin-script language detection is guesswork
MIN_TZ_POSTS = 20       # posts needed to fit an active-hours offset
MIN_VARIANT_CHARS = 20  # Traditional-only + Simplified-only chars needed for a user verdict

# ---------------------------------------------------------------- writing system

SCRIPT_RANGES = [
    ("han", [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F)]),
    ("kana", [(0x3040, 0x30FF), (0x31F0, 0x31FF)]),
    ("hangul", [(0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)]),
    ("thai", [(0x0E00, 0x0E7F)]),
    ("khmer", [(0x1780, 0x17FF), (0x19E0, 0x19FF)]),
    ("cyrillic", [(0x0400, 0x04FF)]),
    ("arabic", [(0x0600, 0x06FF)]),
    ("devanagari", [(0x0900, 0x097F)]),
]

URL_RE = re.compile(r"https?://\S+|www\.\S+")
MENTION_RE = re.compile(r"@[\w.]+")


def char_script(ch: str):
    cp = ord(ch)
    for name, ranges in SCRIPT_RANGES:
        if any(lo <= cp <= hi for lo, hi in ranges):
            return name
    if ch.isalpha() and unicodedata.name(ch, "").startswith("LATIN"):
        return "latin"
    return None


def clean(text: str) -> str:
    return MENTION_RE.sub(" ", URL_RE.sub(" ", text or ""))


# ---------------------------------------------------------------- per post

class PostAnalyzer:
    def __init__(self):
        # Lingua only sees Latin-script text; other scripts are decided by Unicode range,
        # because Lingua has no Khmer and guesses wildly on short CJK/Thai snippets.
        self.latin = LanguageDetectorBuilder.from_all_languages_with_latin_script().build()

    def analyze(self, text: str) -> dict:
        t = clean(text)
        counts = Counter(s for s in map(char_script, t) if s)
        han = "".join(ch for ch in t if char_script(ch) == "han")
        trad_only = sum(1 for ch in han if hz.identify(ch) == hz.TRADITIONAL)
        simp_only = sum(1 for ch in han if hz.identify(ch) == hz.SIMPLIFIED)
        script = counts.most_common(1)[0][0] if counts else None

        lang, conf = "und", None
        if counts["kana"]:
            lang = "ja"                      # kana only occurs in Japanese
        elif script == "han":
            lang = "zh"
        elif script in ("hangul", "thai", "khmer"):
            lang = {"hangul": "ko", "thai": "th", "khmer": "km"}[script]
        elif script == "latin" and counts["latin"] >= MIN_LATIN_LETTERS:
            vals = self.latin.compute_language_confidence_values(t)
            if vals:
                lang, conf = vals[0].language.iso_code_639_1.name.lower(), round(vals[0].value, 3)
        elif script:
            lang = f"und-{script}"

        variant = None
        if han:
            variant = {hz.TRADITIONAL: "traditional", hz.SIMPLIFIED: "simplified",
                       hz.BOTH: "both", hz.MIXED: "mixed"}.get(hz.identify(han), "unknown")
        return {"lang": lang, "lang_conf": conf, "script": script, "n_han": len(han),
                "n_trad_only": trad_only, "n_simp_only": simp_only, "zh_variant": variant}


# ---------------------------------------------------------------- active hours

# Assumed relative posting activity by LOCAL hour (0-23): lowest around 03:00-05:00,
# rising through the morning, highest in the evening. A generic diurnal prior, not
# fitted to this dataset - adjust if a validation set (users with known country) says so.
DIURNAL = [0.55, 0.35, 0.20, 0.12, 0.10, 0.12, 0.25, 0.45, 0.65, 0.80, 0.85, 0.90,
           0.95, 0.95, 0.90, 0.90, 0.95, 1.00, 1.00, 1.00, 1.00, 1.00, 0.90, 0.75]
_LOGP = [math.log(w / sum(DIURNAL)) for w in DIURNAL]


def fit_utc_offset(timestamps):
    """Estimate a user's UTC offset from the hours they post at.

    Tries every offset from -11 to +14 and keeps the one under which the user's local
    posting hours best fit DIURNAL (maximum likelihood). Returns
    (offset, margin, quiet_share):
      margin      log-likelihood per post of the best offset minus the best offset at
                  least 3 h away; near 0 = no clear daily rhythm (bots, schedulers,
                  shared accounts), larger = more confident
      quiet_share share of posts at 01:00-06:59 local under the chosen offset
    """
    hours = Counter(time.gmtime(t).tm_hour for t in timestamps)
    n = sum(hours.values())
    ll = {off: sum(c * _LOGP[(h + off) % 24] for h, c in hours.items())
          for off in range(-11, 15)}
    best = max(ll, key=ll.get)
    far = [v for o, v in ll.items() if min((o - best) % 24, (best - o) % 24) >= 3]
    quiet = sum(c for h, c in hours.items() if 1 <= (h + best) % 24 <= 6)
    return best, round((ll[best] - max(far)) / n, 3), round(quiet / n, 3)


# Plausible UTC offsets per "Based in" country (standard and daylight time).
# Countries not listed get no timezone check.
COUNTRY_OFFSETS = {
    "Taiwan": (8, 8), "China": (8, 8), "Hong Kong": (8, 8), "Macau": (8, 8),
    "Singapore": (8, 8), "Malaysia": (8, 8), "Philippines": (8, 8),
    "Japan": (9, 9), "South Korea": (9, 9), "Thailand": (7, 7), "Cambodia": (7, 7),
    "Vietnam": (7, 7), "Laos": (7, 7), "Myanmar": (6, 7), "Indonesia": (7, 9),
    "India": (5, 6), "Bangladesh": (6, 6), "Pakistan": (5, 5), "Nepal": (5, 6),
    "United Arab Emirates": (4, 4), "Saudi Arabia": (3, 3), "Turkey": (3, 3),
    "United Kingdom": (0, 1), "Ireland": (0, 1), "Portugal": (0, 1),
    "France": (1, 2), "Germany": (1, 2), "Spain": (1, 2), "Italy": (1, 2),
    "Netherlands": (1, 2), "Belgium": (1, 2), "Sweden": (1, 2), "Norway": (1, 2),
    "Denmark": (1, 2), "Poland": (1, 2), "Finland": (2, 3), "Romania": (2, 3),
    "Ukraine": (2, 3), "Russia": (2, 12), "Nigeria": (1, 1), "Kenya": (3, 3),
    "South Africa": (2, 2), "Egypt": (2, 3),
    "United States": (-10, -4), "Canada": (-8, -3), "Mexico": (-8, -5),
    "Brazil": (-5, -2), "Argentina": (-3, -3), "Peru": (-5, -5), "Colombia": (-5, -5),
    "Australia": (8, 11), "New Zealand": (12, 13),
}
TZ_SLACK = 2          # allowed hours outside the expected range (fit is ~±2 h with 20-30 posts)
MIN_TZ_MARGIN = 0.05  # below this the fit is too flat to flag a mismatch


# ---------------------------------------------------------------- bio location

# Terms checked in order; the earliest match in the bio wins. Values use the same
# English country names as the "Based in" field so they can be compared directly.
BIO_PLACES = [
    ("Taiwan", ["台灣", "臺灣", "taiwan", "台北", "臺北", "新北", "桃園", "台中", "臺中",
                "台南", "臺南", "高雄", "基隆", "新竹", "苗栗", "彰化", "南投", "雲林",
                "嘉義", "屏東", "宜蘭", "花蓮", "台東", "臺東", "澎湖", "金門", "馬祖",
                "taipei", "kaohsiung", "taichung", "tainan", "taoyuan", "hsinchu", "🇹🇼"]),
    ("China", ["中國大陸", "中国", "大陆", "北京", "上海", "广州", "廣州", "深圳", "杭州",
               "成都", "china", "beijing", "shanghai", "shenzhen", "🇨🇳"]),
    ("Hong Kong", ["香港", "hong kong", "hongkong", "🇭🇰"]),
    ("Macau", ["澳門", "澳门", "macau", "macao", "🇲🇴"]),
    ("Japan", ["日本", "東京", "大阪", "japan", "tokyo", "osaka", "🇯🇵"]),
    ("South Korea", ["韓國", "韩国", "首爾", "korea", "seoul", "🇰🇷"]),
    ("Thailand", ["泰國", "ประเทศไทย", "กรุงเทพ", "thailand", "bangkok", "🇹🇭"]),
    ("Cambodia", ["柬埔寨", "កម្ពុជា", "cambodia", "phnom penh", "🇰🇭"]),
    ("Singapore", ["新加坡", "singapore", "🇸🇬"]),
    ("Malaysia", ["馬來西亞", "马来西亚", "malaysia", "kuala lumpur", "🇲🇾"]),
    ("United States", ["美國", "美国", "usa", "new york", "los angeles", "california", "🇺🇸"]),
    ("United Kingdom", ["英國", "英国", "london", "🇬🇧"]),
    ("Canada", ["加拿大", "canada", "toronto", "vancouver", "🇨🇦"]),
    ("Australia", ["澳洲", "澳大利亚", "australia", "sydney", "melbourne", "🇦🇺"]),
]
ASCII_TERM = re.compile(r"^[a-z ]+$")


def bio_location(bio: str):
    """Return (country, matched_term) for the earliest place term in the bio."""
    low = (bio or "").lower()
    best = None
    for country, terms in BIO_PLACES:
        for term in terms:
            if ASCII_TERM.match(term):  # whole words only, so "usa" doesn't hit "causa"
                m = re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", low)
                pos = m.start() if m else -1
            else:
                pos = low.find(term)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, country, term)
    return (best[1], best[2]) if best else (None, None)


# ---------------------------------------------------------------- account age

JOINED_RE = re.compile(r"Joined\s*\n?\s*([A-Z][a-z]+)\s+(\d{4})")
# "Joined April 2023 · #2": the Threads join date and signup number (zuck is #1). The number
# rises with time, so it orders accounts within a month - useful because ~half of all
# accounts show "July 2023" (the launch wave). Only the first 100 million accounts get a
# number; later ones show "100M+". It is the Threads join, not Instagram's: no date
# precedes the April 2023 internal test.
SIGNUP_RE = re.compile(r"Joined[^#]{0,40}#\s*([\d,]+)")
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}


def joined_month(country_raw):
    """'Joined April 2023' from the saved 'About this profile' text -> '2023-04'."""
    m = JOINED_RE.search(country_raw or "")
    if not m or m.group(1) not in MONTHS:
        return None
    return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}"


def signup_number(country_raw):
    """(signup number or None, over_100m): over_100m is 1 for "100M+", 0 when a number is
    shown, None when there's no join line at all."""
    m = SIGNUP_RE.search(country_raw or "")
    if m:
        return int(m.group(1).replace(",", "")), 0
    if re.search(r"Joined[^\n]*\n?[^\n]*100M\+", country_raw or ""):
        return None, 1
    return None, None


def month_index(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[5:7]) - 1


def join_vs_topic(jm, topic):
    """(months from joining to the next peak month, joined inside the study period).
    Month precision only - that's all "Joined" gives."""
    if not jm or not topic:
        return None, None
    j = month_index(jm)
    ahead = [month_index(pk) - j for pk in topic.get("peaks", []) if month_index(pk) >= j]
    per = topic.get("period")
    inside = int(month_index(per["from"]) <= j <= month_index(per["to"])) if per else None
    return (min(ahead) if ahead else None), inside


# ---------------------------------------------------------------- copy-paste clusters

MIN_DUP_CHARS = 40     # normalized text shorter than this is too generic to call a copy
NEAR_MIN_CHARS = 80    # below this (~12-15 English words) only EXACT copies count: short
                       # posts can look "80% similar" just by sharing a slogan
SHINGLE = 5            # character n-gram size; works for scripts without spaces too
MAX_SHINGLE_DF = 200   # ignore n-grams this common when looking for candidate pairs
MIN_SHARED = 8         # shared rare n-grams before a pair is compared in full
DUP_JACCARD = 0.8      # n-gram overlap at or above this = near-duplicate
HASHTAG_RE = re.compile(r"#\S+")


def normalize_for_dup(text: str) -> str:
    """Lowercase letters and digits only: drops links, @mentions, hashtags, punctuation,
    emoji and spacing, so trivial edits don't hide a copy."""
    t = HASHTAG_RE.sub(" ", clean(text)).lower()
    return "".join(ch for ch in t if ch.isalnum())


def text_clusters(posts):
    """posts: [(pk, username, text, taken_at)] -> list of clusters of 2+ posts whose
    normalized texts are identical or overlap >= DUP_JACCARD (character n-grams)."""
    norm = [(pk, u, normalize_for_dup(t), ts) for pk, u, t, ts in posts]
    norm = [x for x in norm if len(x[2]) >= MIN_DUP_CHARS]
    grams = [{n[i:i + SHINGLE] for i in range(len(n) - SHINGLE + 1)} for _, _, n, _ in norm]

    parent = list(range(len(norm)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # exact copies first (cheap), then near copies via an inverted index of rare n-grams
    first_by_text = {}
    for i, (_, _, n, _) in enumerate(norm):
        if n in first_by_text:
            parent[find(i)] = find(first_by_text[n])
        else:
            first_by_text[n] = i
    index: dict[str, list[int]] = {}
    for i, g in enumerate(grams):
        for sh in g:
            index.setdefault(sh, []).append(i)
    # count shared rare n-grams one post at a time (pairs j > i): a global pair counter
    # ran out of memory at 160k posts; the components found are the same
    for i, g in enumerate(grams):
        if len(norm[i][2]) < NEAR_MIN_CHARS:
            continue
        shared = Counter()
        for sh in g:
            ids = index[sh]
            if 1 < len(ids) <= MAX_SHINGLE_DF:
                shared.update(j for j in ids if j > i)
        for j, n in shared.items():
            if n >= MIN_SHARED and len(norm[j][2]) >= NEAR_MIN_CHARS and find(i) != find(j):
                if len(g & grams[j]) / len(g | grams[j]) >= DUP_JACCARD:
                    parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(len(norm)):
        groups.setdefault(find(i), []).append(i)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        rows = [norm[i] for i in members]
        pks = sorted(r[0] for r in rows)
        times = [r[3] for r in rows if r[3]]
        users = sorted({r[1] for r in rows})
        first = min(rows, key=lambda r: r[3] or 0)
        gaps = [b - a for a, b in zip(sorted(times), sorted(times)[1:])]
        clusters.append({
            "cluster_id": pks[0], "post_pks": pks, "n_posts": len(rows),
            "n_accounts": len(users), "usernames": json.dumps(users, ensure_ascii=False),
            "exact": int(len({r[2] for r in rows}) == 1),
            "first_at": min(times) if times else None, "last_at": max(times) if times else None,
            "span_hours": round((max(times) - min(times)) / 3600, 2) if times else None,
            # time between consecutive copies: seconds = bot-like burst, hours = people
            # copying a shared message over time
            "min_gap_s": min(gaps) if gaps else None,
            "median_gap_s": statistics.median(gaps) if gaps else None,
            "first_post_pk": first[0],
        })
    return clusters


# ---------------------------------------------------------------- hashtag co-usage

# Copy-paste clusters strip hashtags before comparing text, so posts that share campaign
# hashtags in different wording (or languages) are invisible to them. These features
# cover that: shared campaign hashtags, and pairs of accounts sharing RARE hashtags.
HASHTAG_RE2 = re.compile(r"#[^\s#]+")
TAG_TRAIL = ".,!?;:\u3002\u17d4\u17d5\u0e2f\"'\u201d\u2019)]}"  # incl. CJK/Khmer/Thai stops
CAMPAIGN_MIN_ACCOUNTS = 20   # a hashtag this many accounts use is a campaign tag
RARE_MAX_ACCOUNTS = 10       # sharing a tag this few accounts use is a co-usage signal
RARE_MIN_POSTS = 2           # ...but only if each account used shared rare tags in this many posts


def hashtags_of(text: str):
    """Normalized hashtags in a post: casefolded, trailing punctuation removed."""
    tags = set()
    for raw in HASHTAG_RE2.findall(text or ""):
        tag = raw.rstrip(TAG_TRAIL)
        # drop trailing emoji/symbols/punctuation (incl. flags, variation selectors, ZWJ),
        # but keep combining marks (Mn/Mc): Thai and Khmer words end in them
        while len(tag) > 1 and (unicodedata.category(tag[-1])[0] in "SP"
                                or unicodedata.category(tag[-1]) == "Cf" or tag[-1] == "\ufe0f"):
            tag = tag[:-1]
        tag = tag.casefold()
        if len(tag) > 1:
            tags.add(tag)
    return tags


def hashtag_features(posts, cluster_of=None, quoted_of=None):
    """posts: [(pk, user_pk, username, text, taken_at)] -> (tag rows, pair rows, per-user dict
    keyed by user_pk). Accounts are keyed by user_pk: several deactivated accounts can share
    one display name ("Instagram user").

    A pair of accounts is linked when both used rare tags (2-10 accounts) that the other also
    used, each in RARE_MIN_POSTS or more separate posts. Posts that copy or quote a post of
    the other account are left out first: same copy cluster (cluster_of), the same text, or a
    quote (quoted_of). Otherwise one copied post with a long hashtag block would count as many
    shared tags, which is one act, not repeated co-use."""
    cluster_of, quoted_of = cluster_of or {}, quoted_of or {}
    users_of, posts_of, times_of = {}, Counter(), {}
    tags_by_user: dict[str, set] = {}
    name_of, tags_of, time_of, text_key = {}, {}, {}, {}
    tag_posts: dict[str, dict] = {}  # tag -> user_pk -> [post pk]
    for pk, upk, uname, text, ts in posts:
        tags = hashtags_of(text)
        if not tags:
            continue
        name_of[upk], tags_of[pk], time_of[pk] = uname, tags, ts
        text_key[pk] = " ".join((text or "").casefold().split())
        for tag in tags:
            users_of.setdefault(tag, set()).add(upk)
            posts_of[tag] += 1
            if ts:
                times_of.setdefault(tag, []).append(ts)
            tags_by_user.setdefault(upk, set()).add(tag)
            tag_posts.setdefault(tag, {}).setdefault(upk, []).append(pk)
    tag_rows = [{"tag": tag, "n_accounts": len(us), "n_posts": posts_of[tag],
                 "first_at": min(times_of[tag]) if tag in times_of else None,
                 "last_at": max(times_of[tag]) if tag in times_of else None,
                 "is_campaign": int(len(us) >= CAMPAIGN_MIN_ACCOUNTS)}
                for tag, us in users_of.items()]
    candidates: dict[tuple, set] = {}
    for tag, by_user in tag_posts.items():
        if 2 <= len(by_user) <= RARE_MAX_ACCOUNTS:
            us = sorted(by_user)
            for i in range(len(us)):
                for j in range(i + 1, len(us)):
                    candidates.setdefault((us[i], us[j]), set()).add(tag)

    def copied(p, q):
        c = cluster_of.get(p)
        return (text_key[p] == text_key[q] or quoted_of.get(p) == q or quoted_of.get(q) == p
                or (c is not None and c == cluster_of.get(q)))

    pair_rows = []
    for (a, b), tags in candidates.items():
        pa = {pk for t in tags for pk in tag_posts[t][a]}
        pb = {pk for t in tags for pk in tag_posts[t][b]}
        pa, pb = ({p for p in pa if not any(copied(p, q) for q in pb)},
                  {q for q in pb if not any(copied(p, q) for p in pa)})
        if not pa or not pb:
            continue
        tags = tags & set().union(*(tags_of[p] for p in pa)) & set().union(*(tags_of[q] for q in pb))
        pa = {p for p in pa if tags_of[p] & tags}
        pb = {q for q in pb if tags_of[q] & tags}
        # separate posts: an account posting the same text twice counts once
        na, nb = len({text_key[p] for p in pa}), len({text_key[q] for q in pb})
        if na < RARE_MIN_POSTS or nb < RARE_MIN_POSTS:
            continue
        days = {time.strftime("%Y-%m-%d", time.gmtime(time_of[x])) for x in pa | pb if time_of[x]}
        pair_rows.append({"user_a_pk": a, "user_b_pk": b, "user_a": name_of[a], "user_b": name_of[b],
                          "n_shared": len(tags), "shared_tags": json.dumps(sorted(tags), ensure_ascii=False),
                          "posts_a": na, "posts_b": nb, "n_days": len(days)})
    partners: dict[str, set] = {}
    for r in pair_rows:
        partners.setdefault(r["user_a_pk"], set()).add(r["user_b_pk"])
        partners.setdefault(r["user_b_pk"], set()).add(r["user_a_pk"])
    per_user = {u: {"n_hashtags": len(tags),
                    "n_campaign_tags": sum(len(users_of[t]) >= CAMPAIGN_MIN_ACCOUNTS for t in tags),
                    "n_rare_tag_partners": len(partners.get(u, ()))}
                for u, tags in tags_by_user.items()}
    return tag_rows, pair_rows, per_user


# ---------------------------------------------------------------- tables

POST_COLS = ["post_pk", "user_pk", "lang", "lang_conf", "script", "n_han", "n_trad_only",
             "n_simp_only", "zh_variant", "features_version", "computed_at"]
USER_COLS = ["user_pk", "username", "country", "n_posts", "top_lang", "top_lang_share",
             "n_trad_only", "n_simp_only", "simp_share", "tz_offset", "tz_margin",
             "tz_quiet_share", "tz_n_posts", "bio_country", "bio_term",
             "flag_bio_vs_country", "flag_tz_vs_country", "flag_simplified_in_taiwan",
             "n_copy_posts", "n_copy_partners", "fastest_copy_gap_s", "min_signup_gap_to_partner",
             "n_hashtags", "n_campaign_tags", "n_rare_tag_partners",
             "joined_month", "account_age_months", "signup_number", "signup_over_100m",
             "months_join_to_next_peak", "joined_in_period", "features_version", "computed_at"]
# Feature groups (keep them apart when modelling - see README):
#   coordination:    n_copy_posts, n_copy_partners, fastest_copy_gap_s (+ text_clusters),
#                    n_campaign_tags, n_rare_tag_partners (+ hashtags, hashtag_pairs)
#   bridge (both):   min_signup_gap_to_partner / text_clusters.min_signup_gap - copy
#                    partners whose accounts were created back-to-back
#   inauthenticity:  flag_*, tz_margin / tz_quiet_share, bio_country vs country,
#                    joined_month / account_age_months / signup_number,
#                    months_join_to_next_peak / joined_in_period (need --topic)
#   descriptive:     lang, simp_share, tz_offset, n_posts
CLUSTER_COLS = ["cluster_id", "n_posts", "n_accounts", "usernames", "exact", "first_at",
                "last_at", "span_hours", "min_gap_s", "median_gap_s", "min_signup_gap",
                "n_with_signup", "first_post_pk", "sample_text",
                "features_version", "computed_at"]
POST_CLUSTER_COLS = ["post_pk", "cluster_id"]
HASHTAG_COLS = ["tag", "n_accounts", "n_posts", "first_at", "last_at", "is_campaign",
                "features_version", "computed_at"]
HASHTAG_PAIR_COLS = ["user_a_pk", "user_b_pk", "user_a", "user_b", "n_shared", "shared_tags",
                     "posts_a", "posts_b", "n_days", "features_version", "computed_at"]


def build(db: sqlite3.Connection, topic: dict | None = None):
    t0 = int(time.time())
    pa = PostAnalyzer()
    posts = db.execute("SELECT pk, user_pk, text, taken_at, username FROM posts").fetchall()
    post_rows = []
    for pk, upk, text, _, _ in posts:
        f = pa.analyze(text)
        post_rows.append({"post_pk": pk, "user_pk": upk, **f,
                          "features_version": FEATURES_VERSION, "computed_at": t0})
    by_user: dict[str, list] = {}
    for (pk, upk, _, ts, _), f in zip(posts, post_rows):
        by_user.setdefault(upk, []).append((ts, f))

    clusters = text_clusters([(pk, u, t, ts) for pk, _, t, ts, u in posts])
    text_of = {pk: t for pk, _, t, _, _ in posts}
    user_of = {pk: u for pk, _, _, _, u in posts}
    copy_posts, partners = Counter(), {}
    time_of = {pk: ts for pk, _, _, ts, _ in posts}
    # batch-created accounts get adjacent signup numbers; only the first 100M have one
    signup_of = {u: n for u, raw in db.execute("SELECT username, country_raw FROM users")
                 if (n := signup_number(raw)[0]) is not None}
    fastest: dict[str, int] = {}
    signup_gap: dict[str, int] = {}
    for c in clusters:
        nums = sorted(signup_of[u] for u in json.loads(c["usernames"]) if u in signup_of)
        c["n_with_signup"] = len(nums)
        c["min_signup_gap"] = min((b - a for a, b in zip(nums, nums[1:])), default=None)
        c.update(sample_text=(text_of[c["first_post_pk"]] or "")[:300],
                 features_version=FEATURES_VERSION, computed_at=t0)
        if c["n_accounts"] < 2:
            continue  # one account repeating itself is not cross-account copying
        # per user: closest-in-time copy by ANOTHER account in the same cluster
        for pk in c["post_pks"]:
            u, t = user_of[pk], time_of[pk]
            others = [time_of[q] for q in c["post_pks"] if user_of[q] != u and time_of[q] and t]
            if others:
                gap = min(abs(t - o) for o in others)
                fastest[u] = min(gap, fastest.get(u, gap))
        users = json.loads(c["usernames"])
        for u in users:
            partners.setdefault(u, set()).update(x for x in users if x != u)
            if u in signup_of:
                gaps = [abs(signup_of[u] - signup_of[x]) for x in users if x != u and x in signup_of]
                if gaps:
                    signup_gap[u] = min(min(gaps), signup_gap.get(u, min(gaps)))
        for pk in c["post_pks"]:
            copy_posts[user_of[pk]] += 1

    user_rows = []
    cluster_of = {pk: c["cluster_id"] for c in clusters for pk in c["post_pks"]}
    quoted_of = dict(db.execute("SELECT pk, quoted_post_pk FROM posts WHERE quoted_post_pk IS NOT NULL"))
    tag_rows, pair_rows, tag_user = hashtag_features(
        [(pk, upk, u, t, ts) for pk, upk, t, ts, u in posts], cluster_of, quoted_of)
    for r in tag_rows + pair_rows:
        r.update(features_version=FEATURES_VERSION, computed_at=t0)

    for upk, username, country, bio, country_raw in db.execute(
        "SELECT pk, username, country, bio, country_raw FROM users"
    ):
        items = by_user.get(upk, [])
        langs = Counter(f["lang"] for _, f in items if f["lang"] != "und")
        top = langs.most_common(1)[0] if langs else (None, 0)
        trad = sum(f["n_trad_only"] for _, f in items)
        simp = sum(f["n_simp_only"] for _, f in items)
        simp_share = round(simp / (trad + simp), 3) if trad + simp >= MIN_VARIANT_CHARS else None

        stamps = sorted({ts for ts, _ in items if ts})
        tz = tzm = tzq = None
        if len(stamps) >= MIN_TZ_POSTS:
            tz, tzm, tzq = fit_utc_offset(stamps)

        bio_c, bio_t = bio_location(bio)
        exp = COUNTRY_OFFSETS.get(country or "")
        user_rows.append({
            "user_pk": upk, "username": username, "country": country, "n_posts": len(items),
            "top_lang": top[0], "top_lang_share": round(top[1] / sum(langs.values()), 3) if langs else None,
            "n_trad_only": trad, "n_simp_only": simp, "simp_share": simp_share,
            "tz_offset": tz, "tz_margin": tzm, "tz_quiet_share": tzq, "tz_n_posts": len(stamps),
            "bio_country": bio_c, "bio_term": bio_t,
            "flag_bio_vs_country": int(bio_c != country) if bio_c and country else None,
            "flag_tz_vs_country": (
                int(not (exp[0] - TZ_SLACK <= tz <= exp[1] + TZ_SLACK))
                if exp and tz is not None and tzm >= MIN_TZ_MARGIN else None),
            "flag_simplified_in_taiwan": (
                int(simp_share > 0.5) if country == "Taiwan" and simp_share is not None else None),
            "n_copy_posts": copy_posts.get(username, 0),
            "n_copy_partners": len(partners.get(username, ())),
            "fastest_copy_gap_s": fastest.get(username),
            "min_signup_gap_to_partner": signup_gap.get(username),
            "n_hashtags": tag_user.get(upk, {}).get("n_hashtags", 0),
            "n_campaign_tags": tag_user.get(upk, {}).get("n_campaign_tags", 0),
            "n_rare_tag_partners": tag_user.get(upk, {}).get("n_rare_tag_partners", 0),
            "joined_month": (jm := joined_month(country_raw)),
            "signup_number": signup_number(country_raw)[0],
            "signup_over_100m": signup_number(country_raw)[1],
            "months_join_to_next_peak": join_vs_topic(jm, topic)[0],
            "joined_in_period": join_vs_topic(jm, topic)[1],
            "account_age_months": (
                (time.gmtime(t0).tm_year - int(jm[:4])) * 12 + time.gmtime(t0).tm_mon - int(jm[5:])
                if jm else None),
            "features_version": FEATURES_VERSION, "computed_at": t0,
        })
    post_clusters = [{"post_pk": pk, "cluster_id": c["cluster_id"]}
                     for c in clusters for pk in c["post_pks"]]
    return post_rows, user_rows, clusters, post_clusters, tag_rows, pair_rows


def write_table(db, name, cols, rows, key=None):
    db.execute(f"DROP TABLE IF EXISTS {name}")
    db.execute(f"CREATE TABLE {name} ({', '.join(cols)}, PRIMARY KEY ({key or cols[0]}))")
    db.executemany(
        f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})",
        [tuple(r[c] for c in cols) for r in rows],
    )


def main():
    ap = argparse.ArgumentParser(description="Compute location-proxy features")
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--csv", type=Path, help="also export post_features.csv / user_features.csv here")
    ap.add_argument("--topic", type=Path,
                    help="topic file; its `period` and `peaks` enable the join-vs-conflict features")
    a = ap.parse_args()

    db = sqlite3.connect(a.db)
    topic = json.loads(a.topic.read_text(encoding="utf-8")) if a.topic else None
    post_rows, user_rows, clusters, post_clusters, tag_rows, pair_rows = build(db, topic)
    write_table(db, "post_features", POST_COLS, post_rows)
    write_table(db, "user_features", USER_COLS, user_rows)
    write_table(db, "text_clusters", CLUSTER_COLS, clusters)
    write_table(db, "post_text_clusters", POST_CLUSTER_COLS, post_clusters)
    write_table(db, "hashtags", HASHTAG_COLS, tag_rows)
    write_table(db, "hashtag_pairs", HASHTAG_PAIR_COLS, pair_rows, key="user_a_pk, user_b_pk")
    db.commit()

    if a.csv:
        a.csv.mkdir(parents=True, exist_ok=True)
        for name, cols, rows in [("post_features", POST_COLS, post_rows),
                                 ("user_features", USER_COLS, user_rows),
                                 ("text_clusters", CLUSTER_COLS, clusters),
                                 ("post_text_clusters", POST_CLUSTER_COLS, post_clusters),
                                 ("hashtags", HASHTAG_COLS, tag_rows),
                                 ("hashtag_pairs", HASHTAG_PAIR_COLS, pair_rows)]:
            with open(a.csv / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)

    fitted = [u for u in user_rows if u["tz_offset"] is not None]
    print(f"posts: {len(post_rows)}  users: {len(user_rows)}")
    print(f"  users with Simplified share: {sum(u['simp_share'] is not None for u in user_rows)}")
    print(f"  users with fitted UTC offset: {len(fitted)}"
          + (f" (median margin {statistics.median(u['tz_margin'] for u in fitted)})" if fitted else ""))
    print(f"  users with bio location: {sum(bool(u['bio_country']) for u in user_rows)}")
    for flag in ("flag_bio_vs_country", "flag_tz_vs_country", "flag_simplified_in_taiwan"):
        vals = [u[flag] for u in user_rows if u[flag] is not None]
        print(f"  {flag}: {sum(vals)} of {len(vals)} checkable")
    multi = [c for c in clusters if c["n_accounts"] >= 2]
    print(f"  users with join date: {sum(u['joined_month'] is not None for u in user_rows)}")
    print(f"  hashtags: {len(tag_rows)} ({sum(r['is_campaign'] for r in tag_rows)} campaign tags used "
          f"by {CAMPAIGN_MIN_ACCOUNTS}+ accounts); account pairs sharing rare tags in {RARE_MIN_POSTS}+ "
          f"separate posts each: {len(pair_rows)}")
    print(f"  copy-paste clusters: {len(clusters)} ({len(multi)} spanning 2+ accounts, "
          f"{sum(c['n_posts'] for c in multi)} posts, "
          f"{sum(u['n_copy_posts'] > 0 for u in user_rows)} accounts involved)")


if __name__ == "__main__":
    main()
