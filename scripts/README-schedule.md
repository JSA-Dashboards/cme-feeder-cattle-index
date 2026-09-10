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
- **USDA publication is unfinished at 07:30.** Across 80 auctions and 246
  reports, head-weighted, 83.3% of a sale day's qualifying head is published
  the same day (mostly noon–19:00), 85.2% by 07:30 the next morning, and 95.8%
  by noon.

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
- **Task Scheduler history.** The `Microsoft-Windows-TaskScheduler/Operational`
  log is disabled by default, so there is no record of whether the task fired.
  Enabling it needs an elevated shell:

  ```
  wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
  ```
