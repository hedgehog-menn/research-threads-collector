# Threads Collector — Project Context

## Research purpose
This repo collects a **new Threads dataset** for KT's master's thesis (NTHU, Institute of Information Systems and Applications). The thesis is about detecting **coordinated inauthentic behavior (CIB)**, meaning sockpuppets and amplifiers, and about **predicting when a detector stops working**. It frames this as drift-aware monitoring, with a degradation risk score (drift × permutation importance) that predicts classifier failure.

Why a new dataset: the existing datasets (Pantip sockpuppets and TikTok political amplifiers) predate widespread generative-AI use. The advisor asked for fresh data, and suggested Threads.

## What to collect
**Demo topic: Taiwan 2026 local elections (九合一).** Election day is 2026-11-28 and results are finalized 2026-12-04. The collection window runs from now to mid-December, so the data covers before, during and after the election, which is a natural setup for drift.
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
1. **Commit the current fixes and this file.** Done in code but not yet committed: the engagement-count fix (the real key is `text_post_app_info`), the `reparse` command, the scroll-loop early-stop fix, field refresh on re-seen posts, and the rule that a failed location lookup doesn't overwrite an existing country.
2. **Verify engagement counts.** Run `reparse` on the real DB, then compare about 3 posts against the browser.
3. **Add a `found_as` column** (result / quoted / reply / parent). Right now every post found in a run gets the same `source`, so search results can't be told apart from nested quoted or suggested posts.
4. **Simple snowball for the election topic.** Chain it: seed accounts plus keyword search → post pages → replies → repliers' profiles. Tag posts on-topic or off-topic by keyword.
5. **Scheduled re-collection (cron). Start as soon as possible:** the election window is time-bound, and days that aren't collected can't be backfilled.
6. **`features.py`** (offline, can run later):
   - **Language and script:** fastText lid.176 and lingua both return plain `zh` and can't tell Traditional from Simplified Chinese. Add a script classifier on top (hanzidentifier, or character counts via OpenCC). Threads' own `detected_language` covers only about 3% of posts.
   - **Active-hours timezone** fitted from `taken_at` (needs roughly 20+ posts per user).
   - **Self-declared location** in `users.bio`.
   - Mismatches between these signals and `country` are candidate CIB features.

Resolved: the location lookup clicked the sidebar's "More" button instead of the profile's; that's fixed. The current DB is mostly test data, so start a fresh DB for the demo.

## Downstream
The output feeds KT's existing detection and drift-monitoring pipeline: five classifiers (NB, RF, SVM, DNN, DT), with PSI/KS input drift, prediction drift, and the degradation risk score. Keep the output tabular and reproducible. Every row should be traceable to when and how it was scraped.
