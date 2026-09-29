# Threads Collector — Project Context

## Research purpose
This repo collects a **new Threads dataset** for KT's master's thesis (NTHU, Institute of Information Systems and Applications). The thesis is about detecting **coordinated inauthentic behavior (CIB)**, meaning sockpuppets and amplifiers, and about **predicting when a detector stops working**. It frames this as drift-aware monitoring, with a degradation risk score (drift × permutation importance) that predicts classifier failure.

Why a new dataset: the existing datasets (Pantip sockpuppets and TikTok political amplifiers) predate widespread generative-AI use. The advisor asked for fresh data, and suggested Threads.

## What to collect
**Demo topic (decided 2026-09-30): the Thailand–Cambodia border conflict, English posts, historical.** Topic file: `topics/th_kh_border_en.json`.
- History: the heavy fighting was in July 2025 (Kuala Lumpur ceasefire on 2025-07-28) and December 2025 (20 days, at least 101 killed, over 500k displaced; ceasefire 2025-12-27). A tense ceasefire has held through 2026. UN-backed maritime talks began on 2026-09-15.
- `period` is 2025-05-01 to 2026-01-31. Threads can't search by date, so history comes from (a) scrolling seed outlet profiles back to the period start (thaipbsworld reached March 2024 in 80 scrolls), (b) "top"-mode search on terms specific to those events, and (c) the post pages of old posts, which still have their replies. Posts in the period are opened once each: their engagement is final, so there's **no live drift time series** for them. Drift has to come from comparing phases by `taken_at` (before, during and after each fight).
- Seeds (all checked as real): Thai side thaipbsworld, khaosodenglish; Cambodian side phnompenhpost (1,304 posts on its first deep scroll; an earlier "new account, 24 posts" note was wrong), cambodianess; international reuters, apnews, aljazeeraenglish, channelnewsasia.
- On-topic = a Cambodian-side keyword AND a Thai-side term AND a conflict term (`require_any` holds two groups, and each must match). The conflict group was added on 2026-09-30 because travel, food and ranking posts that only named both countries were inflating the "after" phase. Generic words (fight, mine, peace, army, bomb) are left out because they match casual English ("please fight me"). A hand-check of 12 random on-topic posts found 12 of 12 relevant. `languages: ["en"]`. A first test showed openly partisan posts from both sides, which is good material for CIB.
- KT reads English only (not Chinese). Thai and Khmer could be added later as separate topic files.

Earlier topic, on hold: Taiwan 2026 local elections (`topics/tw2026_local*.json`). English content was too thin (21 on-topic posts out of 141 from two searches). The partial data is in `threads_tw_en.db`, and the older test data is in `threads_test.db`.

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
4. ✅ **Simple snowball.** `snowball <topic.json>` runs one round: keyword search → seed profiles → post pages of recent on-topic posts → profiles of repliers and on-topic authors → a capped batch of location lookups. Posts are tagged in `post_topics`, never dropped. Every page load is logged in `visits` with a `run_id`. A login wall stops the run (exit code 2), and location lookups stop after 3 failures in a row. Historical topics (`period`) are supported as well; see "What to collect".
5. ⏳ **Scheduled re-collection.** `scripts/collect.sh` and `systemd/threads-collect.{service,timer}` run twice a day at 09:00 and 21:00, with up to 45 min random delay and missed runs caught up after wake. There is no cron on this machine, so it uses a systemd user timer. It is written but must be enabled by KT (see README). Each round re-opens on-topic posts up to 14 days old at most once per 20 h, which builds the `post_snapshots` time series.
6. ✅ **`features.py`**, first version (see README). Language comes from Unicode script first, with Lingua used only for Latin text of 12 or more letters. Lingua has no Khmer, which matters for topic 2. The Traditional/Simplified split is a per-user `simp_share` over variant-only characters. `tz_offset` is a likelihood fit against a generic diurnal curve; checked on US accounts, it is about ±2 h with 20–30 posts and can't separate TW from CN (both UTC+8). `bio_country` comes from a gazetteer plus flag emojis. There are three mismatch flags against `country`. **Not validated yet:** the diurnal curve and thresholds are priors. Validate them on users whose `country` is known once the election data is in.
   - `location` lookups now also store the profile header (bio, followers) and posts, so bios are collected without extra page loads.
   - **Copy-paste clusters** (features v2): `text_clusters` and `post_text_clusters` group posts whose normalized text (no links, @mentions, hashtags, punctuation or emoji) is identical or has at least 80% character 5-gram overlap. Per user: `n_copy_posts` and `n_copy_partners`, counting only clusters that span 2+ accounts. On 4,895 posts it found, for example, 3 accounts posting an identical "Thailand … destroyed Hindu god statue" text within 12.8 h, and a 3-account stock-tip spam trio posting within 36 s. Near matches need at least 80 normalized characters (shorter posts match exactly only). Clusters record `min_gap_s` and `median_gap_s`; users get `fastest_copy_gap_s`.
   - **Positive control:** the stock-tip trio atzmlivana6248, starhunter623 and xuyating3261 (identical "My advice for October: $TSLA …" text, copies 10 s apart) is an off-topic but clear bot network. Keep it in the data to sanity-check any detector.
   - **Account age:** `joined_month` and `account_age_months` are parsed from "Joined <Month YYYY>" in `country_raw`, so only for location-checked users. Checked on 122 old lookups: it is the **Threads** join date (it carries a Threads signup number; zuck #1, mosseri #2; nothing before April 2023), not Instagram's. 54% show July 2023 (the launch wave). `signup_number` exists only for the first 100M accounts (56 of 122); later accounts show "100M+" (`signup_over_100m`). With `--topic`: `months_join_to_next_peak` and `joined_in_period`, using the topic's `peaks` (["2025-07", "2025-12"]). Month precision only.
   - `min_signup_gap` (per cluster) and `min_signup_gap_to_partner` (per user): the smallest signup-number difference between copy partners. Adjacent numbers suggest batch-created accounts. Only for accounts ≤100M.
   - **To re-check:** the 54% July-2023 share came from 122 mostly test-data lookups. Recompute it on this topic's (prioritized) lookups once the first round is in.
   - Snowball location lookups prioritize members of cross-account copy clusters (among on-topic posts), then the most active on-topic authors.
   - **Framing (agreed 2026-09-30):** clusters are *coordination* evidence only. *Inauthenticity* needs account-level signals (age, rhythm, country/bio mismatch, shop-named accounts posting politics). Keep them as separate feature groups (listed in README), which is also the frame for the labeling discussion with the professor.

Resolved: the location lookup clicked the sidebar's "More" button instead of the profile's; that's fixed. The old test data was moved to `threads_test.db`; the demo collects into a fresh `threads.db`.

## Demo
`scripts/make_demo.py` builds `export/demo/` (not in git): a posts-per-month chart cropped to the study period, sample posts per conflict phase, and top accounts with a repost-to-like ratio. Columns that are still empty are dropped. Re-run `tag` first if the keywords changed.

## Downstream
The output feeds KT's existing detection and drift-monitoring pipeline: five classifiers (NB, RF, SVM, DNN, DT), with PSI/KS input drift, prediction drift, and the degradation risk score. Keep the output tabular and reproducible. Every row should be traceable to when and how it was scraped.
