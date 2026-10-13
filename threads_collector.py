#!/usr/bin/env python3
"""
threads_collector.py - Threads collector for research (Playwright + SQLite).

How it works:
  - Loads Threads pages in Chromium.
  - Captures data from two places: inline <script type="application/json"> payloads
    (first page load) and /graphql network responses (triggered by scrolling).
  - Walks every JSON payload recursively and picks out anything shaped like a post or
    a user. This means it doesn't depend on exact GraphQL paths, which change often.
  - Stores results in SQLite: users, posts, and per-scrape metric snapshots (for
    tracking engagement over time).

Usage:
  python threads_collector.py login                          # one-time, saves auth.json
  python threads_collector.py profile zuck mosseri --scrolls 30
  python threads_collector.py post https://www.threads.com/@user/post/CODE
  python threads_collector.py search "election" --recent --scrolls 20
  python threads_collector.py location zuck mosseri          # "About this profile" country
  python threads_collector.py stats
  python threads_collector.py reparse                        # refill columns from raw_json
  python threads_collector.py snowball topics/tw2026_local.json   # one topic round (cron)
  python threads_collector.py tag topics/tw2026_local.json        # re-tag on/off-topic

Add --dump-raw raw/ on the first runs to save the raw payloads so you can
check the schema.
"""
import argparse
import asyncio
import calendar
import json
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import async_playwright

BASE = "https://www.threads.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    pk TEXT PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    is_verified INTEGER,
    follower_count INTEGER,
    bio TEXT,
    country TEXT,
    country_raw TEXT,
    country_checked_at INTEGER,
    lookup_reason TEXT,  -- why the location lookup chose this user: cluster / random
    first_seen INTEGER,
    last_seen INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS posts (
    pk TEXT PRIMARY KEY,
    code TEXT,
    user_pk TEXT,
    username TEXT,
    text TEXT,
    taken_at INTEGER,
    like_count INTEGER,
    reply_count INTEGER,
    repost_count INTEGER,
    quote_count INTEGER,
    is_reply INTEGER,
    reply_to_username TEXT,
    thread_root_pk TEXT,
    thread_pos INTEGER,
    url TEXT,
    source TEXT,
    first_scraped_at INTEGER,
    raw_json TEXT,
    reshare_count INTEGER,
    topic_tag TEXT,
    media_type TEXT,
    quoted_post_pk TEXT,
    link_url TEXT,
    root_post_username TEXT,
    self_thread_pos INTEGER,
    self_thread_length INTEGER,
    is_edited INTEGER,
    ai_label TEXT,
    last_scraped_at INTEGER,
    found_as TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_pk);
CREATE INDEX IF NOT EXISTS idx_posts_root ON posts(thread_root_pk);

CREATE TABLE IF NOT EXISTS post_snapshots (
    post_pk TEXT,
    scraped_at INTEGER,
    like_count INTEGER,
    reply_count INTEGER,
    repost_count INTEGER,
    quote_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_post ON post_snapshots(post_pk);

-- one row per page load, so every post can be traced to when and how it was scraped
CREATE TABLE IF NOT EXISTS visits (
    run_id TEXT,
    kind TEXT,          -- search / profile / post / location
    target TEXT,        -- keyword, username or post url
    started_at INTEGER,
    finished_at INTEGER,
    new_posts INTEGER,
    seen_posts INTEGER,
    status TEXT,        -- ok / login_wall / ui_error / error / timeout
    n_responses INTEGER, -- /graphql responses carrying posts during the visit
    reached_end INTEGER  -- 1 if the page footer was visible at the end (list exhausted);
                         -- no batches AND not reached_end on a reply page = throttled
);
CREATE INDEX IF NOT EXISTS idx_visits_target ON visits(kind, target);

-- keyword tagging per topic; posts are tagged, never dropped
CREATE TABLE IF NOT EXISTS post_topics (
    post_pk TEXT,
    topic TEXT,
    on_topic INTEGER,
    matched_keywords TEXT,
    tagged_at INTEGER,
    match TEXT,  -- why on-topic: keywords / thread (reply under an on-topic post)
    PRIMARY KEY (post_pk, topic)
);
"""


def now() -> int:
    return int(time.time())


def as_int(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Columns added after the first release; Store adds them to older databases.
POST_MIGRATIONS = {
    "reshare_count": "INTEGER", "topic_tag": "TEXT", "media_type": "TEXT",
    "quoted_post_pk": "TEXT", "link_url": "TEXT", "root_post_username": "TEXT",
    "self_thread_pos": "INTEGER", "self_thread_length": "INTEGER", "is_edited": "INTEGER",
    "ai_label": "TEXT", "last_scraped_at": "INTEGER", "found_as": "TEXT",
}
USER_MIGRATIONS = {"lookup_reason": "TEXT"}
TOPIC_MIGRATIONS = {"match": "TEXT"}
VISIT_MIGRATIONS = {"n_responses": "INTEGER", "reached_end": "INTEGER"}

MEDIA_TYPES = {1: "image", 2: "video", 8: "carousel", 19: "text"}


def unwrap_link(url):
    """Threads wraps outbound links as l.threads.com/?u=<real url>; return the real one."""
    if url and urlparse(url).netloc == "l.threads.com":
        return parse_qs(urlparse(url).query).get("u", [url])[0]
    return url


def post_fields(p: dict) -> dict:
    """Map a raw post dict to the posts-table columns derived from it."""
    user = p.get("user") or {}
    tpi = p.get("text_post_app_info") or {}
    share = tpi.get("share_info") or {}
    stinfo = tpi.get("self_thread_info") or {}
    cap = p.get("caption")
    reply_to = (tpi.get("reply_to_author") or {}).get("username")
    username, code = user.get("username"), p.get("code")
    mt = as_int(p.get("media_type"))
    quoted = share.get("quoted_post")
    return {
        "code": code,
        "user_pk": str(user.get("pk") or user.get("id") or ""),
        "username": username,
        "text": cap.get("text") if isinstance(cap, dict) else (cap or ""),
        "taken_at": as_int(p.get("taken_at")),
        "like_count": as_int(p.get("like_count")),
        "reply_count": as_int(tpi.get("direct_reply_count")),
        "repost_count": as_int(tpi.get("repost_count")),
        "quote_count": as_int(tpi.get("quote_count")),
        "reshare_count": as_int(tpi.get("reshare_count")),
        "is_reply": int(bool(reply_to or tpi.get("is_reply"))),
        "reply_to_username": reply_to,
        "root_post_username": (tpi.get("root_post_author") or {}).get("username"),
        "url": f"{BASE}/@{username}/post/{code}" if code and username else None,
        "topic_tag": (tpi.get("tag_header") or {}).get("display_name"),
        "media_type": MEDIA_TYPES.get(mt, str(mt) if mt is not None else None),
        "quoted_post_pk": str(quoted["pk"]) if isinstance(quoted, dict) and quoted.get("pk") else None,
        "link_url": unwrap_link((tpi.get("link_preview_attachment") or {}).get("url")),
        "self_thread_pos": as_int(stinfo.get("post_position_in_self_thread")),
        "self_thread_length": as_int(stinfo.get("self_thread_length")),
        "is_edited": as_int(p.get("caption_is_edited")),
        "ai_label": (p.get("gen_ai_detection_method") or {}).get("detection_method"),
    }


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self, path: str, keep_raw: bool = True):
        self.db = sqlite3.connect(path)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(posts)")}
        for col, typ in POST_MIGRATIONS.items():
            if have and col not in have:
                self.db.execute(f"ALTER TABLE posts ADD COLUMN {col} {typ}")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        for col, typ in USER_MIGRATIONS.items():
            if have and col not in have:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(visits)")}
        for col, typ in VISIT_MIGRATIONS.items():
            if have and col not in have:
                self.db.execute(f"ALTER TABLE visits ADD COLUMN {col} {typ}")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(post_topics)")}
        for col, typ in TOPIC_MIGRATIONS.items():
            if have and col not in have:
                self.db.execute(f"ALTER TABLE post_topics ADD COLUMN {col} {typ}")
        self.db.executescript(SCHEMA)
        self.keep_raw = keep_raw
        self.thread_pos: dict[str, tuple[str, int]] = {}
        self.new_posts = 0
        self.seen_posts = 0  # new + already-known posts; used to detect end of feed
        self.result_times: list[int] = []  # taken_at of `result` posts, in arrival order

    def upsert_user(self, u: dict):
        pk = str(u.get("pk") or u.get("id") or "")
        if not pk or not u.get("username"):
            return
        t = now()
        self.db.execute(
            """INSERT INTO users(pk, username, full_name, is_verified, follower_count, bio,
                                 first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(pk) DO UPDATE SET
                 username = excluded.username,
                 full_name = COALESCE(excluded.full_name, full_name),
                 is_verified = COALESCE(excluded.is_verified, is_verified),
                 follower_count = COALESCE(excluded.follower_count, follower_count),
                 bio = COALESCE(excluded.bio, bio),
                 last_seen = excluded.last_seen""",
            (pk, u.get("username"), u.get("full_name"), as_int(u.get("is_verified")),
             as_int(u.get("follower_count")), u.get("biography"), t, t),
        )

    def add_post(self, p: dict, source: str, found_as: str = "other"):
        self.upsert_user(p["user"])
        pk = str(p.get("pk") or p.get("id")).split("_")[0]
        f = post_fields(p)
        root, pos = self.thread_pos.get(pk, (None, None))
        t = now()
        row = {**f, "pk": pk, "thread_root_pk": root, "thread_pos": pos, "source": source,
               "first_scraped_at": t, "last_scraped_at": t,
               "raw_json": json.dumps(p, ensure_ascii=False) if self.keep_raw else None}
        # On re-sight: refresh derived fields (keep old value if the new payload lacks it),
        # but keep the original source, first_scraped_at and thread position.
        keep = {"pk", "source", "first_scraped_at", "thread_root_pk", "thread_pos"}
        updates = ",\n".join(
            f"{c} = COALESCE(excluded.{c}, {c})" for c in row if c not in keep
        )
        updates += ",\n thread_root_pk = COALESCE(thread_root_pk, excluded.thread_root_pk)"
        updates += ",\n thread_pos = COALESCE(thread_pos, excluded.thread_pos)"

        exists = self.db.execute("SELECT found_as FROM posts WHERE pk=?", (pk,)).fetchone()
        if exists and ROLE_RANK.get(exists[0], -1) > ROLE_RANK[found_as]:
            found_as = exists[0]  # keep the strongest role this post has been seen in
        row["found_as"] = found_as
        cols = ", ".join(row)
        self.db.execute(
            f"INSERT INTO posts({cols}) VALUES ({','.join('?' * len(row))})"
            f" ON CONFLICT(pk) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        self.db.execute(
            "INSERT INTO post_snapshots VALUES (?,?,?,?,?,?)",
            (pk, t, f["like_count"], f["reply_count"], f["repost_count"], f["quote_count"]),
        )
        self.seen_posts += 1
        if found_as == "result" and f["taken_at"]:
            self.result_times.append(f["taken_at"])
        if not exists:
            self.new_posts += 1

    def reparse(self) -> int:
        """Re-derive post columns from stored raw_json (after parser fixes)."""
        rows = self.db.execute("SELECT pk, raw_json FROM posts WHERE raw_json IS NOT NULL").fetchall()
        for pk, raw in rows:
            f = post_fields(json.loads(raw))
            self.db.execute(
                f"UPDATE posts SET {', '.join(f'{c}=?' for c in f)} WHERE pk=?",
                (*f.values(), pk),
            )
        self.db.commit()
        return len(rows)

    def set_country(self, username: str, country, raw):
        if raw.startswith("ui_error:"):  # failed lookup: keep any country found earlier
            self.db.execute(
                "UPDATE users SET country_checked_at=?,"
                " country_raw = CASE WHEN country IS NULL THEN ? ELSE country_raw END"
                " WHERE username=?",
                (now(), raw, username),
            )
        else:
            self.db.execute(
                "UPDATE users SET country=?, country_raw=?, country_checked_at=? WHERE username=?",
                (country, raw, now(), username),
            )
        self.db.commit()

    def log_visit(self, run_id, kind, target, started, new, seen, status, n_responses=None,
                  reached_end=None):
        self.db.execute(
            """INSERT INTO visits(run_id, kind, target, started_at, finished_at, new_posts,
                                  seen_posts, status, n_responses, reached_end)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, kind, target, started, now(), new, seen, status, n_responses, reached_end),
        )
        self.db.commit()

    def tag_topic(self, topic: dict) -> int:
        """Tag every post as on/off-topic. Returns the number of on-topic posts.

        keywords   - case-insensitive; ASCII keywords match whole words only, CJK ones
                     as substrings. matched_keywords is recorded either way.
        require_any - the post must also contain one of these (context words); a list of
                     lists means one word from EACH group
        languages  - ORIGINAL posts (is_reply = 0) must be detected as one of these.
                     Replies that match the keywords count in any language.
        reply_scope - "thread": a reply found on the post page of an on-topic post is
                     on-topic too, in any language and without keywords (replies are often
                     short or in Thai/Khmer). The link is posts.source = 'post:<url>' -
                     replies are only collected from that page's reply section, and the
                     reply payload itself carries no parent post id.
        post_topics.match records why: "keywords" or "thread"."""
        kws = [(k, keyword_matcher(k)) for k in topic["keywords"]]
        groups = topic.get("require_any") or []
        if groups and isinstance(groups[0], str):
            groups = [groups]
        ctx = [[keyword_matcher(k) for k in g] for g in groups]
        langs = set(topic.get("languages") or [])
        analyzer = None
        if langs:
            from features import PostAnalyzer  # lazy: only language-filtered topics need it
            analyzer = PostAnalyzer()
        t, name = now(), topic["name"]
        rows = self.db.execute("SELECT pk, text, is_reply, url, source, found_as FROM posts").fetchall()
        result = {}  # pk -> (on_topic, matched keywords, match)
        on_urls = set()
        for pk, text, is_reply, url, _, _ in rows:
            hits = [k for k, match in kws if match(text or "")]
            ok = (bool(hits)
                  and all(any(m(text or "") for m in g) for g in ctx)
                  and (is_reply or not langs or analyzer.analyze(text)["lang"] in langs))
            result[pk] = (int(ok), hits, "keywords" if ok else None)
            if ok and url:
                on_urls.add(url)
        if topic.get("reply_scope") == "thread":
            for pk, _, _, _, source, found_as in rows:
                if (found_as == "reply" and not result[pk][0] and source
                        and source.startswith("post:") and source[5:] in on_urls):
                    result[pk] = (1, result[pk][1], "thread")
        self.db.executemany(
            """INSERT OR REPLACE INTO post_topics(post_pk, topic, on_topic, matched_keywords,
                                                  tagged_at, match) VALUES (?,?,?,?,?,?)""",
            [(pk, name, ok, json.dumps(hits, ensure_ascii=False), t, why)
             for pk, (ok, hits, why) in result.items()])
        self.db.commit()
        return sum(ok for ok, _, _ in result.values())

    def commit(self):
        self.db.commit()


# ---------------------------------------------------------------- parsing

def parse_payload(text: str):
    """Yield JSON objects from a response body (handles 'for (;;);' and NDJSON)."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        yield json.loads(text)
        return
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line[:1] in ("{", "["):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def is_post(d: dict) -> bool:
    return (
        isinstance(d.get("user"), dict)
        and "taken_at" in d
        and ("caption" in d or "text_post_app_info" in d)
        and bool(d.get("pk") or d.get("id"))
    )


def is_user(d: dict) -> bool:
    return (
        "username" in d
        and bool(d.get("pk") or d.get("id"))
        and any(k in d for k in ("follower_count", "profile_pic_url", "is_verified"))
    )


# How a post relates to the page it was found on, decided by the payload key it sits
# under (verified against raw dumps of profile, search and post pages):
#   result - what the page is about: the post-page target, a search hit, a profile post
#   reply  - a reply shown on a post page (direct_replies / pinned_replies)
#   parent - context: the posts a reply answers (containing_thread, or earlier
#            thread_items by another author in a search hit)
#   quoted - a quoted or reposted post embedded in another post
#   other  - anything else on the page (suggestions, sidebars, ...)
ROLE_RANK = {"other": 0, "quoted": 1, "parent": 2, "reply": 3, "result": 4}
RESULT_KEYS = {"media", "mediaData", "searchResults"}
KEY_ROLES = {
    "direct_replies": "reply", "pinned_replies": "reply",
    "containing_thread": "parent",
    "quoted_post": "quoted", "reposted_post": "quoted",
    "quoted_attachment_post": "quoted", "linked_inline_media": "quoted",
}


def post_pk(post: dict) -> str:
    return str(post.get("pk") or post.get("id")).split("_")[0]


def extract(obj, store: Store, source: str):
    stack = [(obj, "other")]
    while stack:
        x, role = stack.pop()
        if isinstance(x, dict):
            items = x.get("thread_items")
            if isinstance(items, list):
                posts = [it.get("post") if isinstance(it, dict) else None for it in items]
                posts = [(i, p) for i, p in enumerate(posts) if isinstance(p, dict) and is_post(p)]
                if posts:
                    # thread chain: record root + position
                    root = post_pk(posts[0][1])
                    for i, p in posts:
                        store.thread_pos.setdefault(post_pk(p), (root, i))
                    # in a result chain the last item is the hit; earlier items by the same
                    # author are their own self-thread, others' are context
                    if role == "result":
                        last_user = (posts[-1][1].get("user") or {}).get("username")
                        for i, p in posts[:-1]:
                            same = (p.get("user") or {}).get("username") == last_user
                            stack.append((p, "result" if same else "parent"))
                        stack.append((posts[-1][1], "result"))
                    else:
                        stack.extend((p, role) for _, p in posts)
                    stack.extend((v, role) for k, v in x.items() if k != "thread_items")
                    continue
            if is_post(x):
                store.add_post(x, source, role)
            elif is_user(x):
                store.upsert_user(x)
            for k, v in x.items():
                if k in KEY_ROLES:
                    stack.append((v, KEY_ROLES[k]))
                elif k in RESULT_KEYS and role == "other":
                    stack.append((v, "result"))
                else:
                    # a post's own children (e.g. its quoted post) are not the result
                    stack.append((v, "other" if role == "result" and is_post(x) else role))
        elif isinstance(x, list):
            stack.extend((v, role) for v in x)


# ---------------------------------------------------------------- browser

class LoginWall(Exception):
    """Session expired or blocked: stop the whole run instead of hammering the site."""


class TooManyFailures(Exception):
    """Several page visits in a row failed or timed out: stop the run (protect the account)."""


# Time limits, so a stuck page can never hang a run (a round once froze for 32 h waiting
# on one response). Values are generous: they only fire when something is really stuck.
RESPONSE_TIMEOUT_S = 30   # reading one /graphql response body
DRAIN_TIMEOUT_S = 30      # waiting for all pending responses after a scroll
LOCATION_TIMEOUT_S = 120  # one whole "About this profile" lookup
MAX_VISIT_FAILURES = 3    # consecutive failed/timed-out visits before the run stops


# Reply pages
LIVE_POST_SCROLLS = 10      # live topics re-open pages often, so a shallow pass is enough
HISTORY_POST_SCROLLS = 40   # historical pages are opened rarely: go deep (10 got ~39%)
REPLY_COMPLETE = 0.8        # re-open a throttled historical page only if below this share
MAX_HISTORY_OPENS = 3       # give up on a page after this many opens
# Threads shows only part of the replies it counts (about 1 in 6 on big threads, in both
# "Top" and "Recent" order; the rest are nested or hidden), so reply_count can't define
# "complete". A page is done once a visit loaded reply batches; it's re-opened only when
# the visit loaded none (n_responses = 0) AND never reached the page footer
# (reached_end = 0) - small threads legitimately fit on the first screen.
INLINE_REPLIES = 15         # a page with more replies than this should load reply batches
THROTTLE_STREAK = 3         # this many reply pages in a row with no batches = throttled
THROTTLE_PAUSE_S = 600      # back off this long the first time; skip the rest the second


def visit_timeout(scrolls: int) -> int:
    """Upper bound for one page visit: load + per-scroll wait (max 3.5 s) + drain, x2 slack."""
    return 120 + scrolls * 2 * (4 + DRAIN_TIMEOUT_S // 6)


class Collector:
    def __init__(self, page, store: Store, dump_dir: Path | None, run_id: str = ""):
        self.page, self.store, self.dump_dir = page, store, dump_dir
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.source = ""
        self.pending: set[asyncio.Task] = set()
        self.dump_n = 0
        self.failures = 0  # consecutive failed visits
        self.post_responses = 0  # /graphql responses that carried posts (all visits)
        page.on("response", self._on_response)

    def _on_response(self, resp):
        if "/graphql" in resp.url:
            task = asyncio.ensure_future(self._handle(resp))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def _handle(self, resp):
        try:
            body = await asyncio.wait_for(resp.text(), RESPONSE_TIMEOUT_S)
        except Exception:  # includes TimeoutError: a body that never finishes is dropped
            return
        if '"taken_at"' in body:
            self.post_responses += 1
        self._ingest(body, "gql")

    def _ingest(self, body: str, kind: str):
        if self.dump_dir:
            self.dump_n += 1
            (self.dump_dir / f"{self.dump_n:05d}_{kind}.json").write_text(body, encoding="utf-8")
        for obj in parse_payload(body):
            extract(obj, self.store, self.source)

    async def _ingest_inline(self):
        scripts = await self.page.eval_on_selector_all(
            'script[type="application/json"]', "els => els.map(e => e.textContent)"
        )
        for s in scripts:
            if s and ("taken_at" in s or "username" in s):
                self._ingest(s, "inline")

    async def _drain(self):
        if self.pending:
            _, stuck = await asyncio.wait(list(self.pending), timeout=DRAIN_TIMEOUT_S)
            for task in stuck:
                task.cancel()

    async def visit(self, url: str, source: str, scrolls: int, stop_before: int | None = None):
        kind, _, target = source.partition(":")
        started, start_new, start_seen = now(), self.store.new_posts, self.store.seen_posts
        start_resp = self.post_responses
        self.reached_end = None
        try:
            await asyncio.wait_for(self._visit(url, source, scrolls, stop_before),
                                   visit_timeout(scrolls))
        except LoginWall:
            self.store.log_visit(self.run_id, kind, target, started, 0, 0, "login_wall")
            raise
        except Exception as e:
            # keep what was collected before the failure
            status = "timeout" if isinstance(e, TimeoutError) else f"error:{type(e).__name__}"
            self.store.commit()
            self.store.log_visit(self.run_id, kind, target, started,
                                 self.store.new_posts - start_new,
                                 self.store.seen_posts - start_seen, status,
                                 self.post_responses - start_resp)
            print(f"[{source}] failed: {status} {e}")
            self.failures += 1
            if self.failures >= MAX_VISIT_FAILURES:
                raise TooManyFailures(f"{self.failures} page visits in a row failed")
            return
        self.failures = 0
        n_resp = self.post_responses - start_resp
        self.store.log_visit(self.run_id, kind, target, started,
                             self.store.new_posts - start_new,
                             self.store.seen_posts - start_seen, "ok", n_resp,
                             self.reached_end)
        # throttled = a scrollable page that loaded nothing more and never reached its end
        return n_resp == 0 and not self.reached_end

    async def _visit(self, url: str, source: str, scrolls: int, stop_before: int | None = None):
        self.source = source
        start = self.store.new_posts
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await self.page.wait_for_timeout(2500)
        if "/login" in self.page.url:
            print(f"[{source}] redirected to login wall - session expired? run `login` again ({url})")
            raise LoginWall(url)

        await self._ingest_inline()

        idle = 0
        for _ in range(scrolls):
            before = self.store.seen_posts
            mark = len(self.store.result_times)
            await self.page.mouse.wheel(0, random.randint(2500, 4500))
            await self.page.wait_for_timeout(random.uniform(1500, 3500))
            await self._drain()
            idle = idle + 1 if self.store.seen_posts == before else 0
            if idle >= 3:  # nothing loaded after 3 scrolls -> end of feed or rate-limited
                break
            # Reached back far enough: even the newest post in this batch is older than
            # the cutoff. (Checked per batch, so an old pinned post doesn't stop it early.)
            batch = self.store.result_times[mark:]
            if stop_before and batch and max(batch) < stop_before:
                break

        await self._drain()
        self.store.commit()
        try:  # page footer visible = the list ran out (not throttled)
            self.reached_end = int(await self.page.get_by_text("Threads Terms").first.is_visible())
        except Exception:
            self.reached_end = None
        print(f"[{source}] +{self.store.new_posts - start} new posts  ({url})")

    async def fetch_country(self, username: str) -> bool:
        """Best-effort: open 'About this profile' and read the 'Based in' line.
        UI selectors WILL need adjusting - run with --headful the first time."""
        started = now()
        try:
            await self.page.goto(f"{BASE}/@{username}", wait_until="domcontentloaded", timeout=45_000)
            await self.page.wait_for_timeout(2500)
        except Exception as e:
            # navigation failed: count it toward the consecutive-failure stop, but don't mark
            # the user as checked, so a later round retries
            print(f"[location] @{username}: page did not load ({type(e).__name__})")
            self.store.log_visit(self.run_id, "location", username, started, 0, 0,
                                 f"error:{type(e).__name__}")
            return False
        if "/login" in self.page.url:
            self.store.log_visit(self.run_id, "location", username, started, 0, 0, "login_wall")
            raise LoginWall(username)
        # the profile page is loaded anyway: keep its header (bio, followers) and posts
        self.source = f"location:{username}"
        await self._ingest_inline()
        await self._drain()
        self.store.commit()
        try:
            # Several "More" buttons exist (the sidebar one opens Settings); try each until
            # one opens a menu containing "About this profile".
            about = self.page.get_by_text(re.compile(r"About this profile", re.I)).first
            more = self.page.get_by_role("button", name=re.compile(r"^more$", re.I))
            for i in range(await more.count()):
                try:
                    await more.nth(i).click(timeout=4000)
                    await about.wait_for(timeout=2500)
                    break
                except Exception:
                    await self.page.keyboard.press("Escape")
                    await self.page.wait_for_timeout(500)
            await about.click(timeout=6000)
            dialog = self.page.get_by_role("dialog").last
            await dialog.wait_for(timeout=8000)
            # The rows load in after the dialog opens; reading after a fixed delay cut some
            # off right after "Joined". Wait for the "Based in" row, but accept its absence
            # (some profiles really have none) once the wait runs out.
            try:
                await dialog.get_by_text(re.compile(r"Based in", re.I)).first.wait_for(timeout=8000)
            except Exception:
                pass
            await self.page.wait_for_timeout(1000)
            text = await dialog.inner_text()
        except Exception as e:
            self.store.set_country(username, None, f"ui_error:{type(e).__name__}")
            print(f"[location] @{username}: could not open dialog ({type(e).__name__})")
            self.store.log_visit(self.run_id, "location", username, started, 0, 0, "ui_error")
            return False
        m = re.search(r"Based in\s*\n?\s*([^\n]+)", text, re.I)
        country = m.group(1).strip() if m else None
        if country and country.lower() == "not shared":
            country = None  # user hid it; distinguishable from "no Based in row" via country_raw
        self.store.set_country(username, country, text[:1000])
        print(f"[location] @{username}: {country or 'hidden/not shown'}")
        self.store.log_visit(self.run_id, "location", username, started, 0, 0, "ok")
        return True

    async def locations(self, usernames, max_failures: int = 3):
        """Look up countries; stop after `max_failures` consecutive dialog failures,
        which usually means Threads is blocking (protect the account)."""
        fails = 0
        for u in usernames:
            try:
                ok = await asyncio.wait_for(self.fetch_country(u.lstrip("@")), LOCATION_TIMEOUT_S)
            except TimeoutError:
                print(f"[location] @{u}: timed out after {LOCATION_TIMEOUT_S} s")
                self.store.log_visit(self.run_id, "location", u.lstrip("@"), now(), 0, 0, "timeout")
                ok = False
            fails = 0 if ok else fails + 1
            if fails >= max_failures:
                print(f"[location] {fails} failures in a row - stopping to protect the account")
                return
            await polite_pause()


async def polite_pause():
    await asyncio.sleep(random.uniform(4, 9))


def search_url(query: str, recent: bool = True) -> str:
    url = f"{BASE}/search?q={quote(query)}&serp_type=default"
    return url + "&filter=recent" if recent else url


def keyword_matcher(kw: str):
    if kw.isascii():
        rx = re.compile(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])")
        return lambda text: bool(rx.search(text.lower()))
    low = kw.lower()
    return lambda text: low in text.lower()


def load_topic(path) -> dict:
    topic = json.loads(Path(path).read_text(encoding="utf-8"))
    topic["seeds"] = [s.lstrip("@") for s in topic.get("seeds", []) if not s.startswith("#")]
    topic.setdefault("search", topic["keywords"])  # search terms default to the tag keywords
    topic["exclude_users"] = [u.lstrip("@") for u in topic.get("exclude_users", [])]
    topic.setdefault("search_modes", ["recent"])
    # optional historical window {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} (UTC, inclusive)
    per = topic.get("period")
    topic["period_ts"] = (
        (int(calendar.timegm(time.strptime(per["from"], "%Y-%m-%d"))),
         int(calendar.timegm(time.strptime(per["to"], "%Y-%m-%d"))) + 86399)
        if per else None)
    return topic


def pick_lookups(st: "Store", topic: dict, run_id: str, n: int):
    """Choose up to n users for location lookups: half copy-cluster members, half a random
    sample of other on-topic authors (seeded with run_id). Records users.lookup_reason."""
    from features import text_clusters  # lazy: keeps the collector light otherwise
    on_rows = st.db.execute(
        """SELECT p.pk, p.username, p.text, p.taken_at FROM posts p JOIN post_topics t
             ON t.post_pk = p.pk AND t.topic = ? AND t.on_topic = 1""", (topic["name"],)).fetchall()
    in_clusters = {u for c in text_clusters(on_rows) if c["n_accounts"] >= 2
                   for u in json.loads(c["usernames"])}
    candidates = st.db.execute(
        """SELECT p.username, COUNT(*) FROM posts p JOIN post_topics t
             ON t.post_pk = p.pk AND t.topic = ? AND t.on_topic = 1
           JOIN users u ON u.pk = p.user_pk
           WHERE u.country_checked_at IS NULL
             AND p.username NOT IN (SELECT value FROM json_each(?))
           GROUP BY p.username ORDER BY p.username""",
        (topic["name"], json.dumps(topic["exclude_users"]))).fetchall()
    clustered = sorted((r for r in candidates if r[0] in in_clusters), key=lambda r: -r[1])
    others = [r[0] for r in candidates if r[0] not in in_clusters]
    n_cluster = min(len(clustered), n // 2)
    n_random = min(len(others), n - n_cluster)
    picks = [(u, "cluster") for u, _ in clustered[:n_cluster]]
    picks += [(u, "random") for u in random.Random(run_id).sample(others, n_random)]
    st.db.executemany("UPDATE users SET lookup_reason = ? WHERE username = ?",
                      [(why, u) for u, why in picks])
    st.commit()
    return picks


async def snowball(col: Collector, topic: dict, a):
    """One collection round: keyword search + seed profiles -> post pages of on-topic
    posts (replies) -> repliers' profiles -> a capped batch of location lookups.
    Re-running it (e.g. from cron) revisits recent on-topic posts, which builds the
    post_snapshots time series."""
    st, name = col.store, topic["name"]
    print(f"== run {col.run_id}  topic={name}" + ("  (post pages only)" if a.only_posts else ""))
    if a.only_posts:  # reply pages only: no searches, seeds, profiles or location lookups
        a.max_profiles = a.max_locations = 0

    for kw in ([] if a.only_posts else topic["search"]):
        for mode in topic["search_modes"]:  # "recent" = newest first, "top" = Threads' ranking
            kind = "search" if mode == "recent" else "search_top"
            await col.visit(search_url(kw, recent=mode == "recent"), f"{kind}:{kw}", a.search_scrolls)
            await polite_pause()
    for u in ([] if a.only_posts else topic["seeds"]):
        if topic["period_ts"]:
            # Scroll back to the period start until some visit has actually reached it;
            # after that, only back to the last visit (a shallow earlier visit - e.g. a
            # handle check - must not stop later rounds from going deep).
            start = topic["period_ts"][0]
            oldest = st.db.execute(
                "SELECT MIN(taken_at) FROM posts WHERE username=? AND found_as='result'",
                (u,)).fetchone()[0]
            last = st.db.execute(
                "SELECT MAX(started_at) FROM visits WHERE kind='profile' AND target=? AND status='ok'",
                (u,)).fetchone()[0]
            reached = oldest is not None and oldest <= start
            stop = max(start, last - 86400) if last and reached else start
            await col.visit(f"{BASE}/@{u}", f"profile:{u}", a.seed_scrolls, stop_before=stop)
        else:
            await col.visit(f"{BASE}/@{u}", f"profile:{u}", a.profile_scrolls)
        await polite_pause()
    print(f"tagged {st.tag_topic(topic)} on-topic posts")

    # Post pages: on-topic posts with replies.
    # Live topics: posts from the last `revisit_days`, never-opened first, then the most
    # replied-to, re-opened every `revisit_hours` (builds the snapshot time series).
    # Historical topics (`period`): posts inside the period never opened yet (most
    # replied-to first), then pages whose last visit loaded no reply batches (throttled;
    # see INLINE_REPLIES), at most MAX_HISTORY_OPENS opens each.
    if topic["period_ts"]:
        since, until = topic["period_ts"]
        urls = [r[0] for r in st.db.execute(
            """WITH got AS (SELECT substr(source, 6) AS url, COUNT(*) AS n FROM posts
                            WHERE found_as = 'reply' AND source LIKE 'post:%' GROUP BY 1),
                    opens AS (SELECT target, COUNT(*) AS n FROM visits
                              WHERE kind = 'post' AND status = 'ok' GROUP BY target)
               SELECT p.url FROM posts p JOIN post_topics t ON t.post_pk = p.pk AND t.topic = ?
               LEFT JOIN got g ON g.url = p.url
               LEFT JOIN opens o ON o.target = p.url
               LEFT JOIN (SELECT target, n_responses, reached_end FROM visits v1
                          WHERE kind = 'post' AND status = 'ok' AND started_at =
                            (SELECT MAX(started_at) FROM visits v2 WHERE v2.kind = 'post'
                             AND v2.status = 'ok' AND v2.target = v1.target)) lv
                      ON lv.target = p.url
               WHERE t.on_topic = 1 AND p.found_as = 'result' AND p.is_reply = 0
                 AND p.url IS NOT NULL AND p.reply_count > 0 AND p.taken_at BETWEEN ? AND ?
                 AND (o.n IS NULL                                   -- never opened
                      OR (lv.n_responses = 0 AND lv.reached_end = 0    -- last visit throttled
                          AND p.reply_count > ?
                          AND COALESCE(g.n, 0) < ? * p.reply_count AND o.n < ?))
               ORDER BY o.n IS NOT NULL, p.reply_count DESC LIMIT ?""",
            (name, since, until, INLINE_REPLIES, REPLY_COMPLETE, MAX_HISTORY_OPENS,
             a.max_post_pages))]
        post_scrolls = a.post_scrolls or HISTORY_POST_SCROLLS
    else:
        since, until = now() - a.revisit_days * 86400, now()
        fresh = now() - a.revisit_hours * 3600
        urls = [r[0] for r in st.db.execute(
            """SELECT p.url FROM posts p JOIN post_topics t ON t.post_pk = p.pk AND t.topic = ?
               LEFT JOIN (SELECT target, MAX(started_at) last FROM visits
                          WHERE kind = 'post' AND status = 'ok' GROUP BY target) v
                      ON v.target = p.url
               WHERE t.on_topic = 1 AND p.found_as = 'result' AND p.url IS NOT NULL
                 AND p.reply_count > 0 AND p.taken_at BETWEEN ? AND ?
                 AND (v.last IS NULL OR v.last < ?)
               ORDER BY v.last IS NOT NULL, p.reply_count DESC LIMIT ?""",
            (name, since, until, fresh, a.max_post_pages))]
        post_scrolls = a.post_scrolls or LIVE_POST_SCROLLS
    print(f"post pages: {len(urls)} ({post_scrolls} scrolls each)")
    replies_of = dict(st.db.execute(
        f"SELECT url, reply_count FROM posts WHERE found_as = 'result' AND url IN "
        f"({','.join('?' * len(urls))})", urls).fetchall()) if urls else {}
    streak, backed_off = 0, False
    for url in urls:
        throttled = await col.visit(url, f"post:{url}", post_scrolls)
        await polite_pause()
        if (replies_of.get(url) or 0) > INLINE_REPLIES:
            streak = streak + 1 if throttled else 0
        if streak >= THROTTLE_STREAK:
            if backed_off:
                print(f"reply loading still throttled after a pause - skipping the remaining "
                      f"post pages this round")
                break
            print(f"{streak} reply pages in a row loaded no reply batches - "
                  f"pausing {THROTTLE_PAUSE_S // 60} min")
            await asyncio.sleep(THROTTLE_PAUSE_S)
            streak, backed_off = 0, True
    print(f"tagged {st.tag_topic(topic)} on-topic posts")

    # Profiles of people taking part in on-topic conversations (repliers on on-topic post
    # pages, and authors of on-topic posts), most active first, skipping recent profiles.
    users = [r[0] for r in st.db.execute(
        """WITH opened AS (SELECT DISTINCT 'post:' || target AS src FROM visits
                           WHERE kind = 'post' AND status = 'ok'),
                parts AS (
                  SELECT p.username FROM posts p JOIN post_topics t
                    ON t.post_pk = p.pk AND t.topic = ? AND t.on_topic = 1
                  UNION ALL
                  SELECT p.username FROM posts p JOIN opened o ON p.source = o.src
                  WHERE p.found_as = 'reply')
           SELECT username FROM parts
           WHERE username NOT IN (SELECT target FROM visits WHERE kind = 'profile'
                                  AND status = 'ok' AND started_at >= ?)
             AND username NOT IN (SELECT value FROM json_each(?))
           GROUP BY username ORDER BY COUNT(*) DESC LIMIT ?""",
        (name, now() - a.profile_revisit_days * 86400, json.dumps(topic["exclude_users"]),
         a.max_profiles))]
    for u in users:
        await col.visit(f"{BASE}/@{u}", f"profile:{u}", a.profile_scrolls)
        await polite_pause()
    print(f"tagged {st.tag_topic(topic)} on-topic posts")

    if a.max_locations:
        # Account-level evidence (country, join date) only exists for looked-up users and the
        # batch is capped. Split it: half to members of cross-account copy clusters (the
        # accounts we most need to judge), half to a RANDOM sample of the other on-topic
        # authors (a fair baseline - looking up only cluster members skewed the country mix).
        # lookup_reason records which half each user came from; the random draw is seeded
        # with the run id so it can be reproduced.
        picks = pick_lookups(st, topic, col.run_id, a.max_locations)
        n_cluster = sum(why == "cluster" for _, why in picks)
        n_random = len(picks) - n_cluster
        todo = [u for u, _ in picks]
        print(f"location lookups: {len(todo)} ({n_cluster} copy-cluster members, "
              f"{n_random} random on-topic authors)")
        await col.locations(todo)

    row = st.db.execute(
        "SELECT COUNT(*), SUM(new_posts) FROM visits WHERE run_id = ?", (col.run_id,)
    ).fetchone()
    print(f"== run {col.run_id} done: {row[0]} page loads, +{row[1] or 0} new posts")


# ---------------------------------------------------------------- CLI

def build_args():
    ap = argparse.ArgumentParser(description="Threads collector (research)")
    ap.add_argument("--db", default="threads.db")
    ap.add_argument("--auth", default="auth.json", help="saved login session (from `login`)")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    ap.add_argument("--dump-raw", type=Path, help="save raw payloads here for schema checks")
    ap.add_argument("--no-raw-json", action="store_true", help="don't store raw post JSON in DB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login")
    p = sub.add_parser("profile"); p.add_argument("usernames", nargs="+"); p.add_argument("--scrolls", type=int, default=20)
    p = sub.add_parser("post"); p.add_argument("urls", nargs="+"); p.add_argument("--scrolls", type=int, default=10)
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--recent", action="store_true"); p.add_argument("--scrolls", type=int, default=20)
    p = sub.add_parser("location"); p.add_argument("usernames", nargs="+")
    p = sub.add_parser("snowball", help="one topic collection round (safe to repeat from cron)")
    p.add_argument("topic", help="topic file, e.g. topics/tw2026_local.json")
    p.add_argument("--search-scrolls", type=int, default=30)
    p.add_argument("--profile-scrolls", type=int, default=10)
    p.add_argument("--seed-scrolls", type=int, default=400,
                   help="max scrolls per seed profile when the topic has a period (stops at the period start)")
    p.add_argument("--post-scrolls", type=int, default=None,
                   help="scrolls per post page (default: 10 live, 40 for historical topics)")
    p.add_argument("--max-post-pages", type=int, default=30)
    p.add_argument("--only-posts", action="store_true",
                   help="only open reply pages (skip searches, seeds, profiles, location lookups)")
    p.add_argument("--max-profiles", type=int, default=30)
    p.add_argument("--max-locations", type=int, default=50)
    p.add_argument("--revisit-days", type=int, default=14, help="re-open post pages of on-topic posts up to this old")
    p.add_argument("--revisit-hours", type=int, default=20, help="don't re-open a post page more often than this")
    p.add_argument("--profile-revisit-days", type=int, default=7)
    p = sub.add_parser("tag", help="tag posts on/off-topic by the topic's keywords"); p.add_argument("topic")
    sub.add_parser("stats")
    sub.add_parser("reparse", help="re-derive post columns from stored raw_json")
    return ap.parse_args()


async def run(args):
    if args.cmd == "stats":
        db = Store(args.db).db
        for label, q in [
            ("posts", "SELECT COUNT(*) FROM posts"),
            ("users", "SELECT COUNT(*) FROM users"),
            ("users with country", "SELECT COUNT(*) FROM users WHERE country IS NOT NULL"),
            ("snapshots", "SELECT COUNT(*) FROM post_snapshots"),
            ("page loads", "SELECT COUNT(*) FROM visits"),
        ]:
            print(f"{label:>20}: {db.execute(q).fetchone()[0]}")
        for topic, on, total in db.execute(
            "SELECT topic, SUM(on_topic), COUNT(*) FROM post_topics GROUP BY topic"
        ):
            print(f"{'on-topic: ' + topic:>20}: {on} of {total}")
        return

    if args.cmd == "tag":
        topic = load_topic(args.topic)
        print(f"tagged {Store(args.db).tag_topic(topic)} on-topic posts for {topic['name']}")
        return

    if args.cmd == "reparse":
        print(f"re-parsed {Store(args.db).reparse()} posts")
        return

    if args.dump_raw:
        args.dump_raw.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        headless = not (args.headful or args.cmd == "login")
        browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs = {"locale": "en-US", "viewport": {"width": 1280, "height": 900}}
        if args.cmd != "login" and Path(args.auth).exists():
            ctx_kwargs["storage_state"] = args.auth
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        if args.cmd == "login":
            await page.goto(f"{BASE}/login")
            await asyncio.to_thread(input, "Log in in the browser window, then press Enter here... ")
            await ctx.storage_state(path=args.auth)
            print(f"Saved session to {args.auth}")
            await browser.close()
            return

        store = Store(args.db, keep_raw=not args.no_raw_json)
        col = Collector(page, store, args.dump_raw)

        try:
            if args.cmd == "snowball":
                topic = load_topic(args.topic)
                if not topic["seeds"]:
                    print("note: no seed accounts in the topic file yet - keyword search only")
                await snowball(col, topic, args)
            elif args.cmd == "profile":
                for u in args.usernames:
                    await col.visit(f"{BASE}/@{u.lstrip('@')}", f"profile:{u}", args.scrolls)
                    await polite_pause()
            elif args.cmd == "post":
                for url in args.urls:
                    await col.visit(url, f"post:{url}", args.scrolls)
                    await polite_pause()
            elif args.cmd == "search":
                await col.visit(search_url(args.query, args.recent), f"search:{args.query}", args.scrolls)
            elif args.cmd == "location":
                await col.locations(args.usernames)
        except LoginWall:
            print("stopped: hit the login wall. Run `login` again, then re-run.")
            store.commit()
            await browser.close()
            raise SystemExit(2)
        except TooManyFailures as e:
            print(f"stopped: {e} - Threads may be blocking or the network is down.")
            store.commit()
            await browser.close()
            raise SystemExit(3)

        store.commit()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run(build_args()))
