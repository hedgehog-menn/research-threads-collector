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
```

Global options go **before** the command, as in `python threads_collector.py --headful location zuck`:

- `--headful` shows the browser window. It's useful when a page layout changes and something stops working.
- `--dump-raw raw/` saves every raw payload so you can inspect the data format.
- `--db`, `--auth` use a different database or session file.

`+N new posts` counts only posts that weren't already in the database. Posts you've already collected still get fresh like and reply counts.

### Looking up locations for everyone collected so far

```zsh
python threads_collector.py location $(sqlite3 -list -noheader threads.db \
  "SELECT username FROM users WHERE country_checked_at IS NULL LIMIT 100;")
```

Work in batches of about 100 and take breaks between them. Each lookup waits 4–9 s, and hundreds of profile visits in a row risk rate limiting. Every result is saved as soon as it's found, so you can re-run the command to continue. If you see many `could not open dialog` errors in a row, stop and wait before trying again.

## Database

- **`users`**: `username`, `full_name`, `follower_count`, `bio`, `country`, `country_raw`, `country_checked_at`
- **`posts`**: `text`, `taken_at` (unix time), like, reply, repost and quote counts, `is_reply`, `thread_root_pk`, `url`, `source` (which command found the post), `raw_json`
- **`post_snapshots`**: one row of engagement counts each time a post is seen, for tracking changes over time

What `country` means: a country name, or `NULL`. For a checked user, `NULL` can mean any of three things, and `country_raw` (the text of the "About this profile" dialog) tells you which:

- it contains `Not shared`: the user hid their location
- it starts with `ui_error:`: the lookup failed
- otherwise: the profile has no "Based in" line, which seems to be the case for newer accounts

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
```

## Troubleshooting

- **`redirected to login wall`**: the session expired. Run `login` again.
- **`location` times out**: Threads may have changed its layout. Run with `--headful` to watch what happens, then adjust the selectors in `Collector.fetch_country`. The profile's "More" (…) button is not the first "More" button on the page, because the sidebar's settings menu also uses that label.
- **`+0 new posts` on a search**: there may be no results, or Threads may be limiting requests. Wait and try again with `--headful`.
