"""
A day that contributed nothing must still be SAID.

WHY THIS IS A TEST. On Monday 2026-09-21 the dashboard's newest index date was
Friday 09-18 and the weekend was simply not on the page. Nothing had failed:
the 07:43 run exited 0, ingested 475 rows across 84 locations and pushed clean,
and AMS had published nothing dated 09-19..09-21 yet. But an absent Saturday
and a Saturday whose reports have not landed both render as nothing at all, so
there was no way to tell from the page whether the index was complete or still
filling. That is this repo's catalogued failure mode -- something failing to
appear, with no error -- in the display layer.

span_contributions() exists to make the zero explicit, which means the thing
worth testing is not that it returns the right head counts. It is that:

  * a day with no sales is RETURNED, with head 0, rather than omitted;
  * "nothing has been reported" and "nothing could have been reported yet"
    come back as different statuses;
  * the Saturday base rate is measured on the Saturdays BEFORE the one being
    described, not including it -- otherwise a Monday-morning Saturday whose
    reports are still arriving votes itself "normal";
  * the per-day heads still add up to the index date's own published total.

Each of the first three is paired below with the opposite input, because a
check that cannot fail is worse than no check: this repo has shipped three of
those, one of which printed the same variable under both labels.
"""
import sqlite3

import pytest

import composition

SCHEMA = """
CREATE TABLE mars_sales (
    report_date TEXT, raw_date TEXT, location TEXT, head_count INTEGER
)
"""


@pytest.fixture(autouse=True)
def _sqlite_placeholders(monkeypatch):
    """These fixtures are sqlite; db.placeholders() must not emit %s."""
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)


def make_conn(rows=()):
    """
    A mars_sales holding exactly `rows` -- (report_date, raw_date, location,
    head). report_date is the CME-merged date the row is stored under, the way
    recompute_fci_daily() stores it; raw_date is the true sale day.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(SCHEMA)
    conn.executemany("INSERT INTO mars_sales VALUES (?,?,?,?)", list(rows))
    conn.commit()
    return conn


def history_back_to(iso, n_weeks=20):
    """
    Filler rows so MIN(raw_date) reaches far enough back for the base rate to
    be computable. Weekdays only, so they cannot be mistaken for Saturday
    sales.
    """
    from datetime import date, timedelta
    d = date.fromisoformat(iso)
    out = []
    for i in range(n_weeks):
        w = d - timedelta(days=7 * i)
        w -= timedelta(days=(w.weekday() - 2) % 7)          # the Wednesday
        out.append((w.isoformat(), w.isoformat(), "Filler Barn", 100))
    return out


# 2026-09-19 Sat, 09-20 Sun, 09-21 Mon -- the weekend the user asked about.
MONDAY = "2026-09-21"
SATURDAY = "2026-09-19"


def test_a_day_with_no_sales_is_returned_rather_than_omitted():
    """The bug itself: the empty days must appear, with an explicit zero."""
    conn = make_conn(history_back_to("2026-09-16"))
    got = composition.span_contributions(conn, MONDAY, as_of="2026-09-22")
    dates = [d["date"] for d in got["days"]]
    assert dates == [SATURDAY, "2026-09-20", MONDAY], (
        f"the merged span came back as {dates}; a day that contributed nothing "
        f"was dropped, which is exactly the defect this function exists to fix")
    assert all(d["head"] == 0 for d in got["days"])
    assert [d["status"] for d in got["days"]] == ["none", "none", "none"]


def test_that_check_can_fail_a_day_with_sales_reads_differently():
    """
    Guard the guard. If every day came back "none" regardless of the data, the
    test above would pass on a function that had stopped reading mars_sales.
    """
    conn = make_conn(history_back_to("2026-09-16") + [
        (MONDAY, SATURDAY, "Ericson Livestock", 480),
        (MONDAY, MONDAY, "Joplin Regional", 1200),
        (MONDAY, MONDAY, "Bassett Livestock", 900),
    ])
    got = composition.span_contributions(conn, MONDAY, as_of="2026-09-22")
    by_date = {d["date"]: d for d in got["days"]}
    assert by_date[SATURDAY]["status"] == "reported"
    assert by_date[SATURDAY]["head"] == 480
    assert by_date[SATURDAY]["barns"] == 1
    assert by_date[MONDAY]["head"] == 2100 and by_date[MONDAY]["barns"] == 2
    assert by_date["2026-09-20"]["status"] == "none"    # Sunday, still empty


def test_today_is_pending_not_none():
    """
    A sale reaches AMS the following day at the earliest, so the current day's
    silence is not evidence of anything. Calling it "none" on a Monday morning
    is the specific wrong answer this distinction prevents.
    """
    conn = make_conn(history_back_to("2026-09-16"))
    got = composition.span_contributions(conn, MONDAY, as_of=MONDAY)
    by_date = {d["date"]: d["status"] for d in got["days"]}
    assert by_date[MONDAY] == "pending"
    assert by_date[SATURDAY] == "none", (
        "Saturday has had a full reporting day by Monday; calling it pending "
        "too would make the distinction meaningless")


def test_that_check_can_fail_pending_resolves_once_the_day_is_past():
    """The same Monday, read a day later, must stop claiming to be pending."""
    conn = make_conn(history_back_to("2026-09-16"))
    got = composition.span_contributions(conn, MONDAY, as_of="2026-09-22")
    assert [d["status"] for d in got["days"]] == ["none", "none", "none"]

    partial = composition.span_contributions(
        make_conn(history_back_to("2026-09-16")
                  + [(MONDAY, MONDAY, "Joplin Regional", 300)]),
        MONDAY, as_of=MONDAY)
    assert partial["days"][-1]["status"] == "partial", (
        "a day that is too early to be complete but already has rows is "
        "neither 'pending' nor settled -- saying 'none' there would be a lie "
        "about data that is visibly present")


def test_the_base_rate_excludes_the_saturday_it_describes():
    """
    The Saturday being reported on must not vote on whether it is normal. On a
    Monday morning its own reports may still be arriving, so counting it would
    bias the rate toward "empty" exactly when the reader is relying on it.
    """
    from datetime import date, timedelta
    rows = list(history_back_to("2026-09-16"))
    # Sales on the described Saturday, and on two of the ten before it.
    rows.append((MONDAY, SATURDAY, "Ericson Livestock", 480))
    for back in (1, 3):
        s = date.fromisoformat(SATURDAY) - timedelta(days=7 * back)
        mon = s + timedelta(days=2)
        rows.append((mon.isoformat(), s.isoformat(), "Ericson Livestock", 500))

    br = composition.span_contributions(
        make_conn(rows), MONDAY, as_of="2026-09-22")["saturday_base_rate"]
    assert br["latest"] == "2026-09-12", (
        f"the base rate reaches up to {br['latest']}; it must stop before "
        f"{SATURDAY}, the Saturday it is being quoted to explain")
    assert br["sampled"] == 10
    assert br["with_sales"] == 2 and br["empty"] == 8


def test_the_base_rate_is_withheld_when_history_is_too_short():
    """
    Three Saturdays is not a rate. Returning one anyway would put a confident
    "6 of 10" on the page computed from almost nothing.
    """
    conn = make_conn([("2026-09-16", "2026-09-16", "Filler Barn", 100)])
    got = composition.span_contributions(conn, MONDAY, as_of="2026-09-22")
    assert got["saturday_base_rate"] is None


def test_only_monday_merges_a_weekend():
    """Rule 10203.A.1 folds Sat and Sun into Monday and nothing else."""
    assert composition.merged_span_dates("2026-09-18") == [
        __import__("datetime").date(2026, 9, 18)]
    assert len(composition.merged_span_dates(MONDAY)) == 3
    # Friday's successor is Monday: Saturday gets no index date of its own.
    assert composition.next_index_date("2026-09-18") == MONDAY
    assert composition.next_index_date(MONDAY) == "2026-09-22"


def test_span_head_sums_to_the_published_same_day_head():
    """
    The data, not the code. If the per-day split does not add back up to the
    index date's own total, the line on the page contradicts the table above
    it -- and a reader trusting the breakdown would be reading a different
    sample from the one the index was built on.
    """
    from pathlib import Path
    db_path = Path(__file__).resolve().parent.parent / "data" / "mars_history.db"
    if not db_path.exists():
        pytest.skip("no local database")
    conn = sqlite3.connect(db_path)
    try:
        mondays = conn.execute(
            "SELECT report_date, same_day_head FROM fci_daily "
            "WHERE same_day_head > 0 ORDER BY report_date DESC LIMIT 40"
        ).fetchall()
    except sqlite3.OperationalError:
        pytest.skip("fci_daily absent")

    checked = 0
    try:
        for report_date, same_day_head in mondays:
            got = composition.span_contributions(conn, report_date,
                                                 as_of="2999-01-01")
            total = (sum(d["head"] for d in got["days"])
                     + sum(o["head"] for o in got["outside_span"]))
            assert total == same_day_head, (
                f"{report_date}: the day-by-day split totals {total:,} head "
                f"but fci_daily.same_day_head is {same_day_head:,}. The line "
                f"on the page would disagree with the table above it.")
            checked += 1
    finally:
        conn.close()
    assert checked >= 20, f"only {checked} dates checked; too few to mean much"


# ===========================================================================
# THE LINE ITSELF, NOT JUST THE DATA BEHIND IT
#
# Everything above tests span_contributions(). That function was never the
# problem: it returned the empty days correctly from the first draft. What
# shipped wrong was the SENTENCE app.py builds out of it, and five separate
# defects lived there where no test could see them:
#
#   1. the lead was chosen off whether any day was unsettled and never off
#      whether the bucket held any head, so a date with nothing in it read
#      "complete as far as AMS has reported" -- the reassuring answer, during
#      an outage, forever, because it never escalated with age either;
#   2. the block only rendered when the span was merged or something was still
#      filling, so once a stalled date slipped into the past and was not a
#      Monday the note vanished entirely -- an absence and a pending fetch
#      looking identical, which is the defect the note exists to fix, rebuilt
#      inside the fix;
#   3. outside_span rows were appended to the day list AFTER the day count had
#      been fixed, so 29 of the weekday index dates between 2026-06-01 and
#      09-18 printed "It covers one calendar day:" above two dated items;
#   4. the "worth a second look" branch fired whenever Saturdays had sold more
#      than half the time, which is most of the year -- 6 of the 18 empty
#      Saturdays in 2026, 20 of the 60 in all of mars_sales.
#
# So these tests run the REAL render block: its own source lines are sliced out
# of app.py between its two banner comments and exec'd against a fixture
# database. A fix that exists only in the test's idea of the wording cannot
# pass. Both copies of app.py are run, which makes this a drift check on the
# block as well -- test_no_drift.py cannot compare app.py, because the two are
# deliberately different files.
# ===========================================================================
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPS = [REPO / "app.py",
        REPO.parent / "livestock-portal" / "apps" / "cme_feeder_cattle" / "app.py"]

_BANNER_START = "── What the merged day is made of"
_BANNER_END = "── Index Composition"


def _block(app_path):
    """The render block's own source, dedented so it can be exec'd."""
    import textwrap
    src = app_path.read_text(encoding="utf-8").splitlines()
    try:
        i = next(n for n, l in enumerate(src) if _BANNER_START in l)
        j = next(n for n, l in enumerate(src) if _BANNER_END in l and n > i)
    except StopIteration:
        pytest.fail(
            f"{app_path} no longer has the weekend-line block between its "
            f"'{_BANNER_START}' and '{_BANNER_END}' banners, so these tests are "
            f"silently checking nothing. Re-point them or delete them; do not "
            f"leave them passing vacuously.")
    return textwrap.dedent("\n".join(src[i:j]))


def render(app_path, conn, have_iso, today_iso):
    """
    The note as the page would print it, tags stripped. Returns "" when the
    block renders nothing at all -- which is itself a failure mode, so it must
    be visible rather than raising.

    `have_iso` is the newest index date the page holds sales for, which is
    what the block anchors on. It is NOT head_date: head_date runs on CME's
    publication clock and our own estimates routinely sit past it.
    """
    import html as _html
    from datetime import datetime as _dt

    import pandas as pd

    import composition

    class _FakeDatetime(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt.fromisoformat(today_iso + "T09:00:00")

    captured = []

    class _St:
        @staticmethod
        def markdown(body, **kw):
            captured.append(body)

    g = {
        # exactly the names app.py has in scope at this point in the file
        "pd": pd, "datetime": _FakeDatetime, "st": _St,
        "timedelta": __import__("datetime").timedelta,
        "next_index_date": composition.next_index_date,
        "_load_span": lambda iso, as_of: composition.span_contributions(
            conn, iso, as_of=as_of),
        "head_date": pd.Timestamp(have_iso),
        # one row is enough: the block only asks fci_df for the newest date
        # carrying same_day_head, and a fixture that offered more would be
        # asserting pandas rather than the note.
        "fci_df": pd.DataFrame({"date": [pd.Timestamp(have_iso)],
                                "same_day_head": [1234]}),
        "BORDER": "#e6eaee", "MUTED": "#6b7280",
    }
    exec(compile(_block(app_path), str(app_path), "exec"), g)
    text = " ".join(captured)
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text).strip()


@pytest.fixture(params=APPS, ids=lambda p: p.parent.name)
def app(request):
    if not request.param.is_file():
        pytest.skip(f"{request.param} not checked out")
    return request.param


# 2026-09-18 Fri was the last printed index date during the real incident.
FRIDAY = "2026-09-18"


def test_an_empty_span_is_reported_as_an_absence_not_as_complete(app):
    """
    Defect 1, the one that matters most. A Monday holding nothing at all read
    "Mon 09/21 is complete as far as AMS has reported", because the lead was
    picked off settled-ness and nothing ever asked whether there was any head.
    The page's staleness caption does not cover this either: it keys off the
    last pipeline run, and the real 2026-09-21 run exited 0 on 475 rows.
    """
    conn = make_conn(history_back_to("2026-09-16"))
    got = render(app, conn, FRIDAY, "2026-09-22")
    assert got, "the note rendered nothing during an outage"
    assert "complete as far as AMS has reported" not in got, (
        f"a span with zero head still reads as finished:\n{got}")
    assert "no sales on record" in got, (
        f"the absence is not stated as an absence:\n{got}")


def test_that_check_can_fail_a_date_with_head_is_not_called_an_absence(app):
    """
    Guard the guard. Hard-wiring the absence wording would pass the test above
    while telling the reader an ordinary Monday had nothing on it. Same
    fixture, same date, one Saturday sale added -- the sentence has to change.
    """
    conn = make_conn(history_back_to("2026-09-16") + [
        ("2026-09-21", "2026-09-19", "Ericson Livestock", 480)])
    got = render(app, conn, FRIDAY, "2026-09-22")
    assert "no sales on record" not in got, (
        f"a Monday carrying 480 head still reads as an absence, so the test "
        f"above would pass on a block that had stopped looking at the data "
        f"entirely:\n{got}")
    assert "480 head" in got


def test_an_empty_span_escalates_with_age(app):
    """
    Saying "no sales on record" once is a status line. Saying it identically
    for a fortnight is the same silence in different words, which is how the
    original got to be four days stale without the page changing a character.
    """
    conn = make_conn(history_back_to("2026-09-16"))
    day_one = render(app, conn, FRIDAY, "2026-09-22")
    day_four = render(app, conn, FRIDAY, "2026-09-25")
    assert day_one != day_four, (
        "four days into an outage the note is byte-identical to one day in")
    assert "gap in the data" in day_four, (
        f"an empty span four business days old is an anomaly, not a status "
        f"line:\n{day_four}")
    assert "gap in the data" not in day_one


def test_the_base_rate_reassurance_is_suppressed_while_the_span_is_empty(app):
    """
    "An empty Saturday is the usual case rather than a gap" is true, and
    exactly the wrong thing to print when the Sunday and the Monday are empty
    too. It softened the one state that needed escalating.
    """
    conn = make_conn(history_back_to("2026-09-16"))
    got = render(app, conn, FRIDAY, "2026-09-22")
    assert "usual case" not in got and "routine" not in got, (
        f"the note reassures about Saturdays while nothing at all is on "
        f"record:\n{got}")


def test_a_non_monday_stall_still_renders(app):
    """
    Defect 2. The guard was merged-or-unsettled, so a Tuesday index date that
    had gone stale rendered NOTHING from the second day on -- the absence and
    the pending fetch made identical again, inside the fix for that very bug.
    """
    rows = history_back_to("2026-09-14") + [
        ("2026-09-15", "2026-09-15", "Joplin Regional", 900)]
    conn = make_conn(rows)
    for today in ("2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21"):
        got = render(app, conn, "2026-09-15", today)
        assert got, (
            f"read on {today} with the index stuck at 2026-09-15, the note "
            f"renders nothing at all -- the reader cannot tell a stalled "
            f"pipeline from a quiet market")


def test_a_stall_names_the_later_missing_dates_too(app):
    """
    The router described only next_index_date(head), so a three-day stall said
    nothing whatever about the second and third missing days.
    """
    conn = make_conn(history_back_to("2026-09-14") + [
        ("2026-09-15", "2026-09-15", "Joplin Regional", 900)])
    got = render(app, conn, "2026-09-15", "2026-09-18")
    for missing in ("Thu 09/17", "Fri 09/18"):
        assert missing in got, (
            f"{missing} is due and unprinted but the note never mentions "
            f"it:\n{got}")


def _claimed_and_listed(text):
    """(days the lead claims to cover, dated items actually listed)."""
    words = {"one": 1, "two": 2, "three": 3}
    m = re.search(r"merges (\d+) calendar days: (.*?)(?:\. |\.$)", text, re.S)
    if m:
        return int(m.group(1)), len(m.group(2).split(" · "))
    m = re.search(r"It covers (\w+) calendar day: (.*?)(?:\. |\.$)", text, re.S)
    if m:
        return words[m.group(1)], len(m.group(2).split(" · "))
    return None, None


def test_the_day_count_matches_the_items_listed_beside_it(app):
    """
    Defects 3 and 4, one root cause: outside_span rows joined the day list
    after the count had been taken. El Reno's Tuesday sale buckets to
    Wednesday, so "It covers one calendar day:" was printed above two dated
    contributions on every Wednesday and Thursday carrying one -- 29 index
    dates between 2026-06-01 and 2026-09-18, one of them 41% of the date's
    head.
    """
    conn = make_conn(history_back_to("2026-09-14") + [
        # El Reno reports Tuesday; CME buckets it on the Wednesday.
        ("2026-09-15", "2026-09-15", "El Reno", 809),
        ("2026-09-16", "2026-09-16", "Joplin Regional", 1169),
    ])
    got = render(app, conn, "2026-09-15", "2026-09-16")
    claimed, listed = _claimed_and_listed(got)
    assert claimed is not None, f"could not parse the day count from:\n{got}"
    assert claimed == listed, (
        f"the lead claims {claimed} calendar day(s) and then lists {listed} "
        f"dated contributions:\n{got}")
    assert "809" in got, (
        "the bucketed-in El Reno head vanished from the note entirely; it must "
        "be stated, just not counted as one of the days")


def test_that_check_can_fail_the_parser_catches_a_real_mismatch():
    """
    _claimed_and_listed is the whole of the test above, so it has to be shown
    to notice. Fed the exact string the old block produced on 2026-09-16, it
    must report the contradiction rather than shrugging.
    """
    old = ("Wed 09/16 is still filling. It covers one calendar day: Wed 09/16 "
           "1,169 head so far · Tue 09/15 809 head, bucketed into this "
           "date. A sale reaches AMS the following day at the earliest.")
    assert _claimed_and_listed(old) == (1, 2)
    fixed = ("Wed 09/16 is still filling. It covers one calendar day: Wed 09/16 "
             "1,169 head so far. Plus Tue 09/15’s 809 head, which CME "
             "buckets into this date.")
    assert _claimed_and_listed(fixed) == (1, 1)


def _monday_with_a_busy_saturday_history():
    """
    2026-02-09's real shape: the Saturday itself empty, but 8 of the previous
    10 sold. That is an ordinary late-winter week -- Ericson, the only barn
    that ever holds a Saturday sale, simply did not hold one.
    """
    from datetime import date, timedelta
    rows = list(history_back_to("2026-02-04", n_weeks=30))
    sat = date(2026, 2, 7)
    for back in range(1, 11):
        if back in (3, 7):                       # two empty, eight sold
            continue
        s = sat - timedelta(days=7 * back)
        rows.append(((s + timedelta(days=2)).isoformat(), s.isoformat(),
                     "Ericson Livestock", 500))
    rows.append(("2026-02-09", "2026-02-09", "Joplin Regional", 7873))
    return rows


def test_a_busy_saturday_history_does_not_raise_an_alarm(app):
    """
    Defect 5. The third branch fired whenever Saturdays had sold more often
    than half the time, so an 8-of-10 reading -- a perfectly normal winter --
    printed "worth a second look" beside an empty Saturday. Replayed over the
    whole of mars_sales it fired on 20 of the 60 empty Saturdays, 7.7 a year,
    and on 6 of the 18 in 2026 (02/09, 03/23, 04/06, 04/20, 04/27, 05/11). A
    weekly false alarm is a check nobody reads, which is the stated reason the
    base rate exists at all.
    """
    conn = make_conn(_monday_with_a_busy_saturday_history())
    got = render(app, conn, "2026-02-06", "2026-02-09")
    assert got, "nothing rendered for an ordinary Monday"
    assert "8 of the previous 10" in got, (
        f"the base rate is not being stated at all, so this test would pass "
        f"on a block that had simply stopped mentioning Saturdays:\n{got}")
    for alarm in ("worth a second look", "busier than this lately"):
        assert alarm not in got, (
            f"an 8-of-10 Saturday history still raises an alarm:\n{got}")


def test_a_quiet_saturday_still_reads_as_routine(app):
    """
    The other side of it. Dropping the alarm must not have dropped the
    reassurance: an empty Saturday in a run of empty Saturdays is the single
    most common state this line renders, and it has to read as normal.
    """
    from datetime import date, timedelta
    rows = list(history_back_to("2026-09-16", n_weeks=30))
    sat = date(2026, 9, 19)
    for back in (1, 5, 7, 9):                    # 4 of 10 sold, as in Sept
        s = sat - timedelta(days=7 * back)
        rows.append(((s + timedelta(days=2)).isoformat(), s.isoformat(),
                     "Ericson Livestock", 480))
    rows.append(("2026-09-21", "2026-09-21", "Joplin Regional", 5321))
    got = render(app, make_conn(rows), FRIDAY, "2026-09-21")
    assert "usual case rather than a gap" in got, (
        f"a 4-of-10 Saturday history no longer reads as routine:\n{got}")
    assert "Mon 09/21 is still filling" in got, (
        f"a Monday with head on it should read as filling, not stalled:\n{got}")


def test_total_head_is_the_whole_bucket():
    """
    The lead is chosen off total_head, so it lives in composition.py rather
    than in each app.py -- the two copies must not be able to disagree about
    the one number that decides "normal day" from "outage". It has to include
    the bucketed-in rows: a Wednesday whose only head arrived from El Reno's
    Tuesday is NOT empty.
    """
    conn = make_conn(history_back_to("2026-09-14") + [
        ("2026-09-15", "2026-09-15", "El Reno", 809)])
    got = composition.span_contributions(conn, "2026-09-16", as_of="2026-09-17")
    assert sum(d["head"] for d in got["days"]) == 0
    assert got["outside_span"] and got["outside_span"][0]["head"] == 809
    assert got["total_head"] == 809, (
        "total_head ignored the rows bucketed in from outside the span, so a "
        "date holding 809 head would be reported as holding nothing")

    empty = composition.span_contributions(
        make_conn(history_back_to("2026-09-14")), "2026-09-16",
        as_of="2026-09-17")
    assert empty["total_head"] == 0
