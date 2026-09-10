# The daily schedule

`scripts/daily_update.ps1` refreshes the FCI reconstruction and publishes it to
Snowflake, which is what the dashboard reads. It runs from Windows Task
Scheduler as **`JSA FCI daily update`**.

## Two triggers, one script

| Trigger | Purpose |
|---|---|
| **07:30** Central | The morning call — the number that goes out, and the one comparable to CIH's and Compass's morning sheets. |
| **13:00** Central | The settled pass. |

The script needs no argument to tell them apart: `snapshots.run_slot()` reads the
clock and files anything before 11:00 as `am`, the rest as `pm`. The 11:00
boundary leaves room for a morning run deferred by `StartWhenAvailable` without
it being misfiled as the afternoon pass.

### Why an afternoon run exists

Two measurements, both taken 2026-09-09:

- **CME posts its own file between 08:05 and 10:05 Central** (median 09:04,
  n=17, from the FTP server's `MDTM` timestamps). The 07:30 run therefore
  *structurally cannot* see the file published that morning, so "Last CME
  Print" was always a day stale.
- **USDA publication is only slightly unfinished at 07:30** — less than it
  first appears. Measured against the date a sale is BUCKETED into (not its
  USDA sale date, which is the framing that made El Reno look like a timing
  problem when it was a bucketing one), 96.17% of a bucket date's qualifying
  head is already published by 07:30 the next morning, rising to 98.41% by
  noon. So the afternoon pass recovers a real but small tail.

### Why 07:30 and not later

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

Revisit this only if the 08:15 deadline itself moves. At 09:00, an 08:30 run
would collect that 0.40% and also catch CME's own file in the morning rather
than at 13:00.

`fci_snapshots` is written INSERT-OR-IGNORE per `(index_date, run_date,
run_slot)`, so the afternoon pass **cannot** overwrite the morning call it
exists to be compared against.

## Task settings that matter

| Setting | Value | Why |
|---|---|---|
| `WakeToRun` | **True** | The machine sleeps overnight. Without this the job waits for someone to wake the PC: on four of the five weekdays before it was enabled, the 20-minute run would have finished *after* the 08:15 deadline. |
| `StartWhenAvailable` | **True** | Covers a full power-off, which no scheduled task can wake from — the run then happens at next boot. |

Wake timers are enabled on AC and this is a desktop with no battery, so the
`WakeToRun` setting is not silently vetoed by power policy. (On a laptop it
would be: wake timers are disabled on battery by default.)

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything succeeded. |
| 2 | venv python missing. |
| 3 | `.env` missing (no `MARS_API_KEY`). |
| 5 | The refresh worked but the **Snowflake push failed** — local data is current, the dashboard is stale. |
| other | Propagated from `update_index.py`. |

A non-zero `cme_exit` is logged as a warning and does **not** fail the run: a
stale CME series is a smaller problem than skipping the publish, and CME's FTP
is a third party we do not control.

Logs land in `logs/update_<date>.log` and are pruned after 30 days.

## Dead-man's switch

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
2. Set its schedule to **cron `30 7 * * *`**, timezone **America/Chicago**,
   grace period **45 minutes**. That makes the alert fire at 08:15 if the
   morning run has not reported success — i.e. it monitors the actual deadline
   rather than a proxy for it. The 13:00 run's extra ping is harmless.
3. Put the ping URL in `.env`:

   ```
   HEALTHCHECK_URL=https://hc-ping.com/<your-uuid>
   ```

`.env` is gitignored — the URL is a capability, so treat it as a secret. With
no `HEALTHCHECK_URL` set the script logs `monitoring inert` and carries on; a
monitoring outage is caught and logged as a warning and can never fail the
pipeline.

If you would rather not use a third party: Snowflake's `SYSTEM$SEND_EMAIL`
needs a notification integration, and creating one requires ACCOUNTADMIN, which
RBALDWIN does not have. That route is closed without an admin.

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
