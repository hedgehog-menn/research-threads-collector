# Threads Collector — Project Context

## Research purpose
This repo collects a **new Threads dataset** for KT's master's thesis (NTHU, Institute of Information Systems and Applications). The thesis is about detecting **coordinated inauthentic behavior (CIB)**, meaning sockpuppets and amplifiers, and about **predicting when a detector stops working**. It frames this as drift-aware monitoring, with a degradation risk score (drift × permutation importance) that predicts classifier failure.

Why a new dataset: the existing datasets (Pantip sockpuppets and TikTok political amplifiers) predate widespread generative-AI use. The advisor asked for fresh data, and suggested Threads.

## What to collect
**Demo topic: Taiwan 2026 local elections (九合一).** **Decision (2026-09-29): English posts first.** KT doesn't read Chinese, so the active topic is `topics/tw2026_local_en.json`: English keywords, `languages: ["en"]`, and a `require_any` Taiwan context. The Chinese topic file `topics/tw2026_local.json` is kept for later. A first test found English volume thin (21 on-topic posts out of 141 from two searches), and CIB detection needs volume, so revisit adding the Chinese topic (or collecting it in the background) before the election. Election day is 2026-11-28 and results are finalized 2026-12-04. The collection window runs from now to mid-December, so the data covers before, during and after the election, which is a natural setup for drift.
- Language: mostly Traditional Chinese.
- Seeds: mayoral candidates in the six municipalities from ALL parties, plus the official KMT, DPP and TPP accounts and major news outlets of mixed leanings. KT verifies the Threads handles. Seeding from one side only reproduces the one-sided sampling problem in prior work, so keep it balanced.
- Keywords: 九合一, 地方選舉, 2026選舉, 市長選舉, 議員選舉, 投票, 催票, 國民黨, 民進黨, 民眾黨, 藍白, 綠營, plus candidate names.
- Tag snowballed posts as on-topic or off-topic by keyword instead of dropping them.

Second topic (later, not for the demo): the Thailand–Cambodia border conflict (Thai, Khmer, English). Still open: one platform with two topics, or cross-platform.

The data needs to support:
- **Account-level behavior:** posting cadence, reply patterns, and who replies to whom.
- **Coordination signals:** accounts that repeatedly co-reply or co-engage on the same threads, and bursty timing.
- **Change over time:** repeated scrapes of the same posts and users, for drift analysis. This is central to the thesis, not optional.

**Labels are an open problem.** There is no ground truth for the new data yet. Don't invent a labeling scheme; raise it with KT.

## Hard constraints
- **Logged-in session.** KT now collects with a logged-in account, because account country is login-only. `auth.json` holds its session cookies, so never commit, print, or copy it anywhere.
- **Protect the account.** A ban loses the session and the collection pipeline. Keep location lookups to batches of about 100 with breaks in between, and stop on repeated `could not open dialog` errors.
- **Stay polite.** Keep the existing delays (1.5–3.5 s per scroll, 4–9 s between targets). Don't parallelize requests.
- **Don't guess payload keys.** Verify them against raw dumps (`--dump-raw raw/`) before changing parsing logic.
- Never commit `threads.db`, `raw/`, or `auth.json`.
- Python 3.12, conda env `threads-scraper`. Install packages with `python -m pip`.

## Current state of `threads_collector.py`
- Playwright (Chromium) loads pages and captures inline `<script type="application/json">` payloads plus `/graphql` responses. A recursive walker extracts post-shaped and user-shaped objects, and the results go into SQLite (`users`, `posts`, `post_snapshots`).
- Domain is `https://www.threads.com`.

Verified behavior:
- ✅ **Post pages** return replies (logged out: 185 replies from 181 users on one post). This is the main discovery mechanism.
- ✅ **"About this profile" (account country)** works logged in. It is login-only; logged out, the "…" menu shows only "Copy link". `country` is NULL when hidden ("Not shared"), when the lookup failed (`ui_error:`), or when there is no "Based in" row. See README.
- ✅ **Search** works logged in, at roughly 3 results per scroll.
- ✅ **Profiles** return recent posts. Logged out, depth was capped (17 posts from 5 scrolls); the logged-in cap is untested.
- ℹ️ **`ai_label`** records Meta's "AI info" source (self-disclosure or C2PA/IPTC metadata). It is disclosure, not detection: `NONE` does not mean human-made. It is relevant to the genAI-era motivation of the dataset, but it is not a label.

## Next tasks (in order)
1. ✅ **Commit fixes.** Done: the engagement-count key (`text_post_app_info`), `reparse`, the scroll early-stop fix, field refresh on re-seen posts, and failed location lookups no longer overwrite a country.
2. ⏳ **Verify engagement counts in the browser.** The code is fixed and `reparse` has been run on the old DB (now `threads_test.db`). Still to do: compare about 3 posts from the fresh DB against the browser.
3. ✅ **`found_as` column.** Values are `result`, `reply`, `parent`, `quoted` and `other`, decided by the payload key a post sits under (checked against dumps of profile, search and post pages). When a post is seen in several roles, the strongest one is kept.
4. ✅ **Simple snowball.** `snowball topics/tw2026_local.json` runs one round: keyword search → seed profiles → post pages of recent on-topic posts → profiles of repliers and on-topic authors → a capped batch of location lookups. Posts are tagged in `post_topics`, never dropped. Every page load is logged in `visits` with a `run_id`. A login wall stops the run (exit code 2), and location lookups stop after 3 failures in a row. **Seeds are still empty: KT has to add verified handles and candidate names to the topic file.**
5. ⏳ **Scheduled re-collection.** `scripts/collect.sh` and `systemd/threads-collect.{service,timer}` run twice a day at 09:00 and 21:00, with up to 45 min random delay and missed runs caught up after wake. There is no cron on this machine, so it uses a systemd user timer. It is written but must be enabled by KT (see README). Each round re-opens on-topic posts up to 14 days old at most once per 20 h, which builds the `post_snapshots` time series.
6. ✅ **`features.py`**, first version (see README). Language comes from Unicode script first, with Lingua used only for Latin text of 12 or more letters. Lingua has no Khmer, which matters for topic 2. The Traditional/Simplified split is a per-user `simp_share` over variant-only characters. `tz_offset` is a likelihood fit against a generic diurnal curve; checked on US accounts, it is about ±2 h with 20–30 posts and can't separate TW from CN (both UTC+8). `bio_country` comes from a gazetteer plus flag emojis. There are three mismatch flags against `country`. **Not validated yet:** the diurnal curve and thresholds are priors. Validate them on users whose `country` is known once the election data is in.
   - `location` lookups now also store the profile header (bio, followers) and posts, so bios are collected without extra page loads.

Resolved: the location lookup clicked the sidebar's "More" button instead of the profile's; that's fixed. The old test data was moved to `threads_test.db`; the demo collects into a fresh `threads.db`.

## Downstream
The output feeds KT's existing detection and drift-monitoring pipeline: five classifiers (NB, RF, SVM, DNN, DT), with PSI/KS input drift, prediction drift, and the degradation risk score. Keep the output tabular and reproducible. Every row should be traceable to when and how it was scraped.
