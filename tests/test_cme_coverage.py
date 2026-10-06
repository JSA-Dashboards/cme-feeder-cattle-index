"""
The slug-level check tests/test_roster_covers_cme.py says it cannot do.

That file compares NAMES and documents its own blind spot: when two AMS reports
print one city name, losing one of the pair is silent because the survivor keeps
the name reachable. It names Billings 1774/1777 as an example. On 2026-10-02 the
blind spot fired -- CME published 32 head of Billings and we held none, because
AMS runs four cattle auctions in that city and the roster carried two.

THE HEADLINE CHECK MATCHES NO NAMES. daily_shortfalls compares one number per
date that each party derived independently. That is not a stylistic choice: the
first version of cme_coverage compared per-location head by name and reported
712 head missing across five locations, and all but 32 of it was a name-matching
failure. CME truncates its location column -- 20 characters in one file era, 30
in another -- and cases names its own way.

So the tests below are in two halves. The ones about daily_shortfalls are about
CORRECTNESS. The ones about matches() are about NOISE: a miss there costs a name
in a message, never a wrong answer, which is why locate() is allowed to come
back empty on a real shortfall.
"""
import sqlite3

import pytest

import cme_coverage as cc


# ---------------------------------------------------------------------------
# The headline check: totals, no names.
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(monkeypatch):
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE fci_daily (report_date TEXT, fci_value REAL,
            same_day_head INTEGER);
        CREATE TABLE cme_ftp_daily (report_date TEXT, fci_value REAL,
            same_day_head INTEGER);
        CREATE TABLE mars_sales (report_date TEXT, location TEXT,
            head_count INTEGER);
        CREATE TABLE cme_ftp_locations (report_date TEXT, location TEXT,
            head_count INTEGER);
    """)
    return c


def _day(c, d, ours, cme):
    c.execute("INSERT INTO fci_daily VALUES (?,340.0,?)", (d, ours))
    c.execute("INSERT INTO cme_ftp_daily VALUES (?,340.0,?)", (d, cme))


def test_a_shortfall_is_reported(conn):
    """2026-10-02 as it happened: CME 7,082, us 7,050."""
    _day(conn, "2026-10-02", 7050, 7082)
    got = cc.daily_shortfalls(conn)
    assert len(got) == 1
    assert got[0]["short"] == 32 and got[0]["date"] == "2026-10-02"


def test_holding_MORE_than_cme_is_not_a_shortfall(conn):
    """
    The direction that must stay quiet. CME's file is a snapshot of its own
    print time and we keep ingesting, so a late or preliminary report
    legitimately puts us ahead -- Oklahoma City does it most Mondays. Flagging
    that would fire every afternoon and the check would be ignored by Friday.
    """
    _day(conn, "2026-10-05", 19606, 18438)
    assert cc.daily_shortfalls(conn) == []


def test_an_exact_match_is_silent(conn):
    _day(conn, "2026-09-30", 3678, 3678)
    assert cc.daily_shortfalls(conn) == []


def test_a_single_head_still_counts(conn):
    """
    No threshold. Two of the three real shortfalls on record are one head
    (2026-08-31 Tulsa, 2026-09-22 McAlester), and a cutoff that hid them would
    have been chosen to hide them rather than measured.
    """
    _day(conn, "2026-08-31", 3551, 3552)
    assert cc.daily_shortfalls(conn)[0]["short"] == 1


def test_dates_before_the_direct_trade_ingest_are_out_of_scope(conn):
    """
    Before 2026-08-28 we held no direct or video rows at all and CME did, so an
    unscoped scan returns 28,889 rows and 13 million head of history that is
    explained and unfixable. Scoping is what keeps the output readable.
    """
    _day(conn, "2026-08-14", 0, 5233)
    assert cc.daily_shortfalls(conn) == []
    assert cc.daily_shortfalls(conn, since="2015-01-01")[0]["short"] == 5233


def test_a_date_only_one_side_has_is_not_compared(conn):
    """An inner join on purpose: a date CME has not printed is not a shortfall."""
    conn.execute("INSERT INTO fci_daily VALUES ('2026-10-05',340.0,19606)")
    assert cc.daily_shortfalls(conn) == []


# ---------------------------------------------------------------------------
# Attribution: best effort, and allowed to fail quietly.
# ---------------------------------------------------------------------------

def test_locate_names_the_barn(conn):
    conn.execute("INSERT INTO cme_ftp_locations VALUES ('2026-10-02','Billings',32)")
    conn.execute("INSERT INTO cme_ftp_locations VALUES ('2026-10-02','Carthage',1501)")
    conn.execute("INSERT INTO mars_sales VALUES ('2026-10-02','Carthage',1501)")
    got = cc.locate(conn, "2026-10-02")
    assert [r["cme_location"] for r in got] == ["Billings"]
    assert got[0]["short"] == 32


# ---------------------------------------------------------------------------
# The name matcher, which exists only so attribution reads well.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cme,ours", [
    ("Mcalester", "McAlester"),                       # case only
    ("Mid Missouri Stockya", "Mid Missouri Stockyards Cattle Auction - Phillipsburg, MO"),
    ("North Platte Stock", "North Platte"),
    ("Colorado Direct", "CO DIRECT"),
    ("Oklahoma Direct", "OK DIRECT"),
    ("Superior Video (Nc)", "SUPERIOR VIDEO (North Central)"),
    ("Wyoming-Nebraska D", "WY DIRECT"),              # truncated before "Direct"
    ("Billings", "Billings"),
])
def test_these_name_the_same_market(cme, ours):
    assert cc.matches(cme, ours), "%r should match %r" % (cme, ours)


@pytest.mark.parametrize("cme,ours", [
    ("Tulsa", "Tulia"),            # two real barns, four letters apart
    ("Billings", "Belen"),
    ("Texas Direct", "OK DIRECT"),
    ("Colorado Direct", "Colorado City"),
    ("Worthing", "Woodward"),
])
def test_these_are_different_markets(cme, ours):
    """
    The expensive direction. A false match hides a real shortfall by crediting
    one barn's head to another, and Tulsa/Tulia are four letters apart and both
    on the roster.
    """
    assert not cc.matches(cme, ours), "%r must not match %r" % (cme, ours)


def test_the_truncation_rule_needs_real_length():
    """
    Prefix matching is how the truncated names are caught, so it has to refuse
    to work on stubs -- otherwise every short name matches every other.
    """
    assert not cc.matches("Ca", "Carthage")
    assert not cc.matches("A", "Apache")
