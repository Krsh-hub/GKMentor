# GK System - starter (Python)

AI-driven daily GK -> weekly wrap -> monthly compilation for competitive exams.
Built to the robust design: trust tiers, event clustering, claim grounding, confidence routing,
solver-validated quizzes, shadow mode, derived weekly/monthly views.

## Quick start
    pip install -r requirements.txt
    python -m pytest -q tests                 # 90 tests, no API key needed
    python run.py daily --demo                # offline demo (mock LLM, sample feeds)
    python run.py approve-all                 # demo shortcut; normally approve in the admin site
    python run.py weekly                      # writes out/weekly_<date>.md
    python run.py admin                       # admin site at http://localhost:5050 : Review queue, Editorials, Publish
    python run.py daily-digest --lens UPSC_PSC   # per-exam digest (also weekly / monthly; lenses in config/lenses.yaml)
    python run.py gold                        # regression-test the note prompt on gold/*.json

## Choosing the AI provider (Claude or Gemini)
    # Gemini (Windows PowerShell; Mac/Linux use export):
    setx GEMINI_API_KEY "your-key"            # then open a NEW terminal
    # Claude:
    setx ANTHROPIC_API_KEY "your-key"
    # Force one when both keys exist (default 'auto' prefers Claude):
    setx GK_PROVIDER gemini                   # anthropic | gemini | mock | auto
    python run.py check-llm                   # tiny test call: confirms provider, key and model names
Model names change often. Override with GK_GEMINI_CHEAP_MODEL / GK_GEMINI_STRONG_MODEL (or GK_CHEAP_MODEL / GK_STRONG_MODEL
for Claude) and re-run check-llm. The defaults are guesses from Google's docs, so verify them in Google AI Studio.
Check your Gemini plan's rate limits and data-use terms (free tiers can be rate-limited and may use your inputs differently).
Free tiers allow only a few requests per minute (e.g. 15/min on a flash-lite model) and a daily cap. The system copes:
- Calls are throttled per model (GK_GEMINI_RPM, default 10) and relevance scoring is batched (GK_RELEVANCE_BATCH, default 10 articles per call).
- On a 429 it waits the delay Google asks for. If the DAILY quota is used up, the run stops cleanly and the next run resumes where it left off.
- A failed batch or note is logged (runs table: score_error / note_error) and skipped, never crashing the run.
- GK_MAX_ARTICLES (default 120) caps how many new articles are scored per run. A first run takes roughly 10 minutes on a free tier.
Rate-limited attempts do not count toward GK_CALL_CAP.
If no key is found the system falls back to the MOCK model and warns you; the daily stats line shows `provider`.
After switching providers, ALWAYS run `python run.py gold` before trusting output.

## Real run
    python run.py daily                       # uses config/sources.yaml

## Flow (gk/pipeline.py)
fetch (RSS, avoid-list filter) -> cluster (one note per event) -> relevance filter (cheap model)
-> note + claim extraction (strong model) -> verify (grounding, tier-1/2-source rule, routing)
-> quiz + blind solver check -> pending queue -> you approve -> weekly/monthly derive from approved data.

## Reader website (the public-facing site)
    python run.py build-site                  # writes site/index.html + site/data.js from APPROVED notes only
Open site/index.html in a browser, or upload the `site` folder to any static host (GitHub Pages, Netlify, ...).
Menu: Today (daily cards, date strip) | Weekly | Monthly | Tests | Editorial | Saved, plus a section row
(Economy, Polity & Administration, International, Science & Tech, Environment, Defence, Awards & Sports, Reports & Indices).
Filters by exam lens and priority, search, expandable cards (exam angle, lens points, sources), star-to-save, quizzes by
section/period with score history. Section menu = GROUPS in gk/site.py; colours/layout = gk/site_template.html.
Workflow: daily run -> approve in the admin site -> Publish tab (or build-site) -> upload. The admin site (python run.py admin) is the only admin tool: no Streamlit anywhere.

## Sections
Every note is assigned one of a fixed list of sectors (gk/sectors.py); the model is told the list and anything it still invents is mapped onto it.
Run `python run.py fix-sections` once to re-map notes saved before this fix, then rebuild the site.

## Coverage (so no section stays empty)
Notes are chosen round-robin across sectors, best story first inside each, so Awards/Science/Economy etc. all get slots (GK_MAX_NOTES, default 20).
Up to GK_MAX_ARTICLES (default 300) new articles are scored per run; anything unscored is picked up by the next run.
config/sources.yaml has topic feeds for RBI/economy, awards, science and reports. `python run.py coverage --q nobel` shows where a story went
(not fetched / not scored yet / dropped / kept without a note yet / has a note) plus feed errors.

## Saving daily quota (free tiers)
Per note: 1 strong-model call (the note) + 1 cheap call (3 quiz questions) + 1 cheap call (blind check of all questions).
GK_QUIZ_TIER (default cheap), GK_QUIZ_PER_NOTE (default 3). If a run says `daily quota used up`, progress is saved: run it again after the
quota resets, or `set GK_GEMINI_STRONG_MODEL=<another model>` (each model has its own quota). Compare with `python run.py gold`.

## Freshness
Articles older than GK_MAX_AGE_DAYS (default 3) are skipped at fetch time and stale unwritten ones are dropped at the start of each run,
so old explainers never appear as today's news. `coverage --q word` now shows each article's published date.

## Business section and GD topics
Business & Corporate is its own menu tab (Economy now = Economy + Banking) with its own Google News feed.
GD Topics: `python run.py gd` (or "Generate from approved notes" in the admin site's GD Topics tab) drafts up to 3 topics (GK_GD_PER_RUN) from
approved High/Medium notes: topic, context, 4 points for, 4 against, facts to quote (numbers not in the note are dropped), opening and closing lines.
They start as pending: review/edit/approve in the admin site, or write your own there. Approved topics appear in the site's GD Topics tab.
GK_GD_TIER (default cheap) chooses the model tier.
Suggest your own topic: in the admin GD Topics tab type a topic (and optional notes/facts) and click "Generate GD with AI". It drafts the same structure
as pending, using related approved notes as verified context. Numbers in "key facts" must come from your notes or those approved notes; other
statements are not fact-checked, so such drafts carry an "AI-written from your topic: verify facts" tag until you approve them.

## User accounts (login) - public server
    python run.py build-site                  # (the admin site also rebuilds it automatically)
    python run.py serve                       # http://localhost:5080 : the site + signup/login/Google sign-in
Accounts: email+password (scrypt-hashed, 8+ chars, lockout after 5 wrong tries) or Google sign-in. Signup asks name, email, password, age band
(not exact age; nobody is blocked), optional target exams, privacy-policy tick (version + time stored). Google users complete the same profile step once.
Logged-in users get bookmarks and test history saved to their account across devices; they can download their data or delete their account.
Google sign-in: create an OAuth client (type Web) in Google Cloud Console, add your site address (e.g. http://localhost:5080) under Authorized JavaScript origins,
then `setx GK_GOOGLE_CLIENT_ID "<client id>"` and restart `serve`. Without it the Google button is hidden.
Before going public: serve over HTTPS and set GK_SECURE_COOKIE=1; set GK_CONTACT_EMAIL; have the draft privacy page (privacy.html) reviewed.
Email verification and password reset: signup emails a confirmation link (48 h); the account box shows "Please confirm your email" with a
Resend button. "Forgot password?" on the login box emails a single-use reset link (1 h) that signs out every other device; it answers the same
way whether or not the email has an account. Logged-in users can change (or, for Google accounts, add) a password. If someone signed up with an
email they never confirmed and the real owner later uses Google sign-in, the unconfirmed password and its sessions are removed.
Email sending (pick one): `RESEND_API_KEY` (HTTPS, works on Render's free tier, which blocks outbound SMTP) or `GK_SMTP_HOST`/`GK_SMTP_PORT`/
`GK_SMTP_USER`/`GK_SMTP_PASS`; plus `GK_MAIL_FROM`. Without one, emails are only printed in the server log. Set `GK_PUBLIC_URL` to your
site's https address so links in emails point to it. Per-IP limits: 6 emails / 15 min, 10 signups / hour, 30 logins / 15 min.
Not built yet: paid-plan gating (the `plan` field exists, default 'free').
Opened as a plain file (no server) the site still works, just without accounts.

## Duplicates (one event = one note)
1. Headlines are clustered by word overlap, including differently worded ones (needs 5+ shared words), comparing against every title in a cluster.
   Only stories from the last GK_CLUSTER_DAYS (default 2) are merged, so an old story cannot swallow a new development.
2. Before notes are written, one cheap AI call groups headlines that are the same event, including repeats of stories already covered in the last 3 days
   (those are dropped, no second note). Follow-ups with a new development are kept apart.
3. A note is written from up to 3 sources of the same event; if they disagree on a figure the note names each source.
`python run.py coverage` now also prints a BY DAY table (fetched / kept / notes / approved) and the stats of recent runs, including 'stopped' when the AI quota ran out.

## Quota and overload handling (Gemini free tier)
- Notes are written GK_NOTE_BATCH (default 3, max 4) events per AI request, so a daily request limit yields about 3x more notes. Events the AI skips stay waiting for the next run.
- If the strong model's DAILY quota is used up, the run continues on the cheap model's separate quota (GK_GEMINI_FALLBACK=0 turns this off); the stats line then shows `fallback`.
- Google 503 "high demand" errors are retried with growing waits (15s, 30s, 60s...) and do not count against GK_CALL_CAP.
- Catch-up after a bad day: `set GK_MAX_NOTES=30` for one run. `python run.py coverage` shows the waiting backlog per sector.

## AI usage and cost
Every run prints a line like `AI usage: 41 calls, 312,400 tokens in / 88,100 out. Estimated cost on a paid plan: $0.52 (about Rs 47 at an assumed Rs 90/USD)`.
Usage is logged for the daily run, `gd`, and the admin site's GD/editorial generators; `python run.py coverage` shows a table per day plus the month so far.
On the free tier you pay nothing: the estimate is what the same usage would cost on a paid plan. Prices and the assumed exchange rate live in config/prices.yaml
(check Google's pricing page; set GK_USD_INR to override the rate). A model with no price listed is shown as "no price set".

## Static GK and Banking Terms (reference data that stays until it changes)
Admin site -> Static GK tab. Categories: Banking Terms (glossary), Countries, States & UTs, Important Days, Books & Authors, Awards, Organisations & HQ,
Constitution, India's Rank in Indices, Union Cabinet; you can create more. Every entry has a source, an "As on" date and a change type
(stable / yearly / volatile -> re-check due after 365 / 365 / 30 days). Only APPROVED entries reach the site (Static GK and Banking Terms tabs, with search);
every edit keeps a History row. Fast-changing (volatile) entries show a warning mark and are never used in quizzes.
- Fill it: type entries, `Load starter set` (banking terms, states & capitals, important days; loaded as pending: verify them), or bulk-load a CSV
  (`python run.py static-import --cat countries --file countries.csv [--approve]`; header templates in data/static_templates/).
- Quizzes: multiple-choice questions are generated automatically from approved rows (no AI, no cost) and appear in Tests as "Static GK: <category>".
- Rates such as the current repo rate are NOT pre-filled: add them in the term's "Current value" field from the RBI, with the date.
Not built yet: AI drafting of entries, and automatic "check for change" alerts from the daily news.

## Automatic policy rates + AI help for Static GK
Policy rates: every daily run checks new RBI (tier-1) releases that mention policy rates, reads the official text, and updates the Banking Terms
`Current value` (with an "as on" date and a source link). A rate is applied AUTOMATICALLY only when (1) the AI's quoted sentence is really in the
official text and contains the number, (2) it is in a sane range, and (3) the rates agree (SDF = repo - 0.25, MSF = repo + 0.25, Bank Rate = MSF).
Otherwise it waits in Admin -> Static GK -> Banking Terms -> "Policy rates" for Apply/Reject. GK_RATES_AUTO=0 makes every update wait for you.
Backup on policy day: paste the statement in that panel, or `python run.py rates --url <release link>` / `--file statement.txt`;
`python run.py rates` alone checks new releases now (schedule it every 30 minutes on MPC days, e.g. 10:00-14:00 IST). Each release is read once.
The RBI feed must be enabled (rbi_press in config/sources.yaml); full text is fetched with trafilatura, else the pasted text is used.
Filling other sections: Admin -> Static GK -> any category -> "AI help". Stable categories can be drafted from the AI's knowledge (pending, tagged "verify");
Union Cabinet and India's Rank in Indices can only be extracted from an official text you paste (rows not backed by that text are dropped).
Automatic filling: each daily run (real AI provider only) drafts GK_STATIC_AUTOFILL (default 2, 0 = off) batches into the Static GK review queue, working through
every stable category's focus areas once (countries by region, books, awards, HQs, constitution, days, banking terms). The admin button "AI: fill the next batch of ALL sections"
does up to 6 batches per click; `python run.py static-fill --calls 40` does many at once. Everything arrives PENDING, tagged "verify": open the category and approve.
CLI: `python run.py static-draft --cat countries --hint Asia --n 25` or `--cat india-indices --file report.txt --url <link>`. GK_STATIC_TIER (default cheap).

## Committees & Commissions (Static GK table)
Columns: Committee, Established, Functions, Current Head. Shown newest-first by Established; readers can click any column heading to re-sort any Static GK table.
Heads change, so the AI never drafts "Current Head": fill it yourself from the official source and set "As on". The Banking Terms glossary is NOT part of the Static GK tab (it has its own tab).
Search boxes hide non-matching rows/cards with an inline display style (a CSS rule had kept filtered Banking Terms cards visible).

## Fresh, correctly dated news
- Only today's and yesterday's reports are used (GK_MAX_AGE_DAYS=2; set 1 for today only). The real publication day of each article is stored.
- The AI is told today's date and each article's published date, and estimates how old the EVENT is. Stories whose event is older than GK_MAX_EVENT_AGE (default 3 days)
  are dropped even if a newspaper republished them today (explainers about old decisions, repeated rate news).
- The duplicate check remembers notes from the last 10 days, so an old story reappearing is folded into its original note instead of becoming a new one.
- A card shows "reported <date>" when the story was reported on an earlier day than the digest it appears in, so students are never misled about when something happened.
- Debug a suspicious card: `python run.py coverage --q "repo rate"` lists matching articles with their published dates and sources.

## Important Bills (Static GK table)
Columns: Bill, Passed Year, Passed By, Importance, Background. Shown newest first (click any heading to re-sort). A starter set of 25 landmark Acts (1976-2025) loads as PENDING with
"Load starter set": verify, then approve. AI filling goes from current to past: the focus periods are 2019-2023, 2014-2018, 2004-2013, 1991-2003, 1950-1990 (each batch is
checked: rows with a missing/non-4-digit year or a year outside the period are dropped). The newest period ("2024 or later") is never drafted automatically because the AI's
knowledge of the latest bills can be stale: pick it in "AI help" and verify, or paste an official list (e.g. from PRS Legislative Research) and use "Extract from text".

## Sobriquets (Static GK table)
Columns: Sobriquet, Given To, Type (Person / Place / Event / Plant / Animal), Field, Why Known. The site shows filter chips by Type and a search box. A starter set of 109
well-known exam sobriquets loads as PENDING with "Load starter set": verify, then approve. Quiz questions ("Who or what is known as ...?" and "What is the sobriquet of ...?")
take their wrong options from the SAME type (a person question never offers a city) and never offer another correct answer. AI drafting covers 8 focus areas (leaders, sportspersons,
writers/artists/scientists, Indian cities, world places, events, plants, animals); every drafted row must have one of the five types.

## Government Schemes, Chief Justices of India, Presidents of India (Static GK tables)
- Schemes: Scheme, Start Date, Sector, Purpose, Place, Description (Sector is an added column that drives the filter chips). Newest first by full launch date; click a heading to re-sort.
  Starter set of 52 well-known schemes (1975-2024) loads as PENDING: verify, then approve. AI drafting by sector (12 focus areas); every row needs a launch date with a real year.
- Chief Justices: CJI, No., Appointed Year, Tenure, Major Decisions. Presidents: President, No., Took Office, Tenure, Major Decisions. Both sort by No. (latest first). The starter set has all 53 CJIs and all 15
  Presidents with tenure dates. The AI cannot draft these two lists: to add or correct entries paste an official list and use "Extract from text". Sitting office-holders change: keep them current yourself.
- Date columns now sort by the full date (not just the first number), and number columns sort numerically.

## Editorial desk (admin-written)
Written and edited by you in the admin site's Editorials tab. "Draft with AI": type a topic/angle (plus optional notes) and click Draft editorial with AI;
a 450-650 word draft is saved as an UNPUBLISHED draft (GK_ED_TIER, default strong), using related approved notes as verified context. Numbers not found in
your notes or those approved notes are listed as flags. Nothing goes live until you edit and publish it. Written and edited by you in the admin site's Editorials tab (or `python run.py editorial --title T --file body.md --lens UPSC_PSC`).
Drafts stay private; only *published* editorials appear, at the top of the daily, weekly and monthly digests.
Tag an editorial with lenses to show it only in those exam views; no tag = every view. The AI pipeline never creates or edits editorials.
Admin site: `python run.py admin` runs it on 127.0.0.1 only. The public server (`serve`) also hosts it at /admin; there GK_ADMIN_PASSWORD is required
(without it /admin only answers requests from the same machine), and 10 wrong passwords from one IP block it for 15 minutes.

## Hosted admin: the everyday workflow
Admin -> Daily workflow tab. Each button starts a background job on the server and streams its log into the page (one job at a time):
- Everyday update (all steps): news pipeline -> GD topics -> rebuild site files -> GitHub backup (if set up). Then review in the other tabs.
- Or one step at a time: News pipeline only, Draft GD topics, Fill Static GK, Check RBI rates, Rebuild site files, Back up to GitHub.
"Today" is India time (GK_TZ_OFFSET_MIN, default 330), not the server's UTC date. Jobs and their logs are kept in the `jobs` table.
Automate it: `curl -X POST https://<backend>/api/admin/jobs -H "Authorization: Bearer $GK_ADMIN_PASSWORD" -H "Content-Type: application/json" -d '{"kind":"everyday"}'`
from any scheduler (cron-job.org, GitHub Actions, Render Cron).
The Vercel site shows the bundled site/data.js instantly, then swaps in live content from /api/content, so approvals show up without a redeploy.
GitHub backup: set GK_GITHUB_TOKEN (fine-grained, Contents read/write on this repo only) and GK_GITHUB_REPO=owner/name (GK_GITHUB_BRANCH, default main).
It commits site/data.js; an empty database re-seeds itself from that file (notes, quizzes, editorials, GD topics, approved Static GK).
Storage: SQLite lives at GK_DB. On a host with an ephemeral disk (Render free), the database, and with it user accounts and anything pending,
is wiped on every restart or deploy. Mount a persistent disk and point GK_DB at it, or move user data to a hosted database.

## Exam lenses
config/lenses.yaml defines UPSC_PSC (analytical), SSC_BANKING (quick facts + memorise line), DEFENCE, MANAGEMENT (discussion point).
Each note carries one-line lens_points; digests re-render the same approved notes per lens - no extra AI cost.

## Gold-note regression test
gold/*.json hold hand-approved cases (article + facts a good note must contain). `python run.py gold` re-runs the note prompt
and checks claim grounding, fact recall, invented numbers and bullet count. Run it after every prompt/model change.
The 3 included cases are DEMO placeholders: replace them with ~30 of your own approved notes.

## Safety rails
- Shadow mode by default: nothing is published until approved. GK_AUTO_PUBLISH=1 lets route=auto items through.
- Every claim must quote its source sentence; digits must match; ungrounded critical claims -> deep_review.
- Daily LLM call cap (GK_CALL_CAP) stops runaway cost; runs are idempotent (re-running adds nothing).
- Source failures are logged to the runs table (stage fetch_error) and never stop the run.

## Next steps (not built yet)
1. Verify/replace feed URLs in config/sources.yaml; add MEA, Parliament, SC, SEBI, NITI, UN/IMF/WB, BSE/NSE.
2. Build 30 gold notes; add a regression test that re-runs prompts against them.
3. Replace the demo gold cases with your own; swap token-overlap clustering for embeddings (gk/cluster.py, same interface).
4. Add PYQ-based relevance calibration, per-lens notes, events calendar, PDF export, user quiz analytics.
5. Scheduler: cron / GitHub Actions at ~5 AM IST; alert on fetch_error rows.
