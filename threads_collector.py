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

Add --dump-raw raw/ on the first runs to save the raw payloads so you can
check the schema.
"""
import argparse
import asyncio
import json
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

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
    raw_json TEXT
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


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self, path: str, keep_raw: bool = True):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.keep_raw = keep_raw
        self.thread_pos: dict[str, tuple[str, int]] = {}
        self.new_posts = 0

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

    def add_post(self, p: dict, source: str):
        user = p["user"]
        self.upsert_user(user)

        pk = str(p.get("pk") or p.get("id")).split("_")[0]
        tpi = p.get("text_post_info") or {}
        cap = p.get("caption")
        text = cap.get("text") if isinstance(cap, dict) else (cap or "")
        reply_to = (tpi.get("reply_to_author") or {}).get("username")
        username = user.get("username")
        code = p.get("code")
        root, pos = self.thread_pos.get(pk, (None, None))
        counts = (as_int(p.get("like_count")), as_int(tpi.get("direct_reply_count")),
                  as_int(tpi.get("repost_count")), as_int(tpi.get("quote_count")))
        t = now()

        exists = self.db.execute("SELECT 1 FROM posts WHERE pk=?", (pk,)).fetchone()
        self.db.execute(
            """INSERT INTO posts(pk, code, user_pk, username, text, taken_at,
                                 like_count, reply_count, repost_count, quote_count,
                                 is_reply, reply_to_username, thread_root_pk, thread_pos,
                                 url, source, first_scraped_at, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pk) DO UPDATE SET
                 like_count = COALESCE(excluded.like_count, like_count),
                 reply_count = COALESCE(excluded.reply_count, reply_count),
                 repost_count = COALESCE(excluded.repost_count, repost_count),
                 quote_count = COALESCE(excluded.quote_count, quote_count),
                 reply_to_username = COALESCE(excluded.reply_to_username, reply_to_username),
                 thread_root_pk = COALESCE(thread_root_pk, excluded.thread_root_pk),
                 thread_pos = COALESCE(thread_pos, excluded.thread_pos)""",
            (pk, code, str(user.get("pk") or user.get("id") or ""), username, text,
             as_int(p.get("taken_at")), *counts,
             int(bool(reply_to or tpi.get("is_reply"))), reply_to, root, pos,
             f"{BASE}/@{username}/post/{code}" if code and username else None,
             source, t, json.dumps(p, ensure_ascii=False) if self.keep_raw else None),
        )
        self.db.execute(
            "INSERT INTO post_snapshots VALUES (?,?,?,?,?,?)", (pk, t, *counts)
        )
        if not exists:
            self.new_posts += 1

    def set_country(self, username: str, country, raw):
        self.db.execute(
            "UPDATE users SET country=?, country_raw=?, country_checked_at=? WHERE username=?",
            (country, raw, now(), username),
        )
        self.db.commit()

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
        and ("caption" in d or "text_post_info" in d)
        and bool(d.get("pk") or d.get("id"))
    )


def is_user(d: dict) -> bool:
    return (
        "username" in d
        and bool(d.get("pk") or d.get("id"))
        and any(k in d for k in ("follower_count", "profile_pic_url", "is_verified"))
    )


def extract(obj, store: Store, source: str):
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            items = x.get("thread_items")
            if isinstance(items, list):  # thread chain: record root + position
                root = None
                for i, it in enumerate(items):
                    post = it.get("post") if isinstance(it, dict) else None
                    if isinstance(post, dict) and is_post(post):
                        pk = str(post.get("pk") or post.get("id")).split("_")[0]
                        root = root or pk
                        store.thread_pos.setdefault(pk, (root, i))
            if is_post(x):
                store.add_post(x, source)
            elif is_user(x):
                store.upsert_user(x)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


# ---------------------------------------------------------------- browser

class Collector:
    def __init__(self, page, store: Store, dump_dir: Path | None):
        self.page, self.store, self.dump_dir = page, store, dump_dir
        self.source = ""
        self.pending: set[asyncio.Task] = set()
        self.dump_n = 0
        page.on("response", self._on_response)

    def _on_response(self, resp):
        if "/graphql" in resp.url:
            task = asyncio.ensure_future(self._handle(resp))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def _handle(self, resp):
        try:
            body = await resp.text()
        except Exception:
            return
        self._ingest(body, "gql")

    def _ingest(self, body: str, kind: str):
        if self.dump_dir:
            self.dump_n += 1
            (self.dump_dir / f"{self.dump_n:05d}_{kind}.json").write_text(body, encoding="utf-8")
        for obj in parse_payload(body):
            extract(obj, self.store, self.source)

    async def _drain(self):
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)

    async def visit(self, url: str, source: str, scrolls: int):
        self.source = source
        start = self.store.new_posts
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await self.page.wait_for_timeout(2500)
        if "/login" in self.page.url:
            print(f"[{source}] redirected to login wall - not available logged out ({url})")
            return

        scripts = await self.page.eval_on_selector_all(
            'script[type="application/json"]', "els => els.map(e => e.textContent)"
        )
        for s in scripts:
            if s and ("taken_at" in s or "username" in s):
                self._ingest(s, "inline")

        idle = 0
        for _ in range(scrolls):
            before = self.store.new_posts
            await self.page.mouse.wheel(0, random.randint(2500, 4500))
            await self.page.wait_for_timeout(random.uniform(1500, 3500))
            await self._drain()
            idle = idle + 1 if self.store.new_posts == before else 0
            if idle >= 3:  # no new posts after 3 scrolls -> end of feed or rate-limited
                break

        await self._drain()
        self.store.commit()
        print(f"[{source}] +{self.store.new_posts - start} new posts  ({url})")

    async def fetch_country(self, username: str):
        """Best-effort: open 'About this profile' and read the 'Based in' line.
        UI selectors WILL need adjusting - run with --headful the first time."""
        await self.page.goto(f"{BASE}/@{username}", wait_until="domcontentloaded")
        await self.page.wait_for_timeout(2500)
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
            await self.page.wait_for_timeout(1500)
            text = await dialog.inner_text()
        except Exception as e:
            self.store.set_country(username, None, f"ui_error:{type(e).__name__}")
            print(f"[location] @{username}: could not open dialog ({type(e).__name__})")
            return
        m = re.search(r"Based in\s*\n?\s*([^\n]+)", text, re.I)
        country = m.group(1).strip() if m else None
        if country and country.lower() == "not shared":
            country = None  # user hid it; distinguishable from "no Based in row" via country_raw
        self.store.set_country(username, country, text[:1000])
        print(f"[location] @{username}: {country or 'hidden/not shown'}")


async def polite_pause():
    await asyncio.sleep(random.uniform(4, 9))


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
    sub.add_parser("stats")
    return ap.parse_args()


async def run(args):
    if args.cmd == "stats":
        db = sqlite3.connect(args.db)
        for label, q in [
            ("posts", "SELECT COUNT(*) FROM posts"),
            ("users", "SELECT COUNT(*) FROM users"),
            ("users with country", "SELECT COUNT(*) FROM users WHERE country IS NOT NULL"),
            ("snapshots", "SELECT COUNT(*) FROM post_snapshots"),
        ]:
            print(f"{label:>20}: {db.execute(q).fetchone()[0]}")
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

        if args.cmd == "profile":
            for u in args.usernames:
                await col.visit(f"{BASE}/@{u.lstrip('@')}", f"profile:{u}", args.scrolls)
                await polite_pause()
        elif args.cmd == "post":
            for url in args.urls:
                await col.visit(url, f"post:{url}", args.scrolls)
                await polite_pause()
        elif args.cmd == "search":
            url = f"{BASE}/search?q={quote(args.query)}&serp_type=default"
            if args.recent:
                url += "&filter=recent"
            await col.visit(url, f"search:{args.query}", args.scrolls)
        elif args.cmd == "location":
            for u in args.usernames:
                await col.fetch_country(u.lstrip("@"))
                await polite_pause()

        store.commit()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run(build_args()))
