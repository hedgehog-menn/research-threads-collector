#!/usr/bin/env python3
"""
features.py - offline location-proxy features for collected Threads data.

Complements users.country ("Based in", often hidden) with three independent signals,
and flags where they disagree. The flags are candidate CIB features, not labels.

  post_features  per post: language, writing system, Traditional/Simplified char counts
  user_features  per user: Simplified share, dominant language, active-hours UTC offset,
                 self-declared location in bio, mismatch flags

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

FEATURES_VERSION = "1"

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


# ---------------------------------------------------------------- tables

POST_COLS = ["post_pk", "user_pk", "lang", "lang_conf", "script", "n_han", "n_trad_only",
             "n_simp_only", "zh_variant", "features_version", "computed_at"]
USER_COLS = ["user_pk", "username", "country", "n_posts", "top_lang", "top_lang_share",
             "n_trad_only", "n_simp_only", "simp_share", "tz_offset", "tz_margin",
             "tz_quiet_share", "tz_n_posts", "bio_country", "bio_term",
             "flag_bio_vs_country", "flag_tz_vs_country", "flag_simplified_in_taiwan",
             "features_version", "computed_at"]


def build(db: sqlite3.Connection):
    t0 = int(time.time())
    pa = PostAnalyzer()
    posts = db.execute("SELECT pk, user_pk, text, taken_at FROM posts").fetchall()
    post_rows = []
    for pk, upk, text, _ in posts:
        f = pa.analyze(text)
        post_rows.append({"post_pk": pk, "user_pk": upk, **f,
                          "features_version": FEATURES_VERSION, "computed_at": t0})
    by_user: dict[str, list] = {}
    for (pk, upk, _, ts), f in zip(posts, post_rows):
        by_user.setdefault(upk, []).append((ts, f))

    user_rows = []
    for upk, username, country, bio in db.execute(
        "SELECT pk, username, country, bio FROM users"
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
            "features_version": FEATURES_VERSION, "computed_at": t0,
        })
    return post_rows, user_rows


def write_table(db, name, cols, rows):
    db.execute(f"DROP TABLE IF EXISTS {name}")
    db.execute(f"CREATE TABLE {name} ({', '.join(cols)}, PRIMARY KEY ({cols[0]}))")
    db.executemany(
        f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})",
        [tuple(r[c] for c in cols) for r in rows],
    )


def main():
    ap = argparse.ArgumentParser(description="Compute location-proxy features")
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--csv", type=Path, help="also export post_features.csv / user_features.csv here")
    a = ap.parse_args()

    db = sqlite3.connect(a.db)
    post_rows, user_rows = build(db)
    write_table(db, "post_features", POST_COLS, post_rows)
    write_table(db, "user_features", USER_COLS, user_rows)
    db.commit()

    if a.csv:
        a.csv.mkdir(parents=True, exist_ok=True)
        for name, cols, rows in [("post_features", POST_COLS, post_rows),
                                 ("user_features", USER_COLS, user_rows)]:
            with open(a.csv / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
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


if __name__ == "__main__":
    main()
