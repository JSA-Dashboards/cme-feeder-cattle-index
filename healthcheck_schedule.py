"""
Read the dead-man's switch's own schedule back from Healthchecks.io and prove it
still matches the schedule this repo declares.

WHY THIS EXISTS. scripts/README-schedule.md asserted three separate times that
"the cron lives on Healthchecks.io, outside this repo, and nothing here can read
it back", and recorded two incidents caused by exactly that:

  2026-09-30  the trigger moved to 08:00 and the check still said `30 7 * * *`,
              so the monitor went red every morning on a healthy pipeline.
  2026-10-01  the trigger moved to 07:45 and the check still says `0 8 * * *`.
              This one does NOT go red, and that is worse: moving a job EARLIER
              can never trip a stale cron, so the only symptom is that the
              monitor sits 15 minutes looser than the deadline check_run.ps1
              judges against. Silent since 2026-10-01.

The second is the case the monitor is structurally incapable of reporting about
itself, and it is the reason this module is a comparison rather than a ping.

THE THREE COPIES, AND WHICH TWO THIS BINDS.

  (1) Windows Task Scheduler, "JSA FCI daily update"  -- the ACTUAL trigger and
      the real truth. NOT read here: it is Windows-only and task-must-exist, and
      this guard's whole subject is the copy that is not local.
  (2) scripts/check_run.ps1 line 23, `$trigH = 7; $trigM = 45`, plus the grace
      literal in `(TriggerOn $t0).AddMinutes(45)` -- morning only.
  (3) scripts/README-schedule.md's configuration table -- BOTH slots.
  (4) Healthchecks.io's live schedule / grace / tz.

The chain is (2) == (3)'s morning row, checked locally on every test run with no
key at all, and (3) == (4), checked live when a key is present.

THE README TABLE IS NOT A FOURTH COPY. It is not documentation *about* the
configuration, it IS the instruction a human follows to set it -- setup step 2
points at the same numbers. That is what makes 13:00 machine-readable without
inventing a literal somewhere nothing reads. Do NOT add $pmTrigH/$pmTrigM to
check_run.ps1 to "fix" the asymmetry below: check_run.ps1 judges only the FIRST
run of the day (see its own comment above the deadline block), so those literals
would be read by no PowerShell code at all, and a guard pinned to a rotted
literal enforces the rot.

The asymmetry, stated plainly: the morning row has a second repo-side witness in
check_run.ps1, whose staleness is self-correcting because Ross reads
check_run.ps1's output and a stale $trigH makes it visibly wrong the next
morning. The afternoon row has no second witness.

NEVER REQUEST A PING URL. A GET on https://hc-ping.com/<uuid> REGISTERS A
SUCCESS -- it would tell the monitor the job ran when it did not, which is a
worse failure than the one this module exists to catch. Exactly one URL is ever
requested, the management API, and _api_get refuses anything that does not start
with _ALLOWED_PREFIX before `requests` is so much as imported. CLAUDE.md records
a guard that "banned a string in comments rather than in queries", so the tests
for this are behavioural: they stub the transport and assert what was called.

WHAT THIS CANNOT CATCH, so nobody reads more into a green suite than is there:

  1. Task Scheduler's real trigger diverging from BOTH (2) and (3). Move the
     Windows task and edit neither file and this is green while the monitor is
     wrong. It converts a three-way silent divergence into a two-way loud one;
     the third leg is covered only by a human reading check_run.ps1's output.
  2. The 13:00 trigger moved with the README's afternoon row left stale -- that
     row has no second repo-side witness, per the asymmetry above.
  3. Whether anyone is actually NOTIFIED. A check with a perfect cron, grace and
     timezone wired to no integration alerts nobody. `channels` is reported in
     notes and NOT asserted, because its semantics (notably whether "*" means
     all integrations) are not established. status != "paused" covers the
     loudest form of the same failure.
  4. Drift between suite runs. This is a pre-commit-grade guard, not
     surveillance.
  5. Machine-timezone drift. The repo's 07:45 is Task Scheduler LOCAL time and
     the cron is America/Chicago; they agree only because this desktop is set to
     Central, and nothing here reads that.
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO = Path(__file__).resolve().parent
CHECK_RUN = REPO / "scripts" / "check_run.ps1"
README = REPO / "scripts" / "README-schedule.md"
ENV_FILE = REPO / ".env"

# The ONE url this module ever requests. Nothing is concatenated onto it: the
# listing is fetched UNFILTERED (no ?slug=, no ?tag=) so that a zero-match
# failure can name the checks the key can actually see, which is the difference
# between "your url matches nothing" and a message somebody can act on.
API_URL = "https://healthchecks.io/api/v1/checks/"

# startswith, never `in`. A substring test would accept
# https://hc-ping.com/x?u=https://healthchecks.io/api/v1/ -- tested.
_ALLOWED_PREFIX = "https://healthchecks.io/api/v1/"

TZ = "America/Chicago"

# Healthchecks.io returns grace in SECONDS; the README declares it in minutes.
_SECONDS_PER_MIN = 60


class PingUrlRefused(RuntimeError):
    """A request was attempted at something that is not the management API."""


class MonitorUnreachable(RuntimeError):
    """Transport failure or 5xx -- a third party we do not control. Skip."""


class MonitorRejected(RuntimeError):
    """The key was present and refused, or an unexpected status. Hard failure."""


# ---------------------------------------------------------------------------
# Parsing the repo's own declarations.
# ---------------------------------------------------------------------------

def _one(text, pattern, what):
    """
    The single match of `pattern`, or an assertion failure naming `what`.

    Uniqueness is the point, borrowed from tests/test_daily_update_order.py's
    _pos(). If a literal is renamed, duplicated or deleted, a guard that quietly
    matched the first hit would go on reporting success about a file it no
    longer understands. Better to break loudly and be re-pointed.
    """
    hits = re.findall(pattern, text, re.M)
    assert len(hits) == 1, (
        "expected exactly one %s (/%s/), found %d. Re-point this guard at the "
        "current wording rather than loosening it." % (what, pattern, len(hits)))
    return hits[0]


def trigger_from_check_run(src):
    """
    {'hour', 'minute', 'grace_min'} out of scripts/check_run.ps1.

    Two literals, both load-bearing INSIDE that script -- the WakeToRun band and
    the finish deadline derive from them -- which is why they are the anchor and
    not a fresh constant: a stale $trigH makes check_run.ps1's own output
    visibly wrong the next morning, so daily use corrects it.

    The grace pattern is deliberately tied to `(TriggerOn $t0).AddMinutes(N)`.
    The same file carries $trigger.AddMinutes(-30) and .AddMinutes(10) for the
    wake band, and those must NOT match.
    """
    h, m = _one(src, r"^\$trigH\s*=\s*(\d{1,2})\s*;\s*\$trigM\s*=\s*(\d{1,2})\b",
                "$trigH/$trigM pair")
    grace = _one(src, r"TriggerOn \$t0\)\.AddMinutes\((\d+)\)",
                 "(TriggerOn $t0).AddMinutes(N) deadline literal")
    return {"hour": int(h), "minute": int(m), "grace_min": int(grace)}


_ROW = (r"^\|\s*%s\s*\|\s*`([^`]+)`\s*\|\s*(\d+)\s*min\s*\|\s*(\d{1,2}:\d{2})\s*\|")


def declared_from_readme(md):
    """
    {'morning': {...}, 'afternoon': {...}} out of README-schedule.md's table.

    Anchored on the TABLE ROWS and nothing else. The file legitimately contains
    `30 7 * * *`, `0 8 * * *`, `30 7,13 * * *`, `0 8,13 * * *` and
    `45 7,13 * * *` elsewhere in its incident history and its explanation of the
    two-check split, and all of those must stay readable without confusing this.
    """
    out = {}
    for slot in ("morning", "afternoon"):
        cron, grace_min, alert = _one(md, _ROW % slot, "`| %s |` table row" % slot)
        out[slot] = {
            "cron": cron.strip(),
            "grace_min": int(grace_min),
            "grace_s": int(grace_min) * _SECONDS_PER_MIN,
            "alert_hhmm": _hhmm(*[int(x) for x in alert.split(":")]),
            "tz": TZ,
        }
    return out


def _hhmm(hour, minute):
    return "%02d:%02d" % (hour, minute)


def cron_time(cron):
    """
    (hour, minute) for a plain `M H * * *`, or None if it is not that shape.

    None is not an error here -- check_schedule turns it into a violation with a
    message naming what was wrong. Parsing and judging are kept apart so the
    judging can be tested without a cron parser in the way.
    """
    fields = (cron or "").split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        return None
    if not (fields[0].isdigit() and fields[1].isdigit()):
        return None
    minute, hour = int(fields[0]), int(fields[1])
    if not (0 <= minute < 60 and 0 <= hour < 24):
        return None
    return hour, minute


def add_minutes(hour, minute, delta):
    total = (hour * 60 + minute + delta) % (24 * 60)
    return _hhmm(total // 60, total % 60)


# ---------------------------------------------------------------------------
# Ping URLs: compared as strings, never requested.
# ---------------------------------------------------------------------------

def env_file_value(name, text):
    """
    FIRST match wins, quotes stripped -- scripts/daily_update.ps1's Get-EnvValue
    translated, including its `Select-Object -First 1`.

    THIS DELIBERATELY DOES NOT CONSULT os.environ, and that is the whole point.
    load_dotenv() defaults to override=False, so a process or Windows user
    environment variable SHADOWS the file -- and python-dotenv resolves a
    duplicated key LAST-wins where Get-EnvValue takes the first. Either
    divergence lets this module audit a different check from the one
    daily_update.ps1 actually pings, which is the "green and lying" failure
    identifying by ping_url exists to prevent. The proof is only as strong as
    the url resolution, so the resolution has to be the pipeline's.
    """
    m = re.search(r"^\s*%s\s*=\s*(\S+)" % re.escape(name), text, re.M)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def resolve_ping_urls(env=None):
    """
    (am, pm) the way scripts/daily_update.ps1 resolves them: the slot-suffixed
    name first, the bare HEALTHCHECK_URL as a fallback for both. Either may be
    None.

    `env` is for tests. With None -- which is what production uses -- the values
    come from the .env FILE via env_file_value, never the process environment.
    """
    if env is None:
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        get = lambda n: env_file_value(n, text)
    else:
        get = env.get
    bare = get("HEALTHCHECK_URL")
    return (get("HEALTHCHECK_URL_AM") or bare,
            get("HEALTHCHECK_URL_PM") or bare)


def _norm(url):
    return (url or "").strip().strip("'\"").rstrip("/").lower()


def _redact(url):
    """
    Enough to tell AM from PM in a failure message, useless as a capability.
    A ping URL IS the credential, so it must never reach a log or an exception.
    """
    tail = _norm(url)[-4:]
    return "...%s" % tail if tail else "(unset)"


# ---------------------------------------------------------------------------
# The only network in this module.
# ---------------------------------------------------------------------------

def _transport():
    """
    The `requests` module, or whatever a test has bound in its place.

    Resolved through globals() rather than a module-level import so that a test
    can substitute a recording stub, while the allowlist check in _api_get still
    runs BEFORE anything here is touched.
    """
    mod = globals().get("requests")
    if mod is not None:
        return mod
    import requests as _r
    return _r


def _reject_unusable_key(api_key):
    """
    A key `requests` cannot even put in a header is OUR misconfiguration, not an
    outage -- but it raises from INSIDE requests before any socket opens, where
    the broad `except` in _api_get would file it as MonitorUnreachable and the
    live test would SKIP. A typo'd key silently switching the guard off is the
    one outcome this module exists to prevent, so classify it here, locally,
    first. (Sorting it out afterwards does not work: requests' InvalidHeader is
    an OSError subclass and so cannot be told from a real ConnectionError.)

    The key itself is never echoed -- it is a capability.
    """
    if not api_key:
        raise MonitorRejected("HEALTHCHECK_API_KEY is set but empty.")
    if api_key != api_key.strip() or re.search(r"[\r\n]", api_key):
        raise MonitorRejected(
            "HEALTHCHECK_API_KEY has surrounding whitespace or a line break, "
            "which cannot go in an HTTP header. Check .env for a wrapped or "
            "quoted value.")
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        raise MonitorRejected(
            "HEALTHCHECK_API_KEY contains a non-latin-1 character, which cannot "
            "go in an HTTP header. Check .env for a smart quote.")


def _api_get(url, api_key, timeout=(3, 10)):
    """
    GET the management API. Refuses any other host before the transport is even
    resolved -- in particular hc-ping.com, where a GET registers a SUCCESS.
    """
    if not url.startswith(_ALLOWED_PREFIX):
        raise PingUrlRefused(
            "refusing to request a url outside the management API. A GET on a "
            "ping url REGISTERS A SUCCESS and would tell the monitor the job "
            "ran when it did not.")
    _reject_unusable_key(api_key)
    http = _transport()
    try:
        resp = http.get(url, headers={"X-Api-Key": api_key},
                        timeout=timeout, allow_redirects=False)
    except PingUrlRefused:
        raise
    except Exception as exc:                       # transport, DNS, TLS, timeout
        raise MonitorUnreachable(
            "could not reach healthchecks.io: %s" % type(exc).__name__)
    code = getattr(resp, "status_code", None)
    if code in (401, 403):
        raise MonitorRejected(
            "healthchecks.io rejected the key in .env (HTTP %s). A key that is "
            "present and refused is a misconfiguration, not an outage." % code)
    if code is not None and 500 <= code < 600:
        raise MonitorUnreachable("healthchecks.io returned HTTP %s" % code)
    if code != 200:
        raise MonitorRejected("healthchecks.io returned HTTP %s" % code)
    return resp


def fetch_checks(api_key):
    """Every check the key can see. Unfiltered on purpose -- see API_URL."""
    return _api_get(API_URL, api_key).json().get("checks", [])


# ---------------------------------------------------------------------------
# The rules. Pure, so they can be fed known violations with no network.
# ---------------------------------------------------------------------------

_SLOT_ENV = {"morning": "HEALTHCHECK_URL_AM", "afternoon": "HEALTHCHECK_URL_PM"}


def check_schedule(checks, declared, am_url, pm_url):
    """
    Every way the live configuration can disagree with what the repo declares,
    as a list of strings. EMPTY is the expected result.

    Messages speak in CLOCK TIME, not cron syntax: the point of the message is
    that somebody edits the console in thirty seconds.

    Deliberately NOT violations, because each would cry wolf on an ordinary bad
    morning and a check that cries wolf gets switched off:
      - status == "down"   that is today's run having failed, which is the
                           monitor doing its job and alerting already
      - a stale last_ping  weekends, holidays, a powered-off desktop
      - n_pings == 0       a freshly created check legitimately has none
      - channels           semantics unverified; see the module docstring
    """
    out = []

    if _norm(am_url) and _norm(am_url) == _norm(pm_url):
        out.append(
            "HEALTHCHECK_URL_AM and HEALTHCHECK_URL_PM are the same check. One "
            "five-field cron cannot express both 07:45 and 13:00, so whichever "
            "slot the cron does not describe is unmonitored.")

    if checks and not any("ping_url" in c for c in checks):
        out.append(
            "the API key in .env is READ-ONLY: the listing carries no ping_url, "
            "so no check can be tied to the url the pipeline actually pings. "
            "Replace it with a read-write project key (healthchecks.io > "
            "project Settings > API Access). A read-only key cannot identify "
            "its subject, and an unidentified subject is the whole defect this "
            "guard exists to remove.")
        return out

    for slot, url in (("morning", am_url), ("afternoon", pm_url)):
        want = declared[slot]
        if not _norm(url):
            out.append("%s is not set in .env, so the %s slot is unmonitored."
                       % (_SLOT_ENV[slot], slot))
            continue
        hits = [c for c in checks if _norm(c.get("ping_url")) == _norm(url)]
        if len(hits) != 1:
            names = ", ".join(sorted(str(c.get("name") or "(unnamed)")
                                     for c in checks)) or "(none)"
            out.append(
                "%s (%s) matches %d checks, expected exactly 1. The key can see: "
                "%s. Zero matches means hc-ping.com is 404ing that url, "
                "Ping-Health logs a WARN and carries on, and the %s slot is "
                "silently unmonitored."
                % (_SLOT_ENV[slot], _redact(url), len(hits), names, slot))
            continue
        out.extend(_judge(slot, hits[0], want))
    return out


def _judge(slot, check, want):
    """One identified check against one declared row."""
    out = []
    label = "%s check" % slot
    schedule = (check.get("schedule") or "").strip()

    if not schedule:
        out.append(
            "the %s is an INTERVAL check (timeout %ss), not a cron check. A "
            "period check alerts `timeout` after the LAST ping, so its deadline "
            "drifts forward with every run instead of sitting at %s. Set it to "
            "cron `%s` in %s."
            % (label, check.get("timeout"), want["alert_hhmm"], want["cron"], TZ))
        return out

    if (check.get("tz") or "") != want["tz"]:
        out.append("the %s is in timezone %r, not %r."
                   % (label, check.get("tz"), want["tz"]))

    live = cron_time(schedule)
    wanted = cron_time(want["cron"])
    if live is None:
        out.append(
            "the %s cron is %r, which this guard cannot read as a plain daily "
            "`M H * * *`. The Windows job runs every day -- logs show Saturday "
            "and Sunday runs -- so a weekday range, step or list is a defect, "
            "not a style choice. Expected `%s`."
            % (label, schedule, want["cron"]))
    elif wanted is not None and live != wanted:
        lh, lm = live
        out.append(
            "the %s cron is `%s`, which alerts at %s; the repo triggers at %s "
            "and judges against %s. Set it to exactly `%s`."
            % (label, schedule, add_minutes(lh, lm, want["grace_min"]),
               _hhmm(*wanted), want["alert_hhmm"], want["cron"]))

    grace = check.get("grace")
    if grace != want["grace_s"]:
        extra = ""
        if slot == "morning":
            extra = (" check_run.ps1's deadline and the monitor's alert then no "
                     "longer judge the same instant, which is what that file's "
                     "own comment claims they do.")
        out.append(
            "the %s grace is %s, expected %s.%s"
            % (label, _mins(grace), _mins(want["grace_s"]), extra))

    if (check.get("status") or "") == "paused":
        out.append("the %s is PAUSED -- it will never alert." % label)
    return out


def _mins(seconds):
    if not isinstance(seconds, int):
        return repr(seconds)
    return "%ds (%g min)" % (seconds, seconds / _SECONDS_PER_MIN)


def notes_for(checks, am_url, pm_url):
    """Reported, never asserted. See check_schedule's docstring for why."""
    notes = []
    for slot, url in (("morning", am_url), ("afternoon", pm_url)):
        # Same guard check_schedule has. Without it an UNSET url normalises to
        # "" and matches every row whose ping_url key is absent -- the
        # read-only-key listing shape -- attributing an arbitrary check to a
        # slot that is not configured at all.
        if not _norm(url):
            continue
        hits = [c for c in checks if _norm(c.get("ping_url")) == _norm(url)]
        if len(hits) != 1:
            continue
        c = hits[0]
        notes.append(
            "%s: status=%s last_ping=%s n_pings=%s channels=%r"
            % (slot, c.get("status"), c.get("last_ping"), c.get("n_pings"),
               c.get("channels")))
    return notes


# ---------------------------------------------------------------------------
# The verdict.
# ---------------------------------------------------------------------------

def audit(env=None):
    """
    {'verified', 'reason', 'violations', 'notes'}.

    THERE IS NO PATH THAT SETS verified=True WITHOUT A SUCCESSFUL MANAGEMENT-API
    READ. That is what keeps "clean" and "unverified" different objects rather
    than the same object formatted differently -- the README's own philosophy,
    made machine-checkable, and the reason the no-key test is never skipped.
    """
    declared = declared_from_readme(README.read_text(encoding="utf-8"))
    # The URLS come from the .env FILE, and `env` stays None here in production
    # so that they do. Collapsing this to os.environ -- which an earlier version
    # of this function did -- quietly puts the resolution back on python-dotenv's
    # last-wins, env-shadowed semantics and undoes the whole point of
    # resolve_ping_urls. The identification is only a proof if the url is the one
    # daily_update.ps1 pings.
    am_url, pm_url = resolve_ping_urls(env)
    # The KEY is different and is read from the process environment, which
    # load_dotenv has already populated from .env. daily_update.ps1 never
    # resolves it, so there is no pipeline behaviour to match, and an ordinary
    # environment variable is what lets this run somewhere without a .env.
    key = (os.environ if env is None else env).get("HEALTHCHECK_API_KEY")
    if not key:
        return {
            "verified": False,
            "reason": ("HEALTHCHECK_API_KEY is not in .env, so the LIVE "
                       "Healthchecks.io cron was NOT read back. The repo's own "
                       "declarations were still checked against each other."),
            "violations": [],
            "notes": [],
        }

    checks = fetch_checks(key)
    return {
        "verified": True,
        "reason": None,
        "violations": check_schedule(checks, declared, am_url, pm_url),
        "notes": notes_for(checks, am_url, pm_url),
    }


def _main():
    """
    What scripts/check_run.ps1 section 5 prints. Never emits the key or a full
    ping url; _redact exists for that reason.
    """
    declared = declared_from_readme(README.read_text(encoding="utf-8"))
    try:
        result = audit()
    except (MonitorUnreachable, MonitorRejected, PingUrlRefused) as exc:
        print("   monitor cron NOT VERIFIED - %s" % exc)
        return 0
    if not result["verified"]:
        print("   monitor cron NOT VERIFIED - %s" % result["reason"])
        return 0
    if result["violations"]:
        print("   monitor cron WRONG on Healthchecks.io:")
        for v in result["violations"]:
            print("     - %s" % v)
    else:
        m = declared["morning"]
        print("   monitor cron VERIFIED against Healthchecks.io "
              "(morning `%s`, grace %d min, %s -> alerts %s)"
              % (m["cron"], m["grace_min"], TZ, m["alert_hhmm"]))
    for n in result["notes"]:
        print("     note: %s" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
