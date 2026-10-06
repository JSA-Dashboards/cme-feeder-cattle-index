# The daily schedule

`scripts/daily_update.ps1` refreshes the FCI reconstruction and publishes it to
Snowflake, which is what the dashboard reads. It runs from Windows Task
Scheduler as **`JSA FCI daily update`**.

## Three triggers, two scripts

`daily_update.ps1` at 07:45 and 13:00 (below), plus `cme_pull.ps1` at 10:15.

### The 10:15 CME print pull

CME publishes each index date's file the NEXT business day. Measured from their
own FTP `MDTM` timestamps over 14 consecutive files:

    earliest 08:35    median 09:05    latest 10:05    (Central)

So **07:30 was always too early** -- that run could never carry yesterday's
official number -- and 13:00 was the first poll that saw it. At the 07:45
trigger it is still too early on any ordinary day, though no longer
*structurally* so the way 07:30 was: CME's earliest file in the sample below
lands 08:05, and the morning run does not reach its own CME step the instant it
starts, so a rare early file can still be caught. Nothing in the logs times that
step -- only the run's start and finish are stamped -- so this is "can", not a
rate. The published print and the whole forecast scorecard therefore ran about
four hours behind CME every morning: a 09:05 print did not reach the dashboard
until 13:05.

10:15 clears the 10:05 worst case by ten minutes. The job fetches, stores, and
pushes only `cme_ftp_daily`, `cme_ftp_locations` and `cme_ftp_brackets` -- about
25 seconds, against the main pipeline's 12 minutes. It recomputes nothing: CME's
value does not feed the estimate, it is what the estimate is scored against.

It has **no healthcheck ping on purpose**. It is an accelerator, not a
guarantee: if it fails, the 13:00 run pulls the same file with a wider lookback
and the only cost is that the print appears when it always used to.

## Two triggers, one script

| Trigger | Purpose |
|---|---|
| **07:45** Central | The morning call — the number that goes out, and the one comparable to CIH's and Compass's morning sheets. |
| **13:00** Central | The settled pass. |

The script needs no argument to tell them apart: `snapshots.run_slot()` reads the
clock and files anything before 11:00 as `am`, the rest as `pm`. The 11:00
boundary leaves room for a morning run deferred by `StartWhenAvailable` without
it being misfiled as the afternoon pass.

### Why an afternoon run exists

Two measurements, both taken 2026-09-09:

- **CME posts its own file between 08:05 and 10:05 Central** (median 09:04,
  n=17, from the FTP server's `MDTM` timestamps). The 07:30 run therefore
  *structurally could not* see the file published that morning, so "Last CME
  Print" was always a day stale. The 07:45 run clears 08:05 only once its own
  CME step is reached, several minutes in, so it may occasionally catch an
  early file -- unlikely, but not the structural impossibility 07:30 was. It is
  less likely than at 08:00, by exactly the fifteen minutes the trigger moved.
- **USDA publication is only slightly unfinished at 07:30** — less than it
  first appears. Measured against the date a sale is BUCKETED into (not its
  USDA sale date, which is the framing that made El Reno look like a timing
  problem when it was a bucketing one), 96.17% of a bucket date's qualifying
  head is already published by 07:30 the next morning, rising to 98.41% by
  noon. So the afternoon pass recovers a real but small tail.

### Why 07:30 and not later

*Recorded 2026-09-09. The morning trigger has moved twice since — to 08:00 on
2026-09-29, then to 07:45 on 2026-10-01 — so read what follows as the case as
it stood then, not as current policy. The head-availability measurements still
hold; what changed is the decision, not the numbers. What did go stale with
those moves, and is corrected elsewhere in this file, is the deadline: the
arithmetic below is written against the old 08:15, which became 08:45 under the
08:00 trigger and is 08:30 under 07:45. That 08:15 is what made an 08:00 start
look impossible here.*

Asked and measured 2026-09-09, against bucket dates:

| Run time | Head available | Share |
|---|---|---|
| 07:00 | 51,096 | 95.41% |
| **07:30** | 51,503 | **96.17%** |
| 08:00 | 51,503 | 96.17% (**+0 head**) |
| 08:30 | 51,718 | 96.57% |
| 12:00 | 52,706 | 98.41% |

Moving to **08:00 gains nothing at all** — zero head across 246 reports and six
weeks. **08:30** gains 215 head, 0.40%, from six reports, worth one to two
cents on the roughly six days in forty-two that it affects. For comparison,
07:00 → 07:30 gains more (+407) than 07:30 → 08:30 does: 07:30 is already past
the steep part of the curve.

And the deadline forbids both anyway. At a 20-minute runtime, an 08:00 start
finishes 08:20 and an 08:30 start finishes 08:50 — both past 08:15. The
strongest single piece of evidence that 07:30 is not costing accuracy: on
2026-09-08 the frozen 07:30 call was $327.4306 on **9,829 head** against CME's
$327.4300 on **9,829 head** — an identical window, matched to six hundredths of
a cent.

Revisit this only if the deadline itself moves — which it has, twice. It went
to 08:45 when the trigger went to 08:00 on 2026-09-29, and back to **08:30** on
2026-10-01 when the trigger went to 07:45, so the estimate is in hand before the
morning email goes out. The brief window in which the paragraph above was moot
has therefore closed. Against 08:30, at today's 25-minute runtime, an 08:30
start finishes 08:55 — twenty-five minutes late — and even an 08:00 start
finishing 08:25 leaves five minutes and assumes zero start latency. A later
start is forbidden again, now by an earlier deadline rather than by the old
08:15. The 0.40% has not been re-measured either, so this section stays history
rather than a recommendation: the 08:30 run that would collect it (and catch
CME's own file in the morning rather than at 13:00) needs a deadline nearer
09:00, and the deadline has moved the other way.

`fci_snapshots` is written INSERT-OR-IGNORE per `(index_date, run_date,
run_slot)`, so the afternoon pass **cannot** overwrite the morning call it
exists to be compared against.

## Task settings that matter

| Setting | Value | Why |
|---|---|---|
| `WakeToRun` | **True** | The machine sleeps overnight. Without this the job waits for someone to wake the PC: on four of the five weekdays before it was enabled, the 20-minute run would have finished *after* the 08:15 deadline. |
| `StartWhenAvailable` | **True** | Covers a full power-off, which no scheduled task can wake from — the run then happens at next boot. |
| `ExecutionTimeLimit` | **PT90M** (PT20M on the CME pull) | Was `PT45M`. On 2026-09-10 the 13:00 run hung 16s in and Task Scheduler killed it at 13:45 with `0xC000013A` (terminated) — correctly, but silently. 90 minutes is 6x a normal 5.6-minute run and 6x the slowest legitimate one observed (14.5 min), so it still fails fast rather than grinding for hours. Do **not** raise it further: a long limit turns a hang into a wasted afternoon instead of an early failure. |

Wake timers are enabled on AC and this is a desktop with no battery, so the
`WakeToRun` setting is not silently vetoed by power policy. (On a laptop it
would be: wake timers are disabled on battery by default.)

## What the CME step maintains

`backfill_ftp.py` (run daily with a 10-day lookback) writes four tables from
CME's own files, all idempotent per date:

| Table | Contents |
|---|---|
| `cme_ftp_daily` | the published index, plus DAILY and SEVEN-DAY totals |
| `cme_ftp_locations` | per-location rows behind each date |
| `cme_ftp_brackets` | per-location **weight/grade brackets** — #1 and #1-2 Steers at 700-749 / 750-799 / 800-849 / 850-899 |
| (and the Snowflake push carries all of them) |

The bracket table exists because the row-level average weight hides the mix. An
804 lb average can be everything at 800-849 or a barbell of 700-749 and
850-899, and those mean different things for an index that only counts 700-899.
Coverage: **daily from 2022-01-01**, plus ISO weeks 33-41 back to 2016 for
seasonal comparison -- 1,463 dates, ~71,900 rows. The daily run extends it
forward. Widen the history with a scoped backfill rather than re-fetching the
whole 3,000-file archive; the 2022-onward continuous range was added because a
nine-week annual band cannot show WHEN a mix shifted, only that it did.

Verified by round trip: deleting one date's brackets and re-running the daily
step rebuilt them exactly (25 brackets, 1,647 head on 2026-09-01), so the job
maintains the table rather than merely leaving it alone.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything succeeded. |
| 2 | venv python missing. |
| 3 | `.env` missing (no `MARS_API_KEY`). |
| 5 | The refresh worked but the **Snowflake push failed** — local data is current, the dashboard is stale. |
| other | Propagated from `update_index.py`. |

`imp_exit` covers the Mexican feeder import refresh (`update_imports.py`: AMS
border reports + Census trade data). Like `cme_exit` it is logged as a warning
and never fails the run — neither source feeds the FCI estimate, so the worst
case is a stale *Mexican Feeder Imports* tab. It returns 1 only if **both**
sources fail, which points at a missing `MARS_API_KEY` / `CENSUS_API_KEY` or no
network rather than a bad day at one agency.

The two sources take deliberately different lookbacks: 30 days for AMS (these
publish same-day and are never revised) and **15 months** for Census, which is
mandatory rather than generous — Census revises prior months for months
afterwards, so a short window would freeze the first, provisional print.

A non-zero `cme_exit` is logged as a warning and does **not** fail the run: a
stale CME series is a smaller problem than skipping the publish, and CME's FTP
is a third party we do not control.

Logs land in `logs/update_<date>.log` and are pruned after 30 days.

## Dead-man's switch

**Two checks, one per slot.** This began as a cron limitation: a five-field cron
shares a single minute field, so 07:30 and 13:00 could not be expressed together
-- `30 7,13 * * *` means 07:30 and 13:30 and would have cried wolf every
afternoon. That collision lapsed for exactly one day, at the 08:00 trigger,
where both slots sat on minute 0 and `0 8,13 * * *` would have expressed both.
**At 07:45 it is back**: the minute fields differ again (45 and 0), and
`45 7,13 * * *` means 07:45 and 13:45. Because the two checks were kept through
the day they were merely optional, nothing had to be rebuilt. Keep them for the
reason that outlasts the arithmetic: a single check alerts identically whichever
slot went missing, and it blends two runs with different jobs into one history
-- the 2026-09-10 hang below is exactly the case you want named rather than
averaged. Configure the monitor with:

| check | cron | grace | alerts |
|---|---|---|---|
| morning | `45 7 * * *` | 45 min | 08:30 |
| afternoon | `0 13 * * *` | 45 min | 13:45 |

in `America/Chicago`, and put the ping URLs in `.env` as `HEALTHCHECK_URL_AM`
and `HEALTHCHECK_URL_PM`. A single `HEALTHCHECK_URL` is still honoured for both
slots as a fallback.

**That cron lives on Healthchecks.io, outside this repo.** Move the Task
Scheduler trigger without editing the check and the monitor goes on expecting the
old time, alerting every morning while the pipeline is perfectly healthy. Moving
the trigger is therefore a two-place change, and for most of this repo's life the
second place was one no test, no grep and no reviewer could see.

**`healthcheck_schedule.py` now reads it back.** The table above is no longer
documentation *about* the configuration — it is parsed, and compared field by
field against what the Healthchecks.io management API reports, so a divergence
fails `tests/test_healthcheck_schedule.py` instead of waiting for a morning that
goes wrong. It needs `HEALTHCHECK_API_KEY` in `.env` (step 4 below); without one
the live half is skipped and says so, while the repo-side half — this table
against `check_run.ps1`'s `$trigH`/`$trigM`, and each row against its own alert
column — still runs on every clone.

That closes the two-place problem in one direction only, and the limit is worth
stating: the guard binds this table to Healthchecks.io, and this table to
`check_run.ps1`. It does **not** read Windows Task Scheduler. Move the task and
edit neither file and everything here is green while the monitor is wrong. It
turns a three-way silent divergence into a two-way loud one; the third leg is
still a human reading section 1 of `check_run.ps1`.

The record so far is one for one:

- **2026-09-30 — self-inflicted.** The task had moved to 08:00 and the check
  still said `30 7 * * *`, so it went red on a healthy run.
- **2026-10-01 — HALF DONE, and still open at the time of writing.** The task
  moved to 07:45 and `check_run.ps1`'s deadline moved with it, to 08:30. The
  Healthchecks.io cron was NOT moved and still reads `0 8 * * *`.

  This is the incident `healthcheck_schedule.py` was written for, and it is the
  reason the guard reads the cron rather than trusting the ping. Expect the
  first run with `HEALTHCHECK_API_KEY` configured to be RED, naming this exact
  edit. That is the guard earning its place, not a bug in it.

  This one does not go red, and the reason is worth understanding rather than
  filing as luck. 09-30 moved the trigger LATER than the check expected, so the
  ping arrived after the deadline and the check fired. 10-01 moved it EARLIER:
  the check still expects a ping by 08:45 and a 07:45 run delivers one near
  08:10, comfortably inside. Moving a job earlier cannot trip a stale cron;
  moving it later always can.

  What it costs instead is tightness. Until the cron becomes `45 7 * * *`, a
  morning that produces NOTHING is reported at 08:45 rather than 08:30 -- the
  monitor is 15 minutes looser than the deadline this repo now judges against.

Changing the trigger means changing the check. Both entries stay: one is the cost
of forgetting in the direction that bites, the other is a reminder that the
direction which does not bite still leaves the monitor wrong.

Monitoring BOTH matters. The failure that prompted this was the 13:00 run
hanging on 2026-09-10 and being killed at its time limit. A killed process never
reaches its `/fail` line, so ABSENCE of a ping is the only signal available --
and a morning-only check would have stayed green straight through it.

A full run -- optional ingests included, so from 2026-09-12 on -- takes 13 to 26
minutes start to success ping, median 19. Re-derived 2026-10-01 over 20 runs:
13.43 min fastest, 26.03 slowest (2026-09-14), median 18.73.

**Which side of the wake boundary does 07:45 land on? The sleeping side.**
Measured 2026-10-02, the first morning on the new trigger: the machine slept at
20:29 the night before, WakeToRun woke it at 07:50:02 (+5.03 min) and the run
started 07:56:02 (+11.0 min). So 07:45 pays the wake penalty that 07:30 paid and
08:00 did not -- on 09-30 and 10-01 the machine was already awake before an 08:00
trigger and both runs started within three seconds of it.

That penalty follows the trigger wherever it goes, so moving earlier again buys
less than the clock suggests. The lever for the remaining ten minutes is keeping
the machine awake overnight, not an earlier trigger.

While measuring it, the wake records also settled a number nobody had checked.
Over the 26 mornings to 2026-10-02, WakeToRun landed +2.65 to +7.97 minutes after
the trigger on 23 of them, with three real outliers at +15.30, +24.95 and +28.82.
`check_run.ps1`'s band had called anything past +5 a human wake, which was 17 of
those 26; it is now +10, which sits in the empty gap between the two groups.

Grace of 45 minutes covers the start latency plus the run itself. Re-derived
from the 23 morning runs in `logs/` to 2026-10-01, start latency splits cleanly
by whether the machine was asleep at the trigger:

- **Asleep (the 07:30 era, 09-09 to 09-29).** Nineteen of those twenty-one runs
  started **+9.7 to +14.0 min** after the trigger. The two exceptions are the
  07:51 start on 2026-09-09, reconstructed from power events below (+21.3), and
  09-29, which started +3.0 because the machine happened to be awake.
- **Awake (the 08:00 era, 09-30 and 10-01).** Both runs started within three
  seconds of the trigger — 08:00:03 and 08:00:02, so **+0.1 and +0.0 min**.

WHAT CAUSES THE ASLEEP DELAY IS NOT ESTABLISHED HERE: the logs carry start times
only, no wake times, and `check_run.ps1`'s own reading of the power events puts
the wake itself at about +3 min — so most of the offset is something else, and
calling it "wake overhead" would be a guess dressed as a measurement.

**Which side of that boundary 07:45 lands on is not yet known.** It sits between
a time the machine was reliably asleep and a time it was reliably awake, and no
07:45 run has happened. Do not assume either; read the first week of starts out
of `logs/` and come back to this paragraph.

Runtime has grown. The last two morning runs took **25.0 minutes** each
(08:00:03→08:25:00 and 08:00:02→08:25:01), against 14.9 to 19.2 over the
preceding week — the census and the push content check are the difference. The
longest full morning run on record is 26.0 min (2026-09-14). Against an 08:30
alert that gives:

| start | run length | finish | slack to 08:30 |
|---|---|---|---|
| 07:45 (awake) | 25.0 min | 08:10 | 20 min |
| 07:59 (+14, asleep) | 25.0 min | 08:24 | 6 min |
| 07:59 (+14, asleep) | 26.0 min, the record | 08:25 | 5 min |

So the worst routine case clears by five to six minutes, the same margin the
08:00 trigger had against 08:45. The one start that would NOT clear is a repeat
of 2026-09-09's +21.3: 08:06 plus 25 minutes is 08:31, a minute late. That
exposure is not new and the move did not widen it — trigger and deadline both
shifted fifteen minutes, so the arithmetic is unchanged. It is an argument for
widening the grace if it ever recurs, not against 07:45.

The freshness banner on the dashboard reports staleness, but only when somebody
opens the page. To be told about a failure with nobody watching, the alert has
to come from **outside this machine** — nothing running on the desktop can
report that the desktop is asleep, powered off, or that Task Scheduler never
fired.

The script pings an external monitor: `/start` when it begins, the bare URL on
success, `/fail` on failure with the exit codes in the body. The `/fail` pings
are a courtesy; **the guarantee comes from absence** — the monitor alerts when
an expected ping does not arrive, which needs no cooperation from whatever
broke.

It is inert until configured. To turn it on:

1. Create a free check at <https://healthchecks.io> (or Cronitor — any service
   with a ping URL works).
2. Set its schedule to **cron `45 7 * * *`**, timezone **America/Chicago**,
   grace period **45 minutes**. That makes the alert fire at **08:30** if the
   morning run has not reported success. At the old 07:30 trigger those 45
   minutes landed exactly on the 08:15 deadline, so the check monitored the
   deadline itself rather than a proxy for it; at 08:00 the alert sat half an
   hour past that deadline, and at 07:45 it sits fifteen minutes past — closer,
   but still a proxy. The grace is sized to the run — start latency plus the
   longest run on record — so cutting it to 30 minutes to recover the
   coincidence would alert on any morning the machine was asleep at 07:45,
   which finishes about 08:24. `check_run.ps1` was moved to 08:30 to match, so
   the two again judge the same instant. The 13:00 run's extra ping is
   harmless.

   **The cron lives on Healthchecks.io, outside this repo** — but since
   `healthcheck_schedule.py`, it is read back and asserted (step 4). Moving the
   Task Scheduler trigger without editing the check
   produces a silent daily false alarm — which is exactly what happened on
   2026-09-30, when the trigger had moved to 08:00 and the check still expected
   07:30. On 2026-10-01 the trigger moved again, to 07:45, and `check_run.ps1`
   moved with it -- but the cron did NOT, and still reads `0 8 * * *`. That one
   did not fire, because the run now finishes EARLIER than the stale deadline
   rather than later; it leaves the monitor 15 minutes loose instead. If you
   change one, change both.
3. Put the ping URL in `.env`:

   ```
   HEALTHCHECK_URL=https://hc-ping.com/<your-uuid>
   ```

4. **Let the repo check step 2 for you.** Create a project API key at
   healthchecks.io > project Settings > API Access and add it to `.env`:

   ```
   HEALTHCHECK_API_KEY=<project api key, READ-WRITE>
   ```

   `tests/test_healthcheck_schedule.py` then reads the live cron, grace and
   timezone back and asserts they match the table above, so moving the trigger
   and forgetting the console fails the suite. `scripts/check_run.ps1` section 5
   prints the same verdict by hand.

   **It has to be a read-write key, and that is a real cost — read it before
   agreeing.** A read-only key omits `ping_url` and `uuid` from the listing and
   returns a `unique_key` whose derivation is undocumented, so no row can be tied
   to the URL the pipeline actually pings. The only identifiers left would be the
   check's name or slug, and matching on those proves merely that *some* check is
   configured correctly — audit a correct check that nothing pings and the guard
   is green and lying, which is the exact failure it exists to remove. Matching
   `ping_url` is a proof; matching a name is an assertion.

   The cost is that a read-write key can pause, reschedule and delete every check
   in its project, and this account also carries the droplet's `cron-alert`
   checks. **Healthchecks.io API keys are per-project**, so putting the two FCI
   checks in their own project bounds the blast radius to exactly what the guard
   audits. Worth doing before creating the key rather than after.

`.env` is gitignored — the ping URL and the API key are both capabilities, so
treat them as secrets. With no `HEALTHCHECK_URL` set the script logs
`monitoring inert` and carries on; a monitoring outage is caught and logged as a
warning and can never fail the pipeline. With no `HEALTHCHECK_API_KEY` the live
comparison is skipped, and `582 passed` becoming `581 passed, 1 skipped` is the
difference between "the monitor is watching the right time" and "nobody checked";
`pytest -rs` prints which.

**Nothing in this repo ever requests a ping URL.** A GET on `hc-ping.com/<uuid>`
registers a SUCCESS — it would tell the monitor the job ran when it did not,
which is worse than the drift the guard catches. `healthcheck_schedule.py`
refuses any URL outside the management API before the HTTP library is imported,
and the tests prove that behaviourally rather than by grepping for a string.

If you would rather not use a third party: Snowflake's `SYSTEM$SEND_EMAIL`
needs a notification integration, and creating one requires ACCOUNTADMIN, which
RBALDWIN does not have. That route is closed without an admin.

## Daily estimate email

The **morning** run mails the estimate; the afternoon pass is silent unless it
failed. Two near-identical emails a day trains you to ignore both, and the
afternoon number is a refinement rather than news. A **failure mails from
either slot** — that is the case worth interrupting someone for.

Delivery is a **OneDrive drop**, not SMTP and no longer Outlook. The original
design used Outlook COM so no password had to be stored; that stopped working on
2026-09-11, when it turned out Ross runs the NEW Outlook (olk.exe), from which
Microsoft removed the COM automation interface entirely. Classic `OUTLOOK.EXE` is
still on disk and the CLSID still registered, which is the trap — it appears to
work and does nothing.

SMTP is attempted but cannot succeed: the tenant has security defaults enforced,
so basic auth is permanently off for this account. So `send_email.ps1` writes the
message into `EMAIL_DROP_DIR` under OneDrive for Business, which syncs on its own,
and a scheduled Power Automate flow reads the file and sends it. The filenames are
stable because the flow fetches by path. The drop happens BEFORE the SMTP attempt
and regardless of it, because it is the path that actually delivers.

**The mail now leaves immediately after the index push, not at the end of the
run** (changed 2026-10-01). It reads only `fci_daily`, `cme_ftp_daily`,
`fci_snapshots` and `peer_estimates` — all CRITICAL, all published by that step —
so it was never waiting for anything it reports. The healthcheck ping deliberately
did NOT move with it: absence of that ping is the only signal a hang produces, so
it stays at the end where it certifies the whole run.

Set the recipients in `.env` (comma-separate for several):

```
EMAIL_TO=RBaldwin@jpsi.com
EMAIL_CC=
```

Unset `EMAIL_TO` and no mail is attempted at all.

To write the message out without delivering it:

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\send_email.ps1 -Preview
```

**INTERNAL ONLY.** The body carries CME's published index values, licensed to
JSA for internal display and internal non-display use. Forwarding it to clients
or reusing it in client communications needs a separate agreement with CME. The
email says so in its own footer, because whoever forwards it will not remember.

## Checking a run

```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check_run.ps1
powershell ... -File scripts\check_run.ps1 -Date 2026-09-10
```

Reports, in order: whether Task Scheduler fired it **on schedule** (event 107)
rather than at boot (118) or by hand (110); whether WakeToRun actually woke the
machine near 07:45; the run's own log, exit codes and whether it met **08:30**
(judging the FIRST run of the day, since that is the one the deadline is for);
and then the data — the frozen morning call, what the freshness banner would
say, and how the call scored once CME printed. Read-only.

The wake band it accepts is **07:15 to 07:50** — thirty minutes early, five
late — and the lopsidedness is the point, not sloppiness. An early wake is
WakeToRun doing its job; a LATE one means a human probably woke the machine,
which is the failure WakeToRun exists to prevent. **Derive both ends from the
trigger** when it moves again. Carrying the late end across as a literal is
what silently turned +5 into +35 at the 08:00 move.

## Still manual

- **Peer estimates.** CIH's and Compass's figures are hand-entered with
  `add_peer_estimate.py`; nothing scrapes them.
- **The dead-man's switch**, until `HEALTHCHECK_URL` is set — see above.

## Already done, recorded so it is not repeated

- **Task Scheduler history is enabled** (2026-09-09). Windows ships the
  `Microsoft-Windows-TaskScheduler/Operational` channel disabled, which is why
  diagnosing the 07:51 start on 2026-09-09 required reconstructing it from
  sleep/wake power events instead of simply reading task history. If it ever
  reverts — a machine rebuild, a policy push — re-enable it from an ELEVATED
  shell (it fails with access denied otherwise):

  ```
  wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
  ```

  Verify without elevation:

  ```
  Get-WinEvent -ListLog Microsoft-Windows-TaskScheduler/Operational |
      Select-Object IsEnabled, RecordCount
  ```

  Event 107 means triggered on schedule, 118 triggered by boot, 110 triggered
  by a user — that distinction is the one worth having.
