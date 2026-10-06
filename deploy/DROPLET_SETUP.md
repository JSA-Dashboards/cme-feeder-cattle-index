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
```

`/opt/alerting/cron-alert` emails on failure and pings healthchecks.io, where the
check's slug comes from the job name. So keep the names, and if you change a
time, change that check's schedule too.

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
