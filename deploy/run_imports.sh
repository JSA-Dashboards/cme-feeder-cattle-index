#!/usr/bin/env bash
# run_imports.sh — evening USDA import refresh, invoked by cron on the Droplet.
#
# Runs update_imports.py, which ingests the AMS border reports behind the
# portal's Mexican Feeder Crossings page. On this host USE_SNOWFLAKE=1, so
# db.get_conn() returns Snowflake and the ingest writes JSA.CME_FEEDER_CATTLE
# DIRECTLY — there is no SQLite step and no push step here, unlike the desktop.
#
# WHY THIS EXISTS. AMS publishes report 3486 the same day it covers — measured
# over all 47 reports of 2026, earliest 12:34, median 15:21, latest 20:14, and
# never next-day. Ross's desktop job ran at 07:45 and 13:00, which caught 0 and
# 1 of those 47 respectively, so the crossings were structurally a full day
# behind: each morning's run collected YESTERDAY's report, exited 0, and read
# as AMS being slow. The same shape as the 10:15 CME pull that never once
# caught a print (scripts/README-schedule.md).
#
# IT IS NOT EXCLUSIVE, AND THAT IS DELIBERATE RATHER THAN A HALF-MOVE. The
# desktop's push DELETEs every row of these tables and reloads them from its
# own SQLite (push_verify.py says so in as many words). So a droplet that were
# the only writer would have its rows destroyed by the next morning's desktop
# reload, from a SQLite that no longer tracked them. The desktop therefore
# keeps ingesting and keeps pushing; this job only gets there first. What it
# buys is the evening, not ownership.
#
# WHICH IS ALSO WHY THE HOUR MATTERS. DROPLET_SETUP.md records that a droplet
# run inside the desktop's push window can read a half-loaded table or race the
# reload, and nothing locks across the two machines. The desktop pushes about
# 08:00-08:28 and again around 13:00-13:25. 21:00 is clear of both, and the
# desktop's own 21:00 trigger was removed when this job took that slot.
set -uo pipefail

APP_DIR="/opt/cme-feeder-cattle-index"
VENV="$APP_DIR/.venv"
LOG_DIR="$APP_DIR/logs"

# cd BEFORE creating anything, so a wrong APP_DIR fails here rather than
# scattering a logs/ directory somewhere else on the box.
cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/imports_$(date +%Y%m%d_%H%M%S).log"

# Credentials live in a .env FILE beside the code, not in root's environment —
# the convention every job on this host uses. cron gives a job almost nothing,
# so without this update_imports.py's load_dotenv() finds no MARS_API_KEY and
# no USE_SNOWFLAKE: it would run, write to a local SQLite nobody reads, and
# exit 0 looking healthy.
if [ -f "$APP_DIR/.env" ]; then
    set -a; . "$APP_DIR/.env"; set +a
else
    echo "$APP_DIR/.env missing — refusing to run with no credentials" >&2
    exit 1
fi

# Single-instance guard. A MISSING flock BINARY IS HANDLED EXPLICITLY: command
# not found is 127, `!` makes that true, and the job would exit 0 having done
# nothing while looking like it had merely skipped a lock.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOG_DIR/.imports.lock"
    if ! flock -n 9; then
        echo "$(date -Is) another import run is in progress — skipping" >>"$LOG"
        exit 0
    fi
else
    echo "$(date -Is) flock not installed — running without a lock" >>"$LOG"
fi

rc=0
{
    echo "=== imports start $(date -Is) ==="
    # --only ams is the border reports alone. update_imports.py also carries
    # calf, corn, feed and census; widening this job is one word here, and
    # every one of them is the same kind of same-day USDA feed with the same
    # staleness. Left narrow because the border page is what was asked for.
    "$VENV/bin/python" update_imports.py --only ams
    rc=$?
    echo "=== imports finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs, matching run_update.sh.
find "$LOG_DIR" -name 'imports_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
