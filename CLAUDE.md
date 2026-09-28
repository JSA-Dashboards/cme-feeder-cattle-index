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

`scripts/daily_update.ps1` runs at **07:30 and 13:00** (Task Scheduler), plus
`scripts/cme_pull.ps1` at **10:15** for CME's print alone. Two runs because
85% of a sale day's head is fetchable by 07:30 but 95.8% by noon — El Reno
publishes its previous-day sale at a median +1 day 11:13.

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

## Backends

`snowflake_db.py` abstracts SQLite (local) vs Snowflake (`USE_SNOWFLAKE=1`).
Production reads **Snowflake** — `JSA.CME_FEEDER_CATTLE`. The local
`data/mars_history.db` is the working copy the pipeline builds and pushes.

Local tables, approximate sizes:

    mars_sales 32.7k   fci_daily 941   cme_ftp_daily 3k   cme_ftp_brackets 72k
    replacement_sales 73k   calf_sales 7.8k   hay_bids 2.5k   corn_bids 1.8k
    border_* / census_cattle_imports   distillers_bids 221

## Two copies of the dashboard

`app.py` here is the standalone twin. The DEPLOYED one is
`livestock-portal/apps/cme_feeder_cattle/app.py`. They are deliberately not
identical (this one calls `set_page_config`, loads `.env`, uses its own
palette), **but a logic fix belongs in both**. Shared modules must stay
byte-identical; `tests/test_no_drift.py` compares them.

Deployment is Streamlit **Community Cloud** at jsa-livestock.streamlit.app, not
Streamlit in Snowflake. The webhook returns 200 and does nothing — Ross must
reboot the app by hand for a change to go live.

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
- `data/mars_history.db` is tracked and ~26 MB. Known wart; do not add more.
- The index is the product. When in doubt about a change, check it against
  `cme_ftp_daily` before committing.
