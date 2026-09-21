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
