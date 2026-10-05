#!/usr/bin/env bash
# run_update.sh — morning CME Feeder Cattle Index update, invoked by cron on the Droplet.
#
# Two steps, both required (see cme-feeder-cattle-index-update SKILL.md for the full
# rationale): Step 0 pulls any newly-published official CME settlement files
# (backfill_ftp.py, last ~10 days, idempotent MERGE); Step 1 runs the MARS/Direct/Video
# reconstruction (update_index.py) for the trailing days CME hasn't published yet. Both
# write straight to Snowflake (JSA.CME_FEEDER_CATTLE) — every deployed app reads it live,
# no propagation step needed. flock prevents overlapping runs.
#
# Cron installs this; adjust APP_DIR to wherever the repo is deployed.
set -uo pipefail

APP_DIR="/opt/cme-feeder-cattle-index"       # <-- deploy path (edit me)
VENV="$APP_DIR/.venv"                         # virtualenv created during setup
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/update_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard: skip silently if a run is already going (e.g. the
# afternoon ftp-check overlapping a slow morning run).
exec 9>"$LOG_DIR/.update.lock"
if ! flock -n 9; then
    echo "$(date -Is) another run is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== update start $(date -Is) ==="
    echo "--- Step 0: backfill_ftp.py (official CME files, last 10 days) ---"
    "$VENV/bin/python" backfill_ftp.py --start "$(date -d '10 days ago' +%Y-%m-%d)" --end "$(date +%Y-%m-%d)"
    rc0=$?
    echo "--- Step 1: update_index.py (MARS/Direct/Video reconstruction) ---"
    "$VENV/bin/python" update_index.py
    rc1=$?
    rc=$(( rc0 != 0 ? rc0 : rc1 ))
    echo "=== update finished $(date -Is) rc=$rc (step0=$rc0 step1=$rc1) ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'update_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
