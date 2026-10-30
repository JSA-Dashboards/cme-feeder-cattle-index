#!/usr/bin/env bash
# install_imports_cron.sh — put the evening border-reports refresh on the Droplet.
#
#   sudo bash deploy/install_imports_cron.sh --check    report only, changes nothing
#   sudo bash deploy/install_imports_cron.sh            install
#
# ASSUMES THE REPO IS ALREADY DEPLOYED. /opt/cme-feeder-cattle-index exists with
# its own .venv and .env, because run_update.sh has been running from it for
# weeks. This adds one crontab line; it does not clone, build a venv or touch
# credentials.
#
# IT APPENDS TO THE CRONTAB AND NEVER REPLACES IT. There are ~25 jobs on this
# box belonging to other repos, and `crontab -` with a freshly composed file
# would delete every one of them.
#
# IT READS THE CRONTAB BACK AFTERWARDS. A sibling installer on this host once
# printed "appending", piped into `crontab -`, and went straight to "done",
# with only a cosmetic grep whose empty output looked exactly like success —
# and reported "cron installed" on a host where nothing had been. An installer
# that claims success it has not earned is the same silent failure as the job
# it installs, one level up.
set -uo pipefail

APP_DIR="/opt/cme-feeder-cattle-index"
ALERT="/opt/alerting/cron-alert"
JOB_NAME="Border reports evening refresh"
WRAPPER="$APP_DIR/deploy/run_imports.sh"
LOG_GLOB="$APP_DIR/logs/imports_*.log"

# 21:00 America/Chicago, Mon-Fri. AMS 3486's own published_date over all 47
# reports of 2026 runs 12:34 to 20:14, median 15:21, always on the report's own
# day — so 21:00 catches 47 of 47.
#
# THE HOUR IS LATER THAN IT LOOKS LIKE IT NEEDS TO BE, ON PURPOSE. The timezone
# of the MARS stamp is NOT established, and scripts/README-schedule.md records
# five hours lost to assuming one on CME's MDTM. 21:00 is 100% whether that
# stamp is Central or Eastern; 17:30 would be 36/47 or 43/47 depending on the
# answer, which is exactly the figure that needs it settled first.
#
# It must also stay clear of the desktop's push windows (~08:00-08:28 and
# ~13:00-13:25), which DELETE and reload these tables — see run_imports.sh.
# Mon-Fri because AMS does not publish 3486 at a weekend.
CRON_LINE="0 21 * * 1-5 $ALERT \"$JOB_NAME\" \"$LOG_GLOB\" $WRAPPER"

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

echo "host timezone: $(timedatectl show -p Timezone --value 2>/dev/null || date +%Z)"
echo

fail=0
for p in "$APP_DIR" "$APP_DIR/.venv/bin/python" "$WRAPPER"; do
    if [ -e "$p" ]; then echo "  OK      $p"; else echo "  MISSING $p"; fail=1; fi
done

# The alerting wrapper is not optional on this host: cron here has no MAILTO
# and the machine has no MTA, so a BARE crontab entry fails silently forever.
if [ -x "$ALERT" ]; then echo "  OK      $ALERT"; else echo "  MISSING $ALERT"; fail=1; fi

# Report credentials BY PRESENCE and never print one.
if [ -f "$APP_DIR/.env" ]; then
    for k in USE_SNOWFLAKE MARS_API_KEY SNOWFLAKE_ACCOUNT SNOWFLAKE_USER; do
        if grep -q "^$k=" "$APP_DIR/.env"; then echo "  OK      .env has $k"
        else echo "  MISSING .env has no $k"; fail=1; fi
    done
    if grep -q '^USE_SNOWFLAKE=1' "$APP_DIR/.env"; then
        echo "  OK      USE_SNOWFLAKE=1 — the ingest writes Snowflake directly"
    else
        echo "  MISSING USE_SNOWFLAKE is not 1 — the ingest would write a local"
        echo "          SQLite nobody reads, and exit 0 looking healthy"
        fail=1
    fi
else
    echo "  MISSING $APP_DIR/.env"; fail=1
fi

echo
if [ "$fail" -ne 0 ]; then
    # A job installed without credentials fails silently once a day forever,
    # which is the one outcome worse than not installing it.
    echo "REFUSING to install — fix the above first."
    exit 1
fi

if crontab -l 2>/dev/null | grep -Fq "$WRAPPER"; then
    echo "already installed:"
    crontab -l 2>/dev/null | grep -F "$WRAPPER"
    exit 0
fi

echo "would install:"
echo "  $CRON_LINE"
if [ "$CHECK" -eq 1 ]; then
    echo
    echo "--check: nothing was changed."
    exit 0
fi

chmod +x "$WRAPPER"
{ crontab -l 2>/dev/null; echo "$CRON_LINE"; } | crontab -

# Read it back from cron, not from our own echo above.
n=$(crontab -l 2>/dev/null | grep -Fc "$WRAPPER")
if [ "$n" -ne 1 ]; then
    echo "FAILED — expected 1 matching crontab line, found $n"
    exit 1
fi
echo
echo "installed, and verified by reading the crontab back:"
crontab -l 2>/dev/null | grep -F "$WRAPPER"
echo
echo "cron-alert keys its healthchecks.io slug off the job name, so if you ever"
echo "change the hour, change that check's schedule too."
