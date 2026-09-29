# threads-collector

Collects Threads posts, users and profile locations for research. It uses Playwright to drive a logged-in Chromium session and saves the results to SQLite.

## Setup

```zsh
conda create -n threads-scraper python=3.12
conda activate threads-scraper
python -m pip install -r requirements.txt
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

A topic file (`topics/*.json`) has these fields:

- `keywords`: words that mark a post as on-topic. English (ASCII) keywords match whole words only, case-insensitively. Chinese keywords match anywhere in the text.
- `search` (optional): the terms to search for. Defaults to `keywords`. Use specific phrases here, because a bare "DPP" search returns a flood of unrelated posts.
- `require_any` (optional): a post must also contain one of these context words. For example, "DPP" only counts next to "Taiwan", "Taipei" and so on, which excludes Kenya's Director of Public Prosecutions.
- `languages` (optional): a post must also be detected as one of these languages, using `features.py`'s detector. `["en"]` keeps Chinese posts that mention "KMT" off-topic.
- `seeds`: Threads handles without `@`
- `search_modes` (optional): `["recent"]` (the default, newest first) and/or `["top"]` (Threads' own ranking, which surfaces older posts)
- `peaks` (optional): months of peak events, such as `["2025-07", "2025-12"]`, used by `features.py --topic`
- `period` (optional): `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}` makes this a **historical** topic. Seed profiles are scrolled back until they reach `from`, up to `--seed-scrolls`, default 400. Later rounds only scroll back to the previous visit. Post pages are opened for on-topic posts inside the period, once each, instead of the rolling 14-day window.
- `exclude_users` (optional): accounts whose posts are kept but never opened as profiles or location-checked, such as `meta.ai`, Meta's AI bot that replies to users

Topics:
- `th_kh_border_en.json`: **the current focus.** Thailand–Cambodia border conflict, English, historical period May 2025 – January 2026.
- `tw2026_local_en.json` and `tw2026_local.json`: Taiwan 2026 local elections, in English and Traditional Chinese. These are on hold; the English Taiwan content was too thin.

One `snowball` round:

1. searches each keyword (recent posts) and opens each seed profile
2. opens the post pages of recent on-topic posts that have replies, to collect the replies
3. opens the profiles of repliers and on-topic authors, most active first
4. looks up the country (and join date) of up to `--max-locations` on-topic authors: members of cross-account copy clusters first, then the most active authors, since account-level evidence only exists for looked-up users

Every stage has a limit (`--max-post-pages`, `--max-profiles`, `--max-locations` and the `--*-scrolls` options; see `snowball -h`). A default round takes about an hour. Re-running is safe: post pages are re-opened at most once per 20 h, for posts up to 14 days old, which builds the engagement time series in `post_snapshots`. Profiles are re-opened at most once a week.

The run stops at a login wall (exit code 2) and stops location lookups after 3 failures in a row, to protect the account.

### Scheduled collection

`systemd/` has a user timer that runs `scripts/collect.sh` twice a day (09:00 and 21:00, with a random delay of up to 45 min). Missed runs happen after the laptop wakes. The script collects `topics/th_kh_border_en.json` by default. To collect several topics, set `THREADS_TOPICS` to a space-separated list in the script or the service file. Each topic adds about an hour per round. Logs go to `logs/YYYY-MM-DD.log`. To enable it:

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

## Location-proxy features (`features.py`)

`country` ("Based in") is often hidden, so `features.py` computes three independent location signals offline and flags where they disagree with `country`. The flags are **candidate CIB features, not labels**.

```zsh
python features.py                          # reads and writes threads.db
python features.py --csv features_out/      # also export CSVs
python features.py --topic topics/th_kh_border_en.json   # adds join-vs-conflict features
```

It needs no browser and can run at any time. Each run replaces the `post_features` and `user_features` tables, and every row carries `features_version` and `computed_at`.

**`post_features`** (one row per post)
- `lang`: the writing system decides first (Han → `zh`, any kana → `ja`, Hangul → `ko`, Thai → `th`, Khmer → `km`). Only Latin-script text is passed to Lingua, and only when it has at least 12 letters. Lingua has no Khmer, and it guesses wildly on short non-Latin snippets. Otherwise the value is `und` (for example emoji-only) or `und-<script>`.
- `n_han`, `n_trad_only`, `n_simp_only`: Han characters, and those that exist only in Traditional or only in Simplified (via `hanzidentifier`). Many characters are shared, so a short post is often `both`.
- `zh_variant`: `traditional`, `simplified`, `both` or `mixed`, for the whole post

**`user_features`** (one row per user)
- `simp_share` = Simplified-only chars / (Traditional-only + Simplified-only), summed over all the user's posts. It needs at least 20 such characters, and is NULL below that.
- `top_lang`, `top_lang_share`
- `tz_offset`: the UTC offset that best fits the user's posting hours to a generic daily rhythm (quiet at 03:00–05:00 local, busiest in the evening). It needs 20 or more posts. `tz_margin` is the confidence: near 0 means there is no clear daily rhythm, which is typical of schedulers, bots and shared accounts. `tz_quiet_share` is the share of posts made between 01:00 and 07:00 local. Accuracy is about ±2 h with 20–30 posts, and Taiwan and China are both UTC+8, so this signal **cannot separate TW from CN**. It catches accounts that post on another continent's rhythm, or with no rhythm at all.
- `bio_country`, `bio_term`: the first place name or flag emoji in the bio, from a small list (`BIO_PLACES`). "中國" alone is deliberately not a China term, because it appears in 中國國民黨 and 中國文化大學. Bios are only collected for profiles that were opened (by `profile`, `snowball` or `location`).
- `flag_bio_vs_country`, `flag_tz_vs_country` (only when `tz_margin` ≥ 0.05, with 2 h of slack), `flag_simplified_in_taiwan` (`country` is Taiwan and `simp_share` > 0.5)

### Copy-paste clusters

`features.py` also finds posts with the same or nearly the same text from different accounts, a basic coordination signal. Text is normalized first: links, @mentions, hashtags, punctuation, emoji and spacing are removed, and it is lowercased. Texts shorter than 40 characters are skipped. Two posts are grouped when their normalized texts are identical, or share at least 80% of their character 5-grams, which works for Thai and Khmer too. Near (non-identical) matches need at least 80 normalized characters in both posts, about 12–15 English words. Shorter posts only match exactly, since two short posts can look 80% similar just by sharing a slogan.

- **`text_clusters`**: `n_posts`, `n_accounts`, `usernames`, `exact` (1 = identical after normalization), `first_at`, `last_at`, `span_hours`, `min_gap_s` and `median_gap_s` (the time between consecutive copies; seconds suggest an automated burst, hours suggest people copying a shared message), `sample_text`
- **`post_text_clusters`**: `post_pk` → `cluster_id`
- In `user_features`: `n_copy_posts` (the user's posts in clusters spanning 2+ accounts), `n_copy_partners` (the other accounts they share copied text with) and `fastest_copy_gap_s` (their closest-in-time copy of another account's text)

```sql
-- copied text across accounts, the most accounts first
SELECT n_accounts, n_posts, span_hours, usernames, sample_text
FROM text_clusters WHERE n_accounts >= 2 ORDER BY n_accounts DESC, span_hours;
```

A cluster only shows that the same text appeared; it doesn't show coordination by itself. Outlets reposting wire copy, or users sharing the same article title, cluster too.

### Feature groups: coordination vs inauthenticity

Meta's definition of coordinated *inauthentic* behavior depends on deception, meaning fake or misrepresented accounts. Real users in a national conflict often copy and paste a shared message on purpose ("share this"), so keep the two kinds of evidence as separate feature groups:

- **Coordination** (what accounts do together): `text_clusters`, `n_copy_posts`, `n_copy_partners`, `fastest_copy_gap_s`, and later co-reply patterns
- **Inauthenticity** (what an account is): `joined_month` and `account_age_months` (parsed from the "About this profile" text in `country_raw`, so only for location-checked users). This is the **Threads** join date, not Instagram's: it comes with a Threads signup number (zuck #1, mosseri #2), and nothing predates April 2023. About half of accounts show July 2023, the launch wave, so raw age is compressed. `signup_number` orders accounts within that wave, but only the first 100M accounts get one; `signup_over_100m` = 1 for later accounts. With `--topic`, `months_join_to_next_peak` (0 = joined in a fighting month) and `joined_in_period` relate the join date to the conflict. Everything is month precision only; `tz_margin` and `tz_quiet_share` (no rhythm or the wrong rhythm), `flag_tz_vs_country`, `flag_bio_vs_country`, and a "Based in" country that doesn't fit the activity
- **Descriptive** (context, not evidence): `lang`, `top_lang`, `tz_offset`, `n_posts`

## Troubleshooting

- **`redirected to login wall`**: the session expired. Run `login` again.
- **`location` times out**: Threads may have changed its layout. Run with `--headful` to watch what happens, then adjust the selectors in `Collector.fetch_country`. The profile's "More" (…) button is not the first "More" button on the page, because the sidebar's settings menu also uses that label.
- **`+0 new posts` on a search**: there may be no results, or Threads may be limiting requests. Wait and try again with `--headful`.
