"""
Proof that healthcheck_schedule.py can actually fail.

CLAUDE.md: "If you add a guard, prove it fails on bad input before trusting it."
Three guards in this repo's history could not -- one printed the same variable
under both labels, one asserted a tautology, one banned a string in comments
rather than in queries. So the rules live in a pure function and the tests below
feed it known violations, every one of them a real incident or a real
misconfiguration rather than an invented one.

THREE GROUPS, AND THEY ARE ABOUT DIFFERENT THINGS.

  local/     the repo against itself -- check_run.ps1 vs the README table, and
             each README row against its own arithmetic. No key, no network, so
             these run on CI, on the droplet and on a fresh clone.
  mutations/ the live comparison fed bad listings. M0 must come first: without a
             clean baseline, a check_schedule() that rejected everything would
             satisfy every other test here. That is the tautology failure.
  ping/      behavioural proof that nothing ever requests a ping url. A GET on
             one REGISTERS A SUCCESS. A source grep is explicitly the weaker
             proof, so these stub the transport and assert what was called.
"""
import os
import re
from pathlib import Path

import pytest

import healthcheck_schedule as hs

REPO = Path(__file__).resolve().parent.parent

# Fabricated, so the fixtures are deterministic and no capability url is ever
# written into a tracked file.
AM_URL = "https://hc-ping.com/11111111-1111-1111-1111-111111111111"
PM_URL = "https://hc-ping.com/22222222-2222-2222-2222-222222222222"


def declared():
    return hs.declared_from_readme(hs.README.read_text(encoding="utf-8"))


def _good_listing(d=None):
    """
    The listing a correctly-configured account returns, derived from the
    README's own table so the baseline tracks the repo and the mutations below
    stay mutations rather than slowly becoming the expected state.
    """
    d = d or declared()
    return [
        {"name": "JSA FCI morning", "ping_url": AM_URL,
         "schedule": d["morning"]["cron"], "grace": d["morning"]["grace_s"],
         "tz": hs.TZ, "status": "up", "n_pings": 412,
         "last_ping": "2026-10-06T09:07:12+00:00", "channels": "*"},
        {"name": "JSA FCI afternoon", "ping_url": PM_URL,
         "schedule": d["afternoon"]["cron"], "grace": d["afternoon"]["grace_s"],
         "tz": hs.TZ, "status": "up", "n_pings": 410,
         "last_ping": "2026-10-06T18:20:00+00:00", "channels": "*"},
    ]


def judge(checks, d=None, am=AM_URL, pm=PM_URL):
    return hs.check_schedule(checks, d or declared(), am, pm)


def mutate(index=0, **fields):
    """A copy of the good listing with one check's fields overridden."""
    rows = _good_listing()
    rows[index] = dict(rows[index], **fields)
    return rows


def shifted_cron(cron, delta_min):
    """
    The declared cron moved `delta_min` minutes.

    Mutations are DERIVED rather than hardcoded so that a legitimate trigger
    move -- done properly in every place the guard binds -- leaves this suite
    green. A fixture pinned to `45 7 * * *` would red on the one change the
    guard exists to police, and a guard that cries wolf gets switched off.
    """
    hour, minute = hs.cron_time(cron)
    total = (hour * 60 + minute + delta_min) % (24 * 60)
    return "%d %d * * *" % (total % 60, total // 60)


def _mutated(text, old, new, what):
    """
    A one-shot replace that refuses to be a no-op.

    The README table and the trigger line are matched by the production parsers
    with `\\s*` tolerance, but these fixtures use exact text. A purely cosmetic
    re-alignment would otherwise turn the mutation into nothing and leave the
    test below asserting about an unmodified file.
    """
    out = text.replace(old, new, 1)
    assert out != text, (
        "the %s fixture no longer matches the file, so this mutation did "
        "nothing. Re-point it at the current wording." % what)
    return out


# ---------------------------------------------------------------------------
# local: the repo against itself. No key, no network.
# ---------------------------------------------------------------------------

def test_check_run_holds_exactly_one_trigger_and_one_grace():
    """
    L1. _one() refuses a file it no longer understands.

    Deliberately NOT pinned to 07:45. A trigger move done properly in every
    place the guard binds must leave this suite green; L3 is what checks the
    VALUE, by binding it to the README rather than to a literal here.
    """
    t = hs.trigger_from_check_run(hs.CHECK_RUN.read_text(encoding="utf-8"))
    assert 0 <= t["hour"] < 24 and 0 <= t["minute"] < 60
    assert t["grace_min"] > 0


def test_the_wake_band_literals_are_not_mistaken_for_the_grace():
    """
    L1b. check_run.ps1 carries $trigger.AddMinutes(-30) and .AddMinutes(10) for
    the WakeToRun band. The grace pattern is anchored on `(TriggerOn $t0)` so
    those cannot match -- if that anchor is ever loosened, _one() sees three
    hits and this fails rather than silently taking the first.
    """
    src = hs.CHECK_RUN.read_text(encoding="utf-8")
    assert ".AddMinutes(-30)" in src and ".AddMinutes(10)" in src
    assert hs.trigger_from_check_run(src)["grace_min"] == 45


def test_the_readme_declares_both_slots():
    """L2."""
    d = declared()
    assert set(d) == {"morning", "afternoon"}
    for slot in d:
        assert hs.cron_time(d[slot]["cron"]) is not None, slot
        assert d[slot]["grace_s"] == d[slot]["grace_min"] * 60


def test_the_readme_morning_row_matches_check_run_ps1():
    """
    L3. The 2026-09-29/10-01 class of bug, caught with no key at all: the two
    repo-side copies of the morning schedule disagreeing with each other.
    """
    t = hs.trigger_from_check_run(hs.CHECK_RUN.read_text(encoding="utf-8"))
    hour, minute = hs.cron_time(declared()["morning"]["cron"])
    assert (hour, minute) == (t["hour"], t["minute"])
    assert declared()["morning"]["grace_min"] == t["grace_min"]


def test_each_row_agrees_with_its_own_alerts_column():
    """
    L4. cron + grace must equal the row's own `alerts` value. Catches a README
    edit that moves a cron and leaves the advertised alert time stale -- a
    divergence nothing else here covers.
    """
    for slot, row in declared().items():
        hour, minute = hs.cron_time(row["cron"])
        assert hs.add_minutes(hour, minute, row["grace_min"]) == row["alert_hhmm"], slot


def test_the_two_slots_have_different_crons():
    """L5. If they were equal, one slot would be unmonitored by construction."""
    d = declared()
    assert d["morning"]["cron"] != d["afternoon"]["cron"]


def test_setup_step_2_agrees_with_the_table():
    """
    L6. The prose a human actually follows. ISOLATED on purpose: it wraps
    mid-clause across two lines, so a reword should fail this one test rather
    than take the live comparison offline.
    """
    md = hs.README.read_text(encoding="utf-8")
    m = re.search(r"Set its schedule to \*\*cron `([^`]+)`\*\*,\s+timezone "
                  r"\*\*([A-Za-z/_]+)\*\*,\s+grace period \*\*(\d+) minutes\*\*."
                  r"\s+That makes the alert fire at \*\*(\d{2}:\d{2})\*\*", md)
    assert m, "setup step 2 no longer parses -- re-point this test at its wording"
    cron, tz, grace, alert = m.groups()
    row = declared()["morning"]
    assert (cron, tz, int(grace), alert) == (
        row["cron"], hs.TZ, row["grace_min"], row["alert_hhmm"])


def test_every_timezone_mention_is_the_one_constant():
    """L7."""
    md = hs.README.read_text(encoding="utf-8")
    found = set(re.findall(r"America/[A-Za-z_]+", md))
    assert found == {hs.TZ}, found


def test_the_env_urls_are_two_distinct_ping_urls():
    """
    L8. Values are compared, never printed, never fetched. Skipped on a clone
    with no .env rather than failing -- .env is gitignored.
    """
    if not (REPO / ".env").exists():
        pytest.skip("no .env in this checkout")
    am, pm = hs.resolve_ping_urls()
    if not am or not pm:
        pytest.skip("healthcheck urls not configured in this .env")
    # pytest.fail, NOT assert. The assertion rewriter prints every
    # sub-expression of a failing `assert`, so `assert hs._norm(am) != ...`
    # would put the capability url itself into the failure block -- under a
    # message that says "redacted". A call is not instrumented, so nothing but
    # what is written here is ever rendered.
    if hs._norm(am) == hs._norm(pm):
        pytest.fail("HEALTHCHECK_URL_AM and _PM are the same check (%s); one "
                    "slot is unmonitored" % hs._redact(am))
    for name, url in (("AM", am), ("PM", pm)):
        if not re.match(r"^https://hc-ping\.com/", url.strip().strip("'\"")):
            pytest.fail("HEALTHCHECK_URL_%s is not an hc-ping.com url "
                        "(redacted: %s)" % (name, hs._redact(url)))


# ---------------------------------------------------------------------------
# mutations: the proof the live comparison can fail.
# ---------------------------------------------------------------------------

def test_M0_the_baseline_is_clean():
    """
    FIRST AND LOAD-BEARING. Without this, a check_schedule() that rejected
    every input would satisfy every test below it -- CLAUDE.md's "asserted a
    tautology" failure, which this repo has shipped before.
    """
    assert judge(_good_listing()) == []


def test_M1_a_cron_EARLIER_than_declared_the_2026_09_30_shape():
    """
    The trigger had moved to 08:00 and the check still said `30 7 * * *`, so it
    alerted before a healthy run could finish and went red every morning.

    The stale cron is derived by shifting the declared one 15 minutes earlier,
    so this stays true if the trigger legitimately moves.
    """
    d = declared()["morning"]
    stale = shifted_cron(d["cron"], -15)
    out = judge(mutate(0, schedule=stale))
    hour, minute = hs.cron_time(stale)
    assert len(out) == 1
    assert hs.add_minutes(hour, minute, d["grace_min"]) in out[0], out[0]
    assert "`%s`" % d["cron"] in out[0], out[0]


def test_M2_a_cron_LATER_than_declared_the_still_open_2026_10_01_shape():
    """
    THE HEADLINE, and the case Healthchecks.io structurally cannot report about
    itself: a cron LATER than the trigger never goes red, because moving a job
    earlier cannot trip it. The only symptom is a monitor sitting loose.

    The message must name both the time it alerts and the time the repo judges
    against, because those two numbers are what get the console edited.
    """
    d = declared()["morning"]
    stale = shifted_cron(d["cron"], +15)
    out = judge(mutate(0, schedule=stale))
    hour, minute = hs.cron_time(stale)
    assert len(out) == 1
    assert hs.add_minutes(hour, minute, d["grace_min"]) in out[0], out[0]
    assert d["alert_hhmm"] in out[0], out[0]
    assert "`%s`" % d["cron"] in out[0], out[0]


def test_M2b_that_shape_is_todays_open_incident_verbatim():
    """
    Pins M2 to history while the declared trigger is still 07:45. Guarded, so a
    legitimate move retires this rather than reddening it -- the incident is a
    fact about 2026-10-01, not a requirement on the future.
    """
    d = declared()["morning"]
    if d["cron"] != "45 7 * * *":
        pytest.skip("the morning trigger has moved since 2026-10-01")
    assert shifted_cron(d["cron"], +15) == "0 8 * * *"
    assert shifted_cron(d["cron"], -15) == "30 7 * * *"
    out = judge(mutate(0, schedule="0 8 * * *"))
    assert "08:45" in out[0] and "08:30" in out[0], out[0]


def test_M3_the_afternoon_slot_is_really_covered():
    out = judge(mutate(1, schedule="0 14 * * *"))
    assert len(out) == 1 and "afternoon" in out[0]


def test_M3b_every_judged_message_names_the_slot_it_is_about():
    """
    The rest of the M-tests assert `len(out) == 1` plus a substring drawn from a
    value or a time, and never that the message names WHICH check it means. A
    _judge that labelled every message "afternoon check" would pass all of them
    and send Ross to the wrong console row at 08:30.
    """
    for index, slot in ((0, "morning"), (1, "afternoon")):
        for field in ({"schedule": "0 23 * * *"}, {"tz": "UTC"},
                      {"grace": 60}, {"status": "paused"},
                      {"schedule": "", "timeout": 86400}):
            out = judge(mutate(index, **field))
            assert out, (slot, field)
            for message in out:
                assert slot in message, (slot, field, message)


def test_M4_an_interval_check_is_rejected():
    rows = _good_listing()
    del rows[0]["schedule"]
    rows[0]["timeout"] = 86400
    out = judge(rows)
    assert len(out) == 1 and "INTERVAL" in out[0] and "86400" in out[0]


def test_M5_an_empty_schedule_string_is_rejected_too():
    """The .get(...) or "" path, not just the missing-key path."""
    out = judge(mutate(0, schedule="", timeout=86400))
    assert len(out) == 1 and "INTERVAL" in out[0]


def test_M6_the_wrong_timezone_is_rejected():
    out = judge(mutate(0, tz="UTC"))
    assert len(out) == 1 and "UTC" in out[0]


def test_M7_a_shortened_grace_is_rejected():
    out = judge(mutate(0, grace=1800))
    assert len(out) == 1 and "1800" in out[0]
    assert "same instant" in out[0], "the morning message must name the claim it breaks"


def test_M8_a_weekday_only_cron_is_rejected():
    """logs/ shows Saturday and Sunday runs, so `1-5` is a defect."""
    out = judge(mutate(0, schedule="45 7 * * 1-5"))
    assert len(out) == 1 and "Saturday" in out[0]


def test_M9_an_unmatched_url_fails_AND_does_not_suppress_the_other_slot():
    """
    Two assertions in one because the second is the easy thing to get wrong:
    an early `return` on identification failure would hide a real afternoon
    violation behind a morning one.
    """
    rows = mutate(0, ping_url="https://hc-ping.com/deadbeef-0000-0000-0000-000000000000")
    rows[1] = dict(rows[1], schedule="0 14 * * *")
    out = judge(rows)
    assert len(out) == 2
    assert any("matches 0 checks" in v for v in out)
    assert any("afternoon" in v and "0 14" in v for v in out)


def test_M9b_the_zero_match_message_names_what_the_key_can_see():
    out = judge(mutate(0, ping_url="https://hc-ping.com/deadbeef-0000-0000-0000-000000000000"))
    assert "JSA FCI afternoon" in out[0], out[0]


def test_M10_a_duplicated_check_is_ambiguous_not_silently_first():
    rows = _good_listing()
    rows.append(dict(rows[0], name="JSA FCI morning (copy)", schedule="0 8 * * *"))
    out = judge(rows)
    assert len(out) == 1 and "matches 2 checks" in out[0]


def test_M11_one_url_for_both_slots_is_rejected():
    out = judge(_good_listing(), am=AM_URL, pm=AM_URL)
    assert any("same check" in v for v in out)


def test_M21_an_unset_slot_url_is_its_own_violation():
    """
    A machine with only HEALTHCHECK_URL_AM configured. Without this the branch
    that reports an unmonitored slot is dead to the suite and could be deleted
    with everything still green -- and the fall-through message would blame
    hc-ping.com for 404ing a url that was never set.
    """
    out = judge(_good_listing(), pm=None)
    assert len(out) == 1
    assert "HEALTHCHECK_URL_PM" in out[0] and "unmonitored" in out[0], out[0]
    assert "404" not in out[0], out[0]


def test_M21b_an_unset_url_produces_no_note_either():
    """
    notes_for() has the same guard for the same reason: an unset url normalises
    to "" and would otherwise match every row whose ping_url key is absent,
    attributing an arbitrary check to a slot that is not configured.
    """
    # Exactly ONE ping_url-less row, deliberately. With two, both match the
    # empty string, len(hits) != 1 drops them, and the test passes whether the
    # guard is there or not -- which is how the first version of this test
    # failed to kill the mutant it was written for.
    rows = [{k: v for k, v in c.items() if k != "ping_url"}
            for c in _good_listing()[:1]]
    assert hs.notes_for(rows, None, None) == []
    assert hs.notes_for(rows, None, PM_URL) == []


def test_M12_a_read_only_key_fails_rather_than_matching_nothing():
    """
    The trap the design turns on. With no ping_url, name/slug matching would
    audit a check nobody pings and come back green -- the exact genus of bug
    this guard exists to kill. So it must be a violation, never a skip.
    """
    rows = [{k: v for k, v in c.items() if k != "ping_url"} for c in _good_listing()]
    for c in rows:
        c["unique_key"] = "abc123"
    out = judge(rows)
    assert len(out) == 1 and "READ-ONLY" in out[0]


def test_M13_a_paused_check_is_rejected():
    out = judge(mutate(0, status="paused"))
    assert len(out) == 1 and "PAUSED" in out[0]


def test_M14_a_down_check_is_NOT_a_violation():
    """
    The deliberate non-assertion. status=="down" means this morning's run
    failed -- which is the monitor working, and already alerting. Flagging it
    here would turn a bad morning into a red suite.
    """
    assert judge(mutate(0, status="down")) == []


def test_M15_a_stale_last_ping_and_zero_pings_are_NOT_violations():
    """Weekends, holidays, a powered-off desktop, a freshly created check."""
    assert judge(mutate(0, last_ping="2026-10-01T12:00:00+00:00", n_pings=0)) == []


def test_M16_nothing_here_is_hardcoded_to_0745():
    """
    THE ANTI-HARDCODE TEST. Move the declared schedule and the expectation must
    move with it -- a literal EXPECTED_CRON = "45 7 * * *" would pass every
    other test in this file and fail this one.
    """
    d = declared()
    d["morning"] = dict(d["morning"], cron="15 6 * * *", alert_hhmm="07:00")
    out = judge(_good_listing(), d=d)       # listing still says 45 7 * * *
    assert len(out) == 1 and "`15 6 * * *`" in out[0]
    # and the previously-failing value is now the clean one
    assert judge(mutate(0, schedule="15 6 * * *"), d=d) == []


def test_M16b_the_check_run_parser_is_not_hardcoded_either():
    src = hs.CHECK_RUN.read_text(encoding="utf-8")
    moved = _mutated(src, "$trigH = 7; $trigM = 45", "$trigH = 6; $trigM = 15",
                     "check_run.ps1 trigger line")
    t = hs.trigger_from_check_run(moved)
    assert (t["hour"], t["minute"]) == (6, 15)


def test_M16c_the_GRACE_expectation_is_not_hardcoded_to_2700():
    """
    M16's twin for the other half of the live comparison. M16 moves the cron;
    this moves the grace. A literal 2700 in _judge passes every other test here
    -- M7 included, since it feeds 1800 and stays red either way -- and fails
    only this one.
    """
    d = declared()
    d["morning"] = dict(d["morning"], grace_min=30, grace_s=1800,
                        alert_hhmm=hs.add_minutes(
                            *hs.cron_time(d["morning"]["cron"]), 30))
    out = judge(_good_listing(), d=d)       # listing still carries 2700
    assert len(out) == 1 and "2700" in out[0], out
    assert judge(mutate(0, grace=1800), d=d) == []


def test_M16d_the_AFTERNOON_expectation_is_not_hardcoded_either():
    """
    The slot with NO second repo-side witness: the README afternoon row is the
    only thing driving it, because check_run.ps1 judges only the first run of
    the day. A literal `0 13 * * *` in the judging would leave 13:00 unpinned,
    and M3 cannot tell the difference -- it feeds a bad cron and checks that
    something was said, which a hardcoded expectation also does.
    """
    d = declared()
    moved = shifted_cron(d["afternoon"]["cron"], +30)
    d["afternoon"] = dict(d["afternoon"], cron=moved,
                          alert_hhmm=hs.add_minutes(
                              *hs.cron_time(moved), d["afternoon"]["grace_min"]))
    out = judge(_good_listing(), d=d)       # listing still says 0 13 * * *
    assert len(out) == 1 and "`%s`" % moved in out[0], out
    assert judge(mutate(1, schedule=moved), d=d) == []


def row_text(slot, cron, grace_min, alert):
    """The README table row as the file writes it, rebuilt from live values."""
    return "| %s | `%s` | %d min | %s |" % (slot, cron, grace_min, alert)


def morning_row(**over):
    d = dict(declared()["morning"], **over)
    return row_text("morning", d["cron"], d["grace_min"], d["alert_hhmm"])


def test_M17_a_readme_cron_edit_breaks_the_local_chain():
    """The README morning row moved and check_run.ps1 left behind."""
    md = _mutated(hs.README.read_text(encoding="utf-8"), morning_row(),
                  morning_row(cron=shifted_cron(declared()["morning"]["cron"], 15)),
                  "README morning row")
    hour, minute = hs.cron_time(hs.declared_from_readme(md)["morning"]["cron"])
    t = hs.trigger_from_check_run(hs.CHECK_RUN.read_text(encoding="utf-8"))
    assert (hour, minute) != (t["hour"], t["minute"])


def test_M18_a_stale_alerts_column_is_caught_by_its_own_arithmetic():
    md = _mutated(hs.README.read_text(encoding="utf-8"), morning_row(),
                  morning_row(alert_hhmm=hs.add_minutes(
                      *hs.cron_time(declared()["morning"]["cron"]), 999)),
                  "README morning row")
    row = hs.declared_from_readme(md)["morning"]
    hour, minute = hs.cron_time(row["cron"])
    assert hs.add_minutes(hour, minute, row["grace_min"]) != row["alert_hhmm"]


def test_M19_a_deleted_readme_row_fails_loudly():
    md = _mutated(hs.README.read_text(encoding="utf-8"),
                  morning_row() + "\n", "", "README morning row")
    with pytest.raises(AssertionError, match="exactly one"):
        hs.declared_from_readme(md)


def test_M20_a_duplicated_trigger_literal_fails_loudly():
    line = re.search(r"\$trigH\s*=\s*\d{1,2}\s*;\s*\$trigM\s*=\s*\d{1,2}",
                     hs.CHECK_RUN.read_text(encoding="utf-8")).group(0)
    src = _mutated(hs.CHECK_RUN.read_text(encoding="utf-8"), line,
                   line + "\n" + line, "check_run.ps1 trigger line")
    with pytest.raises(AssertionError, match="exactly one"):
        hs.trigger_from_check_run(src)


# ---------------------------------------------------------------------------
# ping: behavioural proof that hc-ping.com is never requested.
# ---------------------------------------------------------------------------

class Recorder:
    """
    Stands in for `requests`. Raises on every attribute except `get`, so a later
    .post / .head / .Session() is caught by the same stub rather than quietly
    going out over the wire.
    """

    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        assert "hc-ping.com" not in url, "A PING URL WAS REQUESTED: %s" % url
        self.calls.append((url, kw))
        return FakeResponse()

    def __getattr__(self, name):
        raise AssertionError("healthcheck_schedule touched requests.%s" % name)


class FakeResponse:
    status_code = 200

    def json(self):
        return {"checks": _good_listing()}


def test_P1_the_whole_audit_makes_exactly_one_request_to_the_api():
    rec = Recorder()
    hs.requests = rec
    try:
        result = hs.audit({"HEALTHCHECK_API_KEY": "hc_SECRET_TOKEN",
                           "HEALTHCHECK_URL_AM": AM_URL,
                           "HEALTHCHECK_URL_PM": PM_URL})
    finally:
        del hs.requests
    assert result["verified"] is True and result["violations"] == []
    assert len(rec.calls) == 1
    url, kw = rec.calls[0]
    assert url is hs.API_URL, "the api url must be used verbatim, not rebuilt"
    assert kw["allow_redirects"] is False, "a redirect could leave the allowlist"
    assert kw["headers"] == {"X-Api-Key": "hc_SECRET_TOKEN"}
    # the key travels in the header and nowhere else -- never a query string
    assert "hc_SECRET_TOKEN" not in url


def test_P1b_audit_propagates_a_violation_from_the_listing_it_fetched():
    """
    audit() is the ONLY function _main(), check_run.ps1 section 5 and the live
    test ever call. Every M-test calls check_schedule() directly, and P1, P5 and
    S1 only ever watch audit() return []. Without this, gutting audit() to
    always report "violations": [] leaves the whole suite green while the
    still-open 2026-10-01 cron goes unreported -- which is the production path,
    not a corner of it.
    """
    class Stale(Recorder):
        def get(self, url, **kw):
            assert "hc-ping.com" not in url
            self.calls.append((url, kw))
            return type("R", (), {
                "status_code": 200,
                "json": staticmethod(
                    lambda: {"checks": mutate(0, schedule="0 8 * * *")})})()

    hs.requests = Stale()
    try:
        result = hs.audit({"HEALTHCHECK_API_KEY": "hc_SECRET_TOKEN",
                           "HEALTHCHECK_URL_AM": AM_URL,
                           "HEALTHCHECK_URL_PM": PM_URL})
    finally:
        del hs.requests
    assert result["verified"] is True
    assert len(result["violations"]) == 1, result["violations"]
    assert "08:30" in result["violations"][0]


def test_P2_a_ping_url_is_refused_before_the_transport_is_touched():
    rec = Recorder()
    hs.requests = rec
    try:
        with pytest.raises(hs.PingUrlRefused):
            hs._api_get(AM_URL, "k")
    finally:
        del hs.requests
    assert rec.calls == [], "refusal must precede any socket"


def test_P3_the_allowlist_is_load_bearing(monkeypatch):
    """
    Without this, P2 could be passing for some other reason. Neutralise the
    allowlist and the same call must then reach the transport -- which proves
    the allowlist is the thing stopping it.
    """
    rec = Recorder()
    rec.get = lambda url, **kw: rec.calls.append((url, kw)) or FakeResponse()
    monkeypatch.setattr(hs, "_ALLOWED_PREFIX", "")
    hs.requests = rec
    try:
        hs._api_get(AM_URL, "k")
    finally:
        del hs.requests
    assert len(rec.calls) == 1 and "hc-ping.com" in rec.calls[0][0]


def test_P4_the_allowlist_is_a_prefix_not_a_substring():
    with pytest.raises(hs.PingUrlRefused):
        hs._api_get("https://hc-ping.com/x?u=https://healthchecks.io/api/v1/", "k")


def test_P5_ping_urls_in_the_payload_do_not_become_requests():
    """
    Not hypothetical: a read-write key means real ping urls ARE in the response.
    The data carrying them must not cause them to be fetched.
    """
    rec = Recorder()
    hs.requests = rec
    try:
        hs.audit({"HEALTHCHECK_API_KEY": "k", "HEALTHCHECK_URL_AM": AM_URL,
                  "HEALTHCHECK_URL_PM": PM_URL})
    finally:
        del hs.requests
    assert len(rec.calls) == 1


def test_P6_neither_the_key_nor_a_full_url_reaches_a_message():
    rows = mutate(0, ping_url="https://hc-ping.com/0000-not-a-match")
    out = " ".join(judge(rows))
    assert AM_URL not in out
    assert "11111111-1111-1111-1111-111111111111" not in out
    assert hs._redact(AM_URL) in out, "the message must still distinguish AM from PM"


def test_P6b_a_rejected_key_is_not_echoed():
    class Rejecting(Recorder):
        def get(self, url, **kw):
            self.calls.append((url, kw))
            return type("R", (), {"status_code": 401})()

    hs.requests = Rejecting()
    try:
        with pytest.raises(hs.MonitorRejected) as exc:
            hs.fetch_checks("hc_SECRET_TOKEN")
    finally:
        del hs.requests
    assert "hc_SECRET_TOKEN" not in str(exc.value)


# ---------------------------------------------------------------------------
# url resolution: it must be the PIPELINE's, not python's.
# ---------------------------------------------------------------------------

def test_the_url_is_resolved_the_way_daily_update_ps1_resolves_it(tmp_path, monkeypatch):
    """
    FIRST match wins, and the process environment is ignored -- because
    Get-EnvValue in scripts/daily_update.ps1 does both.

    This is not pedantry. python-dotenv is LAST-wins on a duplicated key, and
    load_dotenv() defaults to override=False so a Windows user variable SHADOWS
    the file. Either divergence would let this module identify, audit and bless
    a different check from the one that actually receives the morning ping --
    green while the real dead-man's switch is stale. Identifying by ping_url is
    only a proof if the url is the one the pipeline uses.
    """
    env = tmp_path / ".env"
    env.write_text(
        "HEALTHCHECK_URL_AM=https://hc-ping.com/first-wins\n"
        "HEALTHCHECK_URL_AM=https://hc-ping.com/second-loses\n"
        "HEALTHCHECK_URL_PM=https://hc-ping.com/pm\n", encoding="utf-8")
    monkeypatch.setattr(hs, "ENV_FILE", env)
    monkeypatch.setenv("HEALTHCHECK_URL_AM", "https://hc-ping.com/process-must-lose")
    am, pm = hs.resolve_ping_urls()
    assert am == "https://hc-ping.com/first-wins"
    assert pm == "https://hc-ping.com/pm"


def test_audit_ITSELF_resolves_from_the_file_not_the_process(tmp_path, monkeypatch):
    """
    The production entry point, not just the helper.

    This exists because the first version of audit() did
    `env = os.environ if env is None else env` and then handed that to
    resolve_ping_urls -- which silently put resolution back on os.environ for
    every real caller (check_run.ps1 section 5 and the live test both call
    audit() with no arguments), while the helper's own test went on passing.
    A guard on a helper is not a guard on the path production takes.
    """
    env = tmp_path / ".env"
    env.write_text("HEALTHCHECK_URL_AM=https://hc-ping.com/from-the-file\n"
                   "HEALTHCHECK_URL_PM=https://hc-ping.com/pm-file\n",
                   encoding="utf-8")
    monkeypatch.setattr(hs, "ENV_FILE", env)
    monkeypatch.setenv("HEALTHCHECK_URL_AM", "https://hc-ping.com/from-the-process")
    monkeypatch.setenv("HEALTHCHECK_API_KEY", "hc_SECRET_TOKEN")

    d = declared()
    listing = [
        {"name": "m", "ping_url": "https://hc-ping.com/from-the-file",
         "schedule": d["morning"]["cron"], "grace": d["morning"]["grace_s"],
         "tz": hs.TZ, "status": "up"},
        {"name": "a", "ping_url": "https://hc-ping.com/pm-file",
         "schedule": d["afternoon"]["cron"], "grace": d["afternoon"]["grace_s"],
         "tz": hs.TZ, "status": "up"},
    ]

    class Stub(Recorder):
        def get(self, url, **kw):
            self.calls.append((url, kw))
            return type("R", (), {"status_code": 200,
                                  "json": staticmethod(lambda: {"checks": listing})})()

    hs.requests = Stub()
    try:
        result = hs.audit()
    finally:
        del hs.requests
    # If audit resolved from the process environment, the morning url would be
    # /from-the-process, match nothing, and this would be a violation.
    assert result["violations"] == [], result["violations"]


def test_a_bare_HEALTHCHECK_URL_falls_back_for_both_slots(tmp_path, monkeypatch):
    """daily_update.ps1 honours it, so resolution must -- check_schedule then
    reports the uncovered slot, which is a different question."""
    env = tmp_path / ".env"
    env.write_text("HEALTHCHECK_URL=https://hc-ping.com/single\n", encoding="utf-8")
    monkeypatch.setattr(hs, "ENV_FILE", env)
    assert hs.resolve_ping_urls() == ("https://hc-ping.com/single",) * 2


def test_quotes_and_padding_are_stripped_like_get_envvalue():
    text = '  HEALTHCHECK_URL_AM = "https://hc-ping.com/quoted"  \n'
    assert hs.env_file_value("HEALTHCHECK_URL_AM", text) == \
        "https://hc-ping.com/quoted"
    assert hs.env_file_value("NOT_PRESENT", text) is None


def test_a_malformed_api_key_FAILS_rather_than_skipping():
    """
    A key `requests` cannot put in a header raises from INSIDE requests, before
    any socket -- where _api_get's broad except would file it as
    MonitorUnreachable, and the live test skips on that. A typo'd key silently
    switching the guard off is the one outcome this module exists to prevent.
    """
    rec = Recorder()
    hs.requests = rec
    try:
        for bad in ("", " leading", "trailing\n", "mid\rbreak", "smart’quote"):
            with pytest.raises(hs.MonitorRejected):
                hs._api_get(hs.API_URL, bad)
    finally:
        del hs.requests
    assert rec.calls == [], "classification must precede the request"


def test_an_ordinary_key_is_not_rejected():
    """The other half: without this, a _reject_unusable_key that refused
    everything would satisfy the test above."""
    hs._reject_unusable_key("hc_AbC123-xyz_456")


# ---------------------------------------------------------------------------
# skip semantics, and the live check.
# ---------------------------------------------------------------------------

def test_S1_the_skip_cannot_pass_for_a_clean_run():
    """
    NEVER SKIPPED. "Clean" and "unverified" must be different objects, not the
    same object formatted differently -- the README's own philosophy about a
    skipped check and a passing one not looking alike, made machine-checkable.
    """
    result = hs.audit({"HEALTHCHECK_URL_AM": AM_URL, "HEALTHCHECK_URL_PM": PM_URL})
    assert result["verified"] is False
    assert result["violations"] == []
    assert "HEALTHCHECK_API_KEY" in (result["reason"] or "")


def test_S3_transport_failures_and_rejections_are_different_kinds():
    """
    5xx and a dropped connection are a third party we do not control, like
    CME's FTP -- skip. A key that is present and refused is OUR misconfiguration
    -- fail.
    """
    class Boom(Recorder):
        def __init__(self, code=None, exc=None):
            Recorder.__init__(self)
            self.code, self.exc = code, exc

        def get(self, url, **kw):
            if self.exc:
                raise self.exc
            return type("R", (), {"status_code": self.code})()

    for code, expected in ((503, hs.MonitorUnreachable), (500, hs.MonitorUnreachable),
                           (401, hs.MonitorRejected), (403, hs.MonitorRejected),
                           (404, hs.MonitorRejected)):
        hs.requests = Boom(code=code)
        try:
            with pytest.raises(expected):
                hs.fetch_checks("k")
        finally:
            del hs.requests

    hs.requests = Boom(exc=OSError("connection reset"))
    try:
        with pytest.raises(hs.MonitorUnreachable):
            hs.fetch_checks("k")
    finally:
        del hs.requests


@pytest.mark.skipif(
    not os.getenv("HEALTHCHECK_API_KEY"),
    reason="HEALTHCHECK_API_KEY not in .env -- the LIVE Healthchecks.io cron is "
           "NOT verified by this run. Add the read-write project API key "
           "(healthchecks.io > project Settings > API Access) to .env; see "
           "scripts/README-schedule.md step 4.")
def test_the_live_cron_matches_what_this_repo_declares():
    """
    The one test that reaches the network. Everything above proves it can fail;
    this is the only one that can tell you it HAS.
    """
    try:
        result = hs.audit()
    except hs.MonitorUnreachable as exc:
        pytest.skip("healthchecks.io unreachable: %s" % exc)
    assert result["verified"] is True
    assert result["violations"] == [], "\n  - " + "\n  - ".join(result["violations"])
