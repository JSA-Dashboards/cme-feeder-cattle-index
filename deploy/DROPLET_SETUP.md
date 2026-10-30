# CME Feeder Cattle Index — daily update on the DigitalOcean Droplet

Runs the two daily jobs that used to be Claude Code scheduled tasks
(`cme-feeder-cattle-index-update` / `cme-feeder-cattle-index-ftp-check`) as real
cron jobs instead — same droplet already running basis-tracker + river-fob-portal
(`137.184.195.51`, `/opt/`). **The Streamlit apps still run on Streamlit Cloud**
(this app, `livestock-portal`, `jsa-admin-portal`) — this droplet only runs the
update scripts and writes to the shared Snowflake DB (`JSA.CME_FEEDER_CATTLE`)
those apps read.

## 1. Deploy the code: push to GitHub, then pull on the box

Since 2026-10-04 `/opt/cme-feeder-cattle-index` is a **git clone** of
`JSA-Dashboards/cme-feeder-cattle-index`, branch `master`. The repo is public,
so the box pulls over HTTPS with no deploy key or PAT. To deploy, push first,
then:

```bash
ssh root@137.184.195.51 "git -C /opt/cme-feeder-cattle-index pull --ff-only"
```

**Do not `git archive HEAD | ssh ... tar -x` into it any more** (that was the
method before 2026-10-04). tar writes files behind git's back:

- the checkout goes dirty;
- new files land as untracked;
- files deleted from the repo stay on the box;
- the next `git pull` refuses with "local changes would be overwritten".

If a pull ever refuses, run `git -C /opt/cme-feeder-cattle-index status` and
read what changed before discarding anything.

A pull also replaces the box's `data/mars_history.db` with the committed one.
That is harmless: the droplet runs `USE_SNOWFLAKE=1` and never opens the file.
Checked 2026-10-05: no cron run had modified it.

Modes come from the repo, so every script cron runs must be **100755 in git**.
Git on Windows does not track the exec bit, so set it with
`git update-index --chmod=+x deploy/<script>.sh`. A 100644 script fails under
cron with exit 126, before it writes a log.

First-time setup on a fresh box:

```bash
ssh root@137.184.195.51 "git clone https://github.com/JSA-Dashboards/cme-feeder-cattle-index.git /opt/cme-feeder-cattle-index && mkdir -p /opt/cme-feeder-cattle-index/logs"
```

## 2. Virtualenv + dependencies

```bash
ssh root@137.184.195.51 "cd /opt/cme-feeder-cattle-index && python3 -m venv .venv \
  && ./.venv/bin/pip install --upgrade pip \
  && ./.venv/bin/pip install -r requirements.txt"
```

## 3. Secrets — `.env`

Both scripts read `/opt/cme-feeder-cattle-index/.env` via `load_dotenv()`.
Copy your local `.env` up — it already has everything:

```bash
scp .env root@137.184.195.51:/opt/cme-feeder-cattle-index/.env
ssh root@137.184.195.51 "chmod 600 /opt/cme-feeder-cattle-index/.env"
```

Required keys: `USE_SNOWFLAKE=1`, `SNOWFLAKE_ACCOUNT` / `_USER` / `_PASSWORD` /
`_ROLE` / `_WAREHOUSE` / `_DATABASE` / `_SCHEMA` (schema = `CME_FEEDER_CATTLE`),
and `MARS_API_KEY`. Unlike the merged `jsa-home-page` portal, this is a
single-purpose deployment — no `SNOWFLAKE_SCHEMA` collision risk, it's fine to
set it explicitly here.

## 4. Test both jobs by hand before trusting cron

```bash
ssh root@137.184.195.51 "chmod +x /opt/cme-feeder-cattle-index/deploy/*.sh"
ssh root@137.184.195.51 "/opt/cme-feeder-cattle-index/deploy/run_ftp_check.sh; echo EXIT=\$?"
ssh root@137.184.195.51 "/opt/cme-feeder-cattle-index/deploy/run_update.sh; echo EXIT=\$?"
```

`run_update.sh` (Step 1, `update_index.py`) takes ~10 minutes — it fetches 84
sale-barn locations plus the weekly Direct/Video PDF reports. Confirm both logs
end `rc=0` in `/opt/cme-feeder-cattle-index/logs/`.

## 5. The cron jobs

Times are America/Chicago, the droplet's own timezone (`timedatectl`). These
are the live crontab lines; edit them on the box with `crontab -e`:

```
45 8 * * 1-5 /opt/alerting/cron-alert "CME feeder index update" "/opt/cme-feeder-cattle-index/logs/update_*.log" /opt/cme-feeder-cattle-index/deploy/run_update.sh
0 15 * * 1-5 /opt/alerting/cron-alert "CME FTP check" "/opt/cme-feeder-cattle-index/logs/ftp_check_*.log" /opt/cme-feeder-cattle-index/deploy/run_ftp_check.sh
0 21 * * 1-5 /opt/alerting/cron-alert "Border reports evening refresh" "/opt/cme-feeder-cattle-index/logs/imports_*.log" /opt/cme-feeder-cattle-index/deploy/run_imports.sh
```

`/opt/alerting/cron-alert` emails on failure and pings healthchecks.io, where the
check's slug comes from the job name. So keep the names, and if you change a
time, change that check's schedule too.

### The weekend run, and why the weekday line stays commented out

The morning update line is COMMENTED OUT on the live box, with its reason in
the crontab: the desktop's `JSA FCI daily update` has `StartWhenAvailable`, so
when the laptop is asleep at 07:45 the catch-up can land anywhere -- on
2026-10-06 it fired 08:45:45, 43 seconds after this cron. Both DELETE and
reload `JSA.CME_FEEDER_CATTLE` and nothing locks across the two machines.

**There is no such race at a weekend**, because the weekends it matters on are
exactly the weekends the desktop is powered off. Add this line and leave the
weekday one commented:

```
45 8 * * 6,0 /opt/alerting/cron-alert "CME feeder index update" "/opt/cme-feeder-cattle-index/logs/update_*.log" /opt/cme-feeder-cattle-index/deploy/run_update.sh
```

WHY IT IS WORTH A CRON, measured off `fci_snapshots` rather than supposed: on
the five weekends a run happened, Saturday's published headline sat 0.0000,
0.0190, 0.0056, 0.0000 and 0.0151 from where the index settled. On the two it
did not -- 2026-10-03/04 and 10-17/18, the machine shut down on the Friday
evening both times -- it sat 0.7663 and 0.7599 off, for about 67 hours each.
The Saturday pass collects the USDA reports that land overnight.

**08:45 and not earlier**, even though there is nothing to wait for at a
weekend: if the desktop IS on, this still starts after its ~08:00-08:28 push
window, which is the ordering the weekday line was written for. Keeping one
time means one thing to reason about.

**The same job name on purpose.** `cron-alert` derives its healthchecks.io slug
from the name and pings with `?create=1`, so a NEW name auto-creates a check
with the default daily period and goes red every weekday. Reusing the name
means failure emails work, but the dead-man's switch will NOT notice a missed
weekend run until that check's own cron is widened to `45 8 * * *` in the
healthchecks.io UI. That cannot be done from the repo: `HEALTHCHECK_API_KEY` is
not in `.env` on either machine.

### What a FULL move off the desktop still needs

The code is already here and current; the blocker is email. `run_update.sh`
sends none -- the morning estimate goes out from the desktop via
`scripts/send_email.ps1`, a OneDrive drop that Power Automate picks up, which
is desktop-bound by construction. So disabling the desktop job to end the race
would silently stop the daily estimate mail. Either that delivery moves to
something the droplet can drive, or the desktop keeps running for the mail
alone. Not decided here.

Two deploy scripts are also **100644 in git** -- `run_imports.sh` and
`install_imports_cron.sh` -- which fails under cron with exit 126 before it
writes a log. Fix with `git update-index --chmod=+x deploy/<script>.sh` before
cronning either.

- **8:45 AM Central, Mon–Fri** — `run_update.sh`: official CME files (Step 0) +
  full MARS/Direct/Video reconstruction (Step 1). Moved from 8:15 on
  2026-10-05. Ross's desktop job (07:45) pushes to the same
  `JSA.CME_FEEDER_CATTLE` tables at about 08:00–08:28, deleting and then
  reloading them. A droplet run inside that window can read a half-loaded
  table or race the reload. 8:45 starts after that window, but only by timing:
  nothing locks across the two machines.
- **3:00 PM Central, Mon–Fri** — `run_ftp_check.sh`: official CME files only,
  catches anything CME published since the morning (mid-afternoon is their
  typical publish time)
- **9:00 PM Central, Mon–Fri** — `run_imports.sh`: `update_imports.py --only
  ams`, the AMS border reports behind the portal's Mexican Feeder Crossings
  page. Added 2026-10-30. AMS publishes 3486 the same day it covers — over all
  47 reports of 2026, earliest 12:34, median 15:21, latest 20:14, never
  next-day — and the desktop's 07:45 and 13:00 runs caught 0 and 1 of those 47,
  so the crossings were a full day behind by construction.

  **The hour has to clear the desktop's push windows** (~08:00–08:28 and
  ~13:00–13:25), for the reason given above the 8:45 entry: that push deletes
  and reloads these same tables and nothing locks across the two machines.
  21:00 is clear of both. It is also later than the publication tail strictly
  needs, because **the timezone of the MARS `published_date` stamp is not
  established** — 21:00 catches 47/47 whether it is Central or Eastern, where
  17:30 would be 36/47 or 43/47 depending on the answer.

  Install with `deploy/install_imports_cron.sh` (`--check` reports only). It
  refuses unless `.env` carries `USE_SNOWFLAKE=1`, since without it the ingest
  writes a local SQLite nobody reads and exits 0 looking healthy.

Both share one `flock` lock file (`logs/.update.lock`) so they can never
overlap if one runs long.

## 6. No propagation step, unlike the old pre-Snowflake pipeline

Both scripts write straight to Snowflake (`MERGE`-based upserts via
`snowflake_db.py`) — every deployed app (this repo's own Cloud deployment,
`livestock-portal`, `jsa-admin-portal`) reads that same live data with no
copy/commit/push step. The committed `data/mars_history.db` in all three repos
is a frozen rollback copy only.

## 7. Monitoring

```bash
ssh root@137.184.195.51 "ls -lt /opt/cme-feeder-cattle-index/logs | head"
ssh root@137.184.195.51 "tail -40 /opt/cme-feeder-cattle-index/logs/update_*.log | tail -40"
```

Logs are pruned at 30 days automatically (each script's own `find -mtime +30`).

## 8. Retire the Claude Code scheduled tasks

Once this has run cleanly on cron for a day or two, disable/delete the
`cme-feeder-cattle-index-update` and `cme-feeder-cattle-index-ftp-check`
scheduled tasks (`~/.claude/scheduled-tasks/`) — the droplet fully replaces
them and no longer depends on a Claude Code session running on a schedule.
Running both in parallel during the transition is harmless (every write is an
idempotent MERGE), just redundant.
