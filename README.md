# threads-collector

Collects Threads posts, users and profile locations for research. It uses Playwright to drive a logged-in Chromium session and saves the results to SQLite.

## Setup

```zsh
conda create -n threads-scraper python=3.12
conda activate threads-scraper
pip install playwright
playwright install chromium
```

Then log in once. A browser window opens. Log in there, then press Enter in the terminal:

```zsh
python threads_collector.py login
```

This saves your session to `auth.json`.

## Files that are not in git

| File | What it is | On a new machine |
|---|---|---|
| `auth.json` | Logged-in session cookies. Anyone with this file has access to the account. | Run `login` again. Never commit or share it. |
| `threads.db` | All collected data | Copy it over manually, or start with an empty database |
| `raw/` | Raw payloads from `--dump-raw` | Not needed |

If you collect data on more than one machine, each machine has its own `threads.db` and they don't merge. Collect on one machine, or copy the database along when you switch.

## Commands

```zsh
python threads_collector.py profile zuck mosseri --scrolls 30   # a user's posts
python threads_collector.py search "climate policy" --recent    # search results (--recent = newest first)
python threads_collector.py post https://www.threads.com/@user/post/CODE   # a post and its replies
python threads_collector.py location zuck mosseri               # "Based in" country from "About this profile"
python threads_collector.py stats                               # row counts
python threads_collector.py reparse                             # refill post columns from raw_json
python threads_collector.py snowball topics/tw2026_local.json   # one full topic round (see below)
python threads_collector.py tag topics/tw2026_local.json        # re-tag posts on/off-topic
```

Global options go **before** the command, as in `python threads_collector.py --headful location zuck`:

- `--headful` shows the browser window. It's useful when a page layout changes and something stops working.
- `--dump-raw raw/` saves every raw payload so you can inspect the data format.
- `--db`, `--auth` use a different database or session file.

`+N new posts` counts only posts that weren't already in the database. Posts you've already collected still get fresh counts and a new snapshot.

Search loads about 3 results per scroll, so use a high `--scrolls` value (for example 60) to collect a useful number of posts.

### Topic collection (snowball)

A topic file (`topics/*.json`) lists `keywords` and `seeds` (Threads handles without `@`). One `snowball` round:

1. searches each keyword (recent posts) and opens each seed profile
2. opens the post pages of recent on-topic posts that have replies, to collect the replies
3. opens the profiles of repliers and on-topic authors, most active first
4. looks up the country of up to `--max-locations` on-topic authors

Every stage has a limit (`--max-post-pages`, `--max-profiles`, `--max-locations` and the `--*-scrolls` options; see `snowball -h`). A default round takes about an hour. Re-running is safe: post pages are re-opened at most once per 20 h, for posts up to 14 days old, which builds the engagement time series in `post_snapshots`. Profiles are re-opened at most once a week.

The run stops at a login wall (exit code 2) and stops location lookups after 3 failures in a row, to protect the account.

### Scheduled collection

`systemd/` has a user timer that runs `scripts/collect.sh` twice a day (09:00 and 21:00, with a random delay of up to 45 min). Missed runs happen after the laptop wakes. Logs go to `logs/YYYY-MM-DD.log`. To enable it:

```zsh
mkdir -p ~/.config/systemd/user
cp systemd/threads-collect.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now threads-collect.timer
systemctl --user list-timers threads-collect.timer    # shows the next run
```

The service file assumes the repo lives at `~/Documents/codes/threads-collector` and the conda env at `~/.conda/envs/threads-scraper`. Edit `ExecStart` in the service file, or set `THREADS_PY` in `collect.sh`, if yours differ. By default the timer runs only while you are logged in. To run it while logged out as well, use `loginctl enable-linger`. To stop it: `systemctl --user disable --now threads-collect.timer`.

### Looking up locations for everyone collected so far

```zsh
python threads_collector.py location $(sqlite3 -list -noheader threads.db \
  "SELECT username FROM users WHERE country_checked_at IS NULL LIMIT 100;")
```

Work in batches of about 100 and take breaks between them. Each lookup waits 4–9 s, and hundreds of profile visits in a row risk rate limiting. Every result is saved as soon as it's found, so you can re-run the command to continue. If you see many `could not open dialog` errors in a row, stop and wait before trying again.

## Database

- **`users`**: `username`, `full_name`, `follower_count`, `bio`, `country`, `country_raw`, `country_checked_at`
- **`posts`**: one row per post
  - `text`, `taken_at` (unix time), `url`, `username`, `user_pk`
  - `like_count`, `reply_count`, `repost_count`, `quote_count`, `reshare_count`
  - `is_reply`, `reply_to_username`, `root_post_username` (who started the conversation)
  - `thread_root_pk`, `thread_pos`, plus `self_thread_pos` and `self_thread_length` for multi-post threads by one author
  - `topic_tag` (such as "Tech Threads"), `media_type` (`text`, `image`, `video`, `carousel`, or a raw number code), `link_url`, `quoted_post_pk`, `is_edited`
  - `ai_label`: Meta's "AI info" label source (`NONE`, `SELF_DISCLOSURE_FLOW`, `C2PA_METADATA`, `IPTC_METADATA`, or an `_EDITED` variant of the last two). It reflects disclosure or embedded metadata, not detection, so `NONE` does not mean the post isn't AI-made.
  - `found_as`: how the post relates to the page it was found on. `result` is the post-page target, a search hit or a profile post. `reply` is a reply on a post page. `parent` is a post that a reply answers, shown as context. `quoted` is a post quoted or reposted inside another. `other` is anything else, such as suggestions. When a post is seen in several roles, the strongest one is kept, in that order.
  - `source` (which command first found the post), `first_scraped_at`, `last_scraped_at`, `raw_json` (the full original data)
- **`post_snapshots`**: one row of engagement counts each time a post is seen, for tracking changes over time
- **`visits`**: one row per page load (`run_id`, `kind`, `target`, times, new/seen posts, `status`), so every post can be traced to when and how it was scraped
- **`post_topics`**: per post and topic, `on_topic` (0/1) and `matched_keywords` (JSON list). Keyword matching is a case-insensitive substring match on the post text.

What `country` means: a country name, or `NULL`. For a checked user, `NULL` can mean any of three things, and `country_raw` (the text of the "About this profile" dialog) tells you which:

- it contains `Not shared`: the user hid their location
- it starts with `ui_error:`: the lookup failed
- otherwise: the profile has no "Based in" line, which seems to be the case for newer accounts

If the parser changes, run `reparse` to rebuild the post columns from `raw_json` without collecting again. Older databases get the new columns added automatically.

### Useful queries

```sql
-- how many accounts are in each country
SELECT country, COUNT(*) FROM users WHERE country IS NOT NULL GROUP BY country ORDER BY 2 DESC;

-- location status for each checked user
SELECT username,
  CASE WHEN country IS NOT NULL THEN country
       WHEN country_raw LIKE '%Not shared%' THEN '(hidden by user)'
       WHEN country_raw LIKE 'ui_error%' THEN '(lookup failed)'
       ELSE '(no Based in row)' END AS status
FROM users WHERE country_checked_at IS NOT NULL;

-- most-liked posts
SELECT username, like_count, substr(text, 1, 80) FROM posts ORDER BY like_count DESC LIMIT 20;

-- posts per command that collected them
SELECT source, COUNT(*) FROM posts GROUP BY source;

-- on-topic search hits and replies only
SELECT p.username, p.found_as, t.matched_keywords, substr(p.text, 1, 60)
FROM posts p JOIN post_topics t ON t.post_pk = p.pk AND t.topic = 'tw2026_local'
WHERE t.on_topic = 1 AND p.found_as IN ('result', 'reply');

-- what each collection run did
SELECT run_id, kind, COUNT(*), SUM(new_posts), SUM(status != 'ok') AS problems
FROM visits GROUP BY run_id, kind;
```

## Troubleshooting

- **`redirected to login wall`**: the session expired. Run `login` again.
- **`location` times out**: Threads may have changed its layout. Run with `--headful` to watch what happens, then adjust the selectors in `Collector.fetch_country`. The profile's "More" (…) button is not the first "More" button on the page, because the sidebar's settings menu also uses that label.
- **`+0 new posts` on a search**: there may be no results, or Threads may be limiting requests. Wait and try again with `--headful`.
