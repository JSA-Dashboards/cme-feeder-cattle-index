#!/usr/bin/env bash
# run_ftp_check.sh — afternoon catch-up check for CME's own official Feeder Cattle
# Index settlement files, invoked by cron on the Droplet. CME typically publishes a
# business day's official file with a 1-3 business day lag, usually by mid-afternoon —
# this exists so newly-published official data gets picked up the same day rather than
# waiting for the next morning's run_update.sh. Idempotent, safe to re-run (MERGE-based
# upsert into Snowflake JSA.CME_FEEDER_CATTLE). Does NOT run update_index.py — that's
# run_update.sh's job. flock prevents overlapping runs.
#
# Cron installs this; adjust APP_DIR to wherever the repo is deployed.
set -uo pipefail

APP_DIR="/opt/cme-feeder-cattle-index"       # <-- deploy path (edit me)
VENV="$APP_DIR/.venv"                         # virtualenv created during setup
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/ftp_check_$(date +%Y%m%d_%H%M%S).log"

cd "$APP_DIR" || { echo "APP_DIR $APP_DIR missing" >&2; exit 1; }

# Single-instance guard: skip silently if a run is already going (e.g. the
# morning update overlapping a slow afternoon check).
exec 9>"$LOG_DIR/.update.lock"
if ! flock -n 9; then
    echo "$(date -Is) another run is in progress — skipping" >>"$LOG"
    exit 0
fi

rc=0
{
    echo "=== ftp_check start $(date -Is) ==="
    "$VENV/bin/python" backfill_ftp.py --start "$(date -d '10 days ago' +%Y-%m-%d)" --end "$(date +%Y-%m-%d)"
    rc=$?
    echo "=== ftp_check finished $(date -Is) rc=$rc ==="
} >>"$LOG" 2>&1

# Keep 30 days of logs.
find "$LOG_DIR" -name 'ftp_check_*.log' -mtime +30 -delete 2>/dev/null || true

exit "$rc"
