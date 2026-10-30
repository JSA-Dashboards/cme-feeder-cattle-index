# cme-feeder-cattle-index

Reconstructs the **CME Feeder Cattle Index** from USDA AMS auction data and
publishes an estimate before CME does. JSA trades on this number and clients see
it, so the bar is "matches CME to the cent", not "looks about right".

Two competing desks publish the same estimate daily — **CIH** and **Compass Ag
Solutions** — which makes this one of the rare pipelines with a same-day
independent check. Use it. On 2026-09-14 both printed $341.71 and so did we.

## What good looks like

    2026-09-15   ours $342.7373   CME $342.7373
    2026-09-14   ours $341.5794   CME $341.5800
    2026-09-11   ours $341.7073   CME $341.7100

Agreement is to a fraction of a cent from 2026-08-28 on (when the direct-trade
component was added). If a change moves any of those, it is wrong — check before
committing, not after.

    .venv/Scripts/python.exe -m pytest tests/ -q      # all must pass

### When a date will NOT match, and it is not a bug

**AMS revises a report after CME has printed, and CME does not go back.**
2026-10-06 through 2026-10-19 ran 1-2 cents ABOVE CME while both sides of the
run were exact. Nothing in this repo was wrong. McAlester OK (slug 1827) was
published with 26 qualifying head on 10/06 and USDA later cut it to 21 — the
800 lot (2 head @ $260.19) and the 850 lot (2 head @ $255.91) withdrawn, and
the 750 lot corrected from 5 head @ $310.16 to 4 @ $313.97. Same barn on
10/13, 8 head cut to 6. CME's file still carries the originals.

The proof is our own git history, not a code reading: `data/mars_history.db`
at 3a0d264 (2026-10-09) holds those four lots, and they aggregate to
26 head / 749.62 lb / $321.62 — which is CME's published McAlester row to the
cent. Restore them and the whole run collapses, worst |diff| 0.0223 -> 0.0048,
i.e. exact everywhere at CME's 2dp. Dropping them is `store_slug_rows()`
working as designed (1952483), so "fixing" this means re-adding lots USDA has
withdrawn.

CLAUDE.md already said this reconstruction tracks CME's FINAL value and
differs from CME's FIRST print by whatever had not landed yet. This is the
mirror image: CME's first print can be SHORT of the truth, and its final print
can be LONG of it. What is actually established is that CME had not restated
10/06 as of the 2026-10-23 pull, 17 days after the sale and well after the AMS
revision — not that CME never restates. Re-pull before assuming a date is
still off.

**Read the window boundaries before believing any other story.** A wrong sale
day shows up on exactly the five index dates whose 7-day window contains it.
Here sale day 10-06 covers index 10-06..10-12 and sale day 10-13 covers
10-13..10-19 — which is the run, split at exactly the right place, with the
two sub-runs sized by their own head (5 head -> ~2.0 cents, 2 head -> ~0.7).
That alignment identified the two sale days before any barn was named.

## The index, per CME Rule 10203 (verified against the rulebook, not inferred)

- **7 calendar days ending on the index date.** Not business days.
- **Pound-weighted**: total dollars / total pounds. Never head-weighted.
- **700–899 lb Medium & Large #1 and #1-2 feeder STEERS**, 12 states.
- **Saturday and Sunday sales count as Monday** — Rule 10203.A.1 verbatim:
  "Saturday and Sunday sales shall be included in the sample as if all
  transactions occurred on Monday." A CME week has five index days, not seven.
  Friday is NOT merged into Monday; it reaches Monday's number only via the
  7-day window.
- **Direct trade reports are Friday transactions.** Multi-day sales are dated to
  the final day (`detect_final_sale_day`).
- **Do NOT exclude fancy / thin / fleshy / gaunt / full lots.** That exclusion
  was deleted by SER-8154 (22 May 2018) and CME's own explainer PDF is STALE and
  still lists it. Tested live: including those lots is what reproduces the
  published number; excluding them is 22 cents wrong. See the docstring on
  `qualifying_rows()` before "fixing" this.
- **Dairy/exotic/Brahma exclusion survives** and is satisfied by construction —
  AMS files those under their own class values, so the exact `class == "Steers"`
  match already drops them. Loosening that match to `endswith("Steers")` would
  pull dairy cattle into the index.

## Pipeline

    update_index.py     AMS auctions + direct/video -> mars_sales -> fci_daily
    backfill_ftp.py     CME's own published files   -> cme_ftp_daily/_locations/_brackets
    update_imports.py   everything else (see OPTIONAL below)
    app.py              the dashboard (see "two copies" below)

`scripts/daily_update.ps1` runs at **07:45 and 13:00** (Task Scheduler), plus
`scripts/cme_pull.ps1` at **14:15** for CME's print alone. Two runs because
85% of a sale day's head is fetchable by 07:30 but 95.8% by noon — El Reno
publishes its previous-day sale at a median +1 day 11:13.

The CME pull was at **10:15 until 2026-10-07 and never once caught a print**:
CME's files are written 13:04–15:05 Central, not 08:35–10:05 as
`README-schedule.md` claimed. The FTP `MDTM` timestamp is already Central and
had been read as UTC, so every derived time was five hours early. See
"The 14:15 CME print pull" there.

**THE CEILING SAID 14:04 UNTIL 2026-10-30 AND THAT WAS THE SAME ERROR, HALF
CORRECTED.** The pre-correction band was 08:05 / 09:04 / 10:05, and +5h gives
13:05 / 14:04 / **15:05** -- but 14:04 is the old MEDIAN plus five, not the old
worst case plus five, and that is the number that got written down as the
latest. Re-measured 2026-10-30 off 159 business-day index dates: min 08:05, p10 13:04, median 13:35, p90 15:05, p99 16:04, max 20:48. So 13:04–14:04 held only 50.3% of prints and called
42.1% of them late.

**`MDTM` IS WHEN CME WROTE THE FILE, NOT WHEN IT CAN BE READ.** Measured
2026-10-28: stamped 14:34:46, first successfully fetched 14:51:49, with the
path 550-ing as though absent in between. One observation, so treat the gap as
real but unquantified.

**ONE CONSTANT WAS DOING TWO JOBS**, which is how its value drifted unnoticed:
`PUBLISH_DEADLINE` gated both what `catch_up_dates()` will ASK CME for and what
the dashboard calls LATE, and those want opposite values -- early so the
afternoon pull still requests a 13:35 print, late so the page does not accuse
CME on an ordinary afternoon. Split on 2026-10-30 into `PUBLISH_EARLIEST`
(13:04) and `PUBLISH_OVERDUE` (16:05) in `index_dates.py`. Fetching is
unchanged; only the banner moved.

**Step order in the daily job is load-bearing.** Index first, then optional
ingests, then their push:

    update_index -> cme pull -> wal checkpoint -> push --critical-only
                 -> update_imports -> wal checkpoint -> push --optional-only

The optional ingests take ~8 minutes. Running them before the push put
third-party API calls on the index's critical path, where a hang would strand a
finished index unpublished. Do not reorder this.

CRITICAL vs OPTIONAL lives in `snowflake/02_migrate_data.py`, not in the
PowerShell — adding a table must not require a matching edit somewhere nobody
looks.

## The index must stay isolated

`recompute_fci_daily()` reads `mars_sales` with **NO WHERE CLAUSE**. Every row
there is treated as index-qualifying; the filtering happened once, on the way in.
One misrouted insert would put 400 lb calves at $475/cwt into the published
index with no error.

`calf_sales` (400–900 lb) and the feed tables exist for the dashboards and must
never reach it. `tests/test_index_isolation.py` enforces this structurally — no
index module may query `calf_sales`, `calf_sales.py` may write only its own
table, and `mars_sales` must contain only brackets 700/750/800/850.

ONE EXEMPTION, in `DIAGNOSTIC_MODULES`: `barn_report.py` reads `calf_sales` to
tell a barn that filed a report with no 700–899 lb cattle in it from a barn that
never reported. It is allowed because it cannot carry anything to the index — it
returns strings, nothing consumes them but a log line and a caption, and it
writes nothing. That is not taken on trust: the exemption is paid for by tests
asserting it contains no write verb and never names `calf_sales` and
`mars_sales` in one query. Weaken either and it belongs back under the ban.

## Backends

`snowflake_db.py` abstracts SQLite (local) vs Snowflake (`USE_SNOWFLAKE=1`).
Production reads **Snowflake** — `JSA.CME_FEEDER_CATTLE`. The local
`data/mars_history.db` is the working copy the pipeline builds and pushes.

Local tables, re-measured 2026-10-30 — every one of these had drifted since
it was written, `calf_sales` by 7x, so **re-measure rather than quote**:

    mars_sales 34.7k   fci_daily 985   cme_ftp_daily 3.1k  cme_ftp_brackets 73.8k
    replacement_sales 103k  calf_sales 56k   hay_bids 3.6k   corn_bids 3.6k
    border_* / census_cattle_imports   distillers_bids 323
    cme_archive_daily 3.6k   cme_archive_locations 43.4k

**`cme_archive_*` IS NOT THE SCORECARD.** It is CME's own yearly workbooks —
a weighted average CME recomputed later, which differs from the reported
index in `cme_ftp_daily` by up to 2.3374 in the older years and is identical
to 2dp rounding from 2021 on. Nothing in it is named `fci_value`, by design,
so no query can quietly average the two series together. It is worth having
for 2013–14, which exist in no other source: CME's as-printed daily files
floor at 2015. Loaded by `backfill_cme_archive.py` (manual, needs `xlrd`
via `uv run --with`), pushed to Snowflake as the last two OPTIONAL tables.

### The Long History panel — STAFF ONLY, and the one rule it runs on

Added 2026-10-30, after Ross asked whether the archive could be seen on the
dashboard. It sits on the Index tab between Volume vs Seasonal Norm and
Weekly Rundown, behind `portal_view.staff_expander(ARCHIVE_LABEL)` in the
portal and a plain `st.expander` here. `tests/test_no_drift.py` asserts that
asymmetry in both directions, as it does for the conditional study.

**`archive_history.py` NEVER RETURNS A BLENDED SERIES**, and that is the
whole design rather than a style note. It returns the restated series and
its agreement with CME's as-printed file as two separate things, and
`tests/test_archive_history.py` asserts the separation structurally — the
guard is on the SELECT list, not the identifier, because the caption
deliberately says "nothing in these tables is named fci_value" and a guard
that banned the word would punish the warning for doing its job.

Three different reasons a date can be missing, and **adding them together is
how the panel would be wrong**:

- **Before the as-printed era** (523 dates, 2012-12-31..2014-12-31) — no
  collection could have them. This is the orange segment on the chart.
- **A hole inside the era** (9 on file) — a gap in OUR copy of CME's files,
  not in CME's publishing. Worth a `backfill_ftp` re-pull.
- **After the last as-printed date** — today's print, not fetched yet. Not a
  gap, and counting it as one would fire the warning every afternoon.

`rounding_era_from()` derives the 2021 boundary rather than hard-coding it:
that is a fact about CME's restating practice, not a constant of this repo,
and a literal would quietly over-claim if 2020 were ever reissued cleanly.

Chart colours were validated, not eyed — `#0693e3`/`#e8833a`, ΔE 31.3 normal
and 24.2 protan. 2015-onward keeps JPSI_BLUE because that is the colour the
same era already wears on Index Trend; a period that changed colour between
two charts on one page would read as two different series.

## Two copies of the dashboard

`app.py` here is the standalone twin. The DEPLOYED one is
`livestock-portal/apps/cme_feeder_cattle/app.py`. They are deliberately not
identical (this one calls `set_page_config`, loads `.env`, uses its own
palette), **but a logic fix belongs in both**. Shared modules must stay
byte-identical; `tests/test_no_drift.py` compares them.

Deployment is Streamlit **Community Cloud**, not Streamlit in Snowflake. The
webhook returns 200 and does nothing — Ross must reboot the app by hand for a
change to go live.

**THERE ARE TWO DEPLOYMENTS, AND THIS LINE SAID ONE UNTIL 2026-10-28.** Both
run the same repo on `master` and differ only in the main file the console is
pointed at:

| main file | URL | who |
|---|---|---|
| `Home.py` | jsa-livestock.streamlit.app | staff |
| `Client.py` | jsa-livestock-client.streamlit.app | clients |

`Client.py` is six lines: it calls `portal_view.set_client()` and then
`runpy.run_path`s `Home.py`. Same fifteen dashboards, same CME Feeder Cattle
Index page, same Snowflake data, with the working tools and raw-data tabs
taken out. So **a correction to a published number is stale on both** until
each one restarts, and "reboot the dashboard" is ambiguous unless somebody
says which. The portal's own CLAUDE.md has the deploy mechanics and puts it
as "a key rotation is now two consoles, and a push is two reboots".

**But a sleeping app needs no reboot, which is the part that is easy to get
backwards.** Checked on 2026-10-28 rather than assumed: the client app was
serving "This app has gone to sleep due to inactivity", so it had no process
and no warm caches, and it cold-starts on whatever Snowflake holds the next
time a client opens it. Rebooting it would have achieved nothing, and waking
it to prove the point would have left a process idling. The staff app was
awake and did need the reboot. **Read the moon icon in the console, or just
open the URL — do not reboot both on principle.**

Caches are the whole reason a reboot matters here: the page's loaders are
`@st.cache_data(ttl=300)` and `ttl=3600`, so an awake app serves the old
number for up to an hour on its own. That is also the honest answer to "is a
reboot even needed" — it is a question of how long, not whether.

**"Delete app" sits directly below "Reboot app"** in the ⋮ menu. Click by
element reference, never by coordinate. The confirm dialog reads "This will
disrupt all current users". The portal's CLAUDE.md adds: ask before rebooting,
because Ross writes the daily letter on the staff app.

### The 6 State tab, and the one thing it changed about this process

Ported from the portal 2026-10-19 at Ross's request, a week after it shipped
there. `six_state.py` sits at this repo's root and is **byte-identical** to
`livestock-portal/apps/cme_feeder_cattle/six_state.py`; the module, its test
and the AMS fixture are all three in `SHARED` in `tests/test_no_drift.py`.
The fixture is in that list on purpose — `test_six_state.py` pins eighteen
cells against a real AMS payload, so two fixtures free to drift would be two
suites quietly asserting different numbers about the same week, both green.

Every rule the arithmetic follows is documented in the portal's CLAUDE.md
under "The 6 State Weekly Feeder Cattle Prices tab", and each has an
alternative that renders perfectly and prints a different number. Read it
before touching `six_state.py`.

`tests/test_six_state.py` is shared verbatim, so it probes for
`apps/cme_feeder_cattle` then the repo root and sets `APP_DIR` to whichever
answered. Its three page-level tests therefore read **this** repo's `app.py`
and enforce this copy's own wiring: the schema passed at the call site, the
`if tab_six.open:` guard, and `st.html` rather than `st.markdown`.

**THIS TAB IS THE FIRST THING IN THE STREAMLIT PROCESS HERE TO READ
`marsapi`.** The five modules that already use `MARS_API_KEY` —
`border_reports`, `corn_bids`, `feeder_sex_mix`, `feed_bids`,
`replacement_reports` — are ingest scripts the droplet runs, and `app.py`
imports none of them. Locally `load_dotenv()` supplies the key from `.env`.
**If this app is ever deployed, the key must be added to its own secrets
console**; without it the tab prints one error naming the secret and the
other three tabs are unaffected.

### The market summary — what is computed and what is written

Added 2026-10-19, after Ross asked why lighter calves were bringing a bigger
premium over the month and whether the page could explain the week. Two
halves, and the split is the point: `six_state.summary()` computes WHAT
moved, and a box on the page takes the WHY from a person. Nothing on the
page infers a cause. That is the arithmetic/editorial line the daily letter
already runs on, and this file records a note deleted from the beef-trade
chart for being a plausible wrong cause.

**THE HEADLINE IS THE FIXED-PANEL MOVE, NOT THE AVERAGE ACROSS STATES, and
that is the whole correctness of the panel.** A state entering or leaving a
bracket moves a head-weighted average with no barn changing its bid.
Nebraska went from **none** of the 5-600# head in the week of 8/30/2026 to
**62%** of it by 10/11, at the dearest light price in the panel — on its own
that was **+27.45 of an apparent +49.56**, 55% of the move. `bracket_move()`
returns `reported`, `same_panel` and `roster`, the summary leads with
`same_panel`, and `roster_caution()` prints both numbers when they disagree
by more than `ROSTER_MATERIAL` ($2.00/cwt).

**The eighteen published cells carry none of this** — each compares a state
against itself, and a state that did not report prints N/A. Only an
aggregate across states is exposed, which is exactly what a summary is.

Three more things in it that look fussy and are not:

- **The slide is a MEDIAN ACROSS STATES, never the gap between two
  head-weighted averages.** The two ends of the ladder are not drawn from the
  same roster in a given week, so differencing two averages measures the
  roster as much as the ladder. Over the three weeks to 10/17/2026 the
  head-weighted spread moved 91.80 → 97.50 → 92.90 while the median sat at
  80.88 → 80.42 → 80.81. The flat series is the honest one.
- **`MIN_WEEKS_FOR_EXTREME = 8`.** `base_caution()` says when the month-ago
  week is the widest or narrowest on file, which matters: the week of
  8/30/2026 was the narrowest of 26 and every month change measured off it
  overstates the move. But on a short file EVERY week is an extreme — on the
  two-week test fixture the caution fired on both, which is a banner that
  always fires and so is one nobody reads on the week it matters.
- **`leader_sentence` names the states that went the OTHER way.** Over the
  month to 10/10/2026 the light end outran the heavy end in Iowa, Kansas and
  Missouri and LOST to it in Arkansas and Oklahoma. A panel figure alone
  reports "lighter calves led" and hides that it was false in two of five.

`six_state.py` still imports exactly `{datetime, concurrent, requests}` and a
test pins it, which is why `_median` is written out rather than imported from
`statistics`, and why corn and the feeder board are **not** in here even
though both moved the same way over this window (corn −10.7%, the index
+4.4%). Consistent is not causal, and the module has no business holding
either series.

### The written cause is PORTAL-ONLY, deliberately

It is saved in `JSA.LETTER.DRAFTS` under `KIND = "sixstate"`, keyed on the
**week** it describes — the store `weekbase` and `settlelog` already share.
No new table, and it survives the reboot that wipes `out/`, which is how a
nearly finished Friday letter was lost once already.

`cme-feeder-cattle-index` gets the computed half and **no box**: it has no
`letter/` package and so no durable store, and a box there would take a note
and lose it on the next restart. `tests/test_no_drift.py` asserts the
asymmetry in both directions so it stays a decision rather than something
that rotted.

Keyed on the week and not on today, which is pinned by AST because the save
path needs Snowflake and cannot run on a developer machine — `enabled()` is
false without `USE_SNOWFLAKE`, so the box hides itself locally and says why.
**A live save is only exercisable on the deployed app.**

### Three page-level defects that only a browser found

All three passed `py_compile` and the full suite, and two of them passed
`get_page_text` as well.

- **`_h` as a helper name is already taken.** The page rebinds `_h` to an int
  in a plot loop a thousand lines earlier (`for _w2, _h in _pts`), so every
  call raised `TypeError: 'int' object is not callable` and the whole summary
  panel vanished. It is `_six_esc` now. **Both copies had it**, because the
  loop is in both.
- **A LITERAL TAG INSIDE A `<style>` BLOCK ENDS THE BLOCK.** The comment
  explaining the state banding said "one class per `<tr>`", and the HTML
  parser stopped the style element there — so the table's entire stylesheet
  was dropped and it rendered as an unstyled grid with no card, no border, no
  font size and no banding. The data was all correct, so `get_page_text` read
  it back perfectly. Found by measuring `.six-wrap`'s computed background and
  getting `rgba(0, 0, 0, 0)` where a white card belonged.
  `tests/test_six_state.py` now scans every `<style>` block on the page for a
  tag start.
- **Four dollar amounts in one sentence is LaTeX.** `slide_sentence()` quotes
  four, so the panel renders through `st.html` like the table. The tab's own
  test already fails any `st.caption` here containing a `$`.

### The state banding

Ross's own sheet greys alternate states to separate them, and the table now
does the same: `_six_band(i)` puts `six-band` on every other STATE, grouped
by `len(BRACKETS)` rather than a literal 3, so adding a weight bracket moves
the banding with the states instead of cutting across them. Both blocks
change state on the same rows, so one class per row bands the left and right
halves together. The gap column is left clear so the two halves still read as
two tables rather than one wide banded one, and the shade is a tint
(`rgba(0,0,0,0.04)`) rather than a palette colour so it sits on whatever
`CARD_BG` each repo uses without a second constant to keep in step.

## Traps that have cost real time

Every one of these failed **silently**. None raised.

- **MARS report sections are PATH segments**, not query params:
  `/reports/3486/Report%20Volume`. Passing `section=` is accepted and ignored,
  returning the header with no rows. This was once mistaken for "AMS publishes
  no structured data".
- **Section names differ by report family.** Grain and co-products use
  `Report Detail`; Direct Hay uses `Report Details` (plural). Wrong one = header
  only, no error.
- **Field names differ by report family too.** Direct Hay has no `report_date`
  at all — only `report_begin_date` / `report_end_date`. Asking for the wrong
  one skipped every row and reported "0 rows seen", exit 0.
- **CME's FTP file is FIXED-WIDTH and the layout changed on 2026-09-14.** The
  location column was renamed and widened until numeric values touch
  (`715.24377.40`), the row-level average-price column was dropped, and the
  REPORTED INDEX label vanished. `cme_ftp.py` now derives column positions from
  the file's own header — keep it that way.
- **Primary keys must include every field that varies within a report.**
  `corn_bids` lost 76% of its rows to a missing `trade_loc` (1,912 ingested,
  448 stored). `hay_bids` lost 444 to six missing fields. Both were found only
  by comparing an ingested count against a stored count.
- **Mixed units.** AMS quotes hay Per Ton, Per Bale, Per Bundle and
  Per Point/Ton. Averaging across them blended $4.50 bale prices into a figure
  labelled $/ton. Always filter the unit.
- **Zero is not a price.** 37% of Direct Hay rows carry a literal `0` on
  Ask/Offer lines. Store NULL. Texas and Missouri are 100% zero and have no
  usable hay price at all.
- **A date offset in `mars_sales` is NOT an offset in the index.** El Reno is
  stored on Tuesday and Clovis on Wednesday, one day earlier than CME files
  each, every week — and it is already corrected. `recompute_fci_daily()`
  buckets through `shifted_bucket_date()`, not the raw `report_date` column,
  so a diff of `mars_sales.report_date` against `cme_ftp_locations` prints a
  standing offset that the index never sees. Diffed raw, 2026-10-06 is 1,147
  head out; diffed through `bucketing`, it is 5. Cost most of a session
  chasing El Reno, and the giveaway was there from the start: the offset is
  present every week including the ones that match CME exactly.
- **As-fed is not dry matter.** Wet distillers at $52/ton is 65-70% water —
  $160/ton of actual feed, near parity with dry at $166. Mixing bases is a 3x
  error. Use `feed_bids.dm_price()`.

## How these get caught

Not by reading code. By **comparing a number against an independently derived
number** — our index against CME's published file, rows ingested against rows
stored, a state average against a hand query.

Which means the check itself must be able to fail. Three checks written during
this work could not: one printed the same variable under both labels, one had a
compound assertion that was a tautology, one banned a string in comments rather
than in queries. Each is now paired with a test that feeds it a known violation.
If you add a guard, prove it fails on bad input before trusting it.

## Rules

- Never commit `.env`, `*.p8`/`*.pem`, or secrets.
- `data/mars_history.db` is tracked and **44.7 MB** (it was ~26 MB when this rule
  was written; it grows with every ingest). Known wart. The rule is **do not add
  another large binary** — not "avoid committing this one", because committing it
  is how the working copy stays shareable. Re-measured 2026-10-22: 65 commits
  touch it and `.git` is 249 MB, so git deltas the unchanged SQLite pages well and
  each commit costs about 3.8 MB amortised rather than another 45. **Every figure
  in this bullet has drifted at least once — re-measure rather than quote.** The
  only other tracked file over 1 MB is `data/Feeder Cattle Info Ross.xlsx` at
  6.1 MB, recorded here as `data/Feeder` until 2026-10-22 because the name was
  truncated at the space.
- The index is the product. When in doubt about a change, check it against
  `cme_ftp_daily` before committing.
