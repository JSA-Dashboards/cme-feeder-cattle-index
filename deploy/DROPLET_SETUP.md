# CME Feeder Cattle Index — daily update on the DigitalOcean Droplet

Runs the two daily jobs that used to be Claude Code scheduled tasks
(`cme-feeder-cattle-index-update` / `cme-feeder-cattle-index-ftp-check`) as real
cron jobs instead — same droplet already running basis-tracker + river-fob-portal
(`137.184.195.51`, `/opt/`). **The Streamlit apps still run on Streamlit Cloud**
(this app, `livestock-portal`, `jsa-admin-portal`) — this droplet only runs the
update scripts and writes to the shared Snowflake DB (`JSA.CME_FEEDER_CATTLE`)
those apps read.

## 1. Deploy the code (no `.git` on the box — archive, don't clone)

This repo is private, so `git clone` on the box would need a deploy key/PAT.
Instead, archive from your local checkout and pipe it over SSH (same method as
basis-tracker):

```bash
# from your local machine, inside the repo:
ssh root@137.184.195.51 "mkdir -p /opt/cme-feeder-cattle-index/deploy /opt/cme-feeder-cattle-index/logs"
git archive HEAD | ssh root@137.184.195.51 "tar -x -C /opt/cme-feeder-cattle-index"
```

To update later (code changes only — this data pipeline never needs a
propagation step, see below): re-run the same `git archive` command, it
overwrites in place.

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

## 5. Install the cron jobs

Matches the two old scheduled tasks' times (America/Chicago — confirm with
`timedatectl`, this droplet is already set to it):

```bash
ssh root@137.184.195.51 "( crontab -l 2>/dev/null | grep -v -e cme-feeder-cattle-index
  echo '15 8 * * 1-5 /opt/cme-feeder-cattle-index/deploy/run_update.sh'
  echo '0 15 * * 1-5 /opt/cme-feeder-cattle-index/deploy/run_ftp_check.sh' ) | crontab -"
ssh root@137.184.195.51 "crontab -l"
```

- **8:15 AM Central, Mon–Fri** — `run_update.sh`: official CME files (Step 0) +
  full MARS/Direct/Video reconstruction (Step 1)
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
