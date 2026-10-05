"""
Does the daily email actually put the warnings in front of a human?

WHY THIS FILE EXISTS. On 2026-10-05 every check that mattered worked. The
parser knew it had dropped 2,162 head of Texas Direct. The barn report ran. CIH
and Compass had both published 337.76 against our 339.55. And the estimate went
to clients anyway, because the parser's warning went into
/opt/cme-feeder-cattle-index/logs/update_*.log on a droplet that nothing reads
and that deletes its own logs after 30 days, the barn report was not in the
email, and nothing compared us to a peer on the date being published.

A check nobody receives is not a check. These tests are about DELIVERY, not
detection -- every one of them asserts that a fact already known to the pipeline
reaches the body of the message Ross opens.

The replay at the bottom is the point of the whole file: feed it the morning as
it actually happened and require that the email would have stopped it.
"""
import re

import pytest

import notify_email as ne


def _text(body):
    """HTML stripped to readable text, so assertions are about what a human sees."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _base(**over):
    """A minimal, healthy gather() result. Overridden per test."""
    d = {
        "index_date": "2026-10-02", "value": 337.7363, "head": 20201, "locs": 283,
        "dod": 0.90, "sd_price": 333.73, "sd_head": 7050, "sd_wt": 816.0,
        "window": [("2026-10-02", 7050, 816.0, 333.73)],
        "cme_date": "2026-10-01", "cme_value": 336.84, "cme_head": 21854,
        "scored_call": 337.15, "scored_head": 21854, "peers": [],
        "live_peers": [("CIH", 337.76)], "prior_call": None, "barn_lines": ["Barn report -- 2 of 2 expected barns reported"],
        "ingest_warnings": [],
    }
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# The peer check: the line that would have caught 2026-10-05.
# ---------------------------------------------------------------------------

def test_a_peer_gap_inside_a_nickel_is_reported_without_alarm():
    t = _text(ne.build(_base(value=337.7363, live_peers=[("CIH", 337.76)]))[1])
    assert "Against the desks" in t
    assert "CIH" in t and "337.76" in t
    assert "from a published peer" not in t, \
        "two cents is ordinary agreement and must not cry wolf"


def test_a_peer_gap_past_a_nickel_raises_the_alarm():
    """
    The 2026-10-05 magnitude. If this ever stops firing, the one check that
    actually caught that morning -- a human reading CIH -- is all that is left.
    """
    t = _text(ne.build(_base(value=339.5514,
                             live_peers=[("CIH", 337.76), ("COMPASS", 337.76)]))[1])
    assert "from a published peer" in t
    assert "1.79" in t
    assert "check the ingest before sending this out" in t


def test_the_alarm_is_symmetric():
    """Being well BELOW the desks is just as wrong as being above."""
    t = _text(ne.build(_base(value=335.90, live_peers=[("CIH", 337.76)]))[1])
    assert "from a published peer" in t, "a negative gap must alarm too"


def test_no_recorded_peer_says_so_rather_than_staying_silent():
    """
    Capture is still manual -- CIH publishes on X and Compass by email, and
    add_peer_estimate.py is run by hand. The failure mode is forgetting, and a
    silently absent check reads exactly like a passing one. So the email names
    the gap and hands over the command.
    """
    t = _text(ne.build(_base(live_peers=[]))[1])
    assert "No CIH or Compass estimate recorded" in t
    assert "add_peer_estimate.py" in t
    assert "from a published peer" not in t, "absence is not a discrepancy"


# ---------------------------------------------------------------------------
# Unlabelled rows: the warning that was produced and never delivered.
# ---------------------------------------------------------------------------

TX = {"source": "TX DIRECT", "report_date": "2026-10-02", "head": 2162,
      "avg_weight": 820.0, "avg_price": 322.79, "muscle_grade": "1-2",
      "weight_low": 800}


def test_an_unlabelled_row_is_named_in_the_body():
    t = _text(ne.build(_base(ingest_warnings=[TX]))[1])
    assert "NOT ingested" in t
    assert "2,162 head" in t, "name the row -- a count sends nobody to the PDF"
    assert "820 lb" in t and "322.79" in t
    assert "TX DIRECT" in t


def test_the_wording_says_it_is_not_an_exclusion():
    """
    The distinction the whole fix rests on. A reader who thinks these are
    ordinary exclusions will ignore them, which is the state we were in.
    """
    t = _text(ne.build(_base(ingest_warnings=[TX]))[1])
    assert "not exclusions" in t.lower()
    assert "short by them" in t


def test_a_clean_run_shows_no_warning_block():
    t = _text(ne.build(_base(ingest_warnings=[]))[1])
    assert "NOT ingested" not in t, \
        "a block that renders every day is a block nobody reads"


def test_a_missing_sidecar_is_not_an_error(tmp_path, monkeypatch):
    """
    A fresh checkout and a clean run look the same: no file. That must read as
    "nothing to report", not as a crash in the one thing reporting crashes.
    """
    monkeypatch.chdir(tmp_path)
    assert ne._ingest_warnings() == []


# ---------------------------------------------------------------------------
# The barn report, carried rather than re-derived.
# ---------------------------------------------------------------------------

def test_the_barn_report_reaches_the_body():
    t = _text(ne.build(_base(barn_lines=[
        "Barn report -- index date 2026-10-02 (Fri): 1 of 2 expected barns reported",
        "  no qualifying cattle: Belen NM  ~8,002 lb  (~1% of a typical Friday)"]))[1])
    assert "expected barns reported" in t
    assert "Belen NM" in t


def test_the_email_does_not_rebuild_the_roster_itself():
    """
    gather() calls barn_report.report_lines and carries its strings verbatim.
    If the email ever derives its own roster, it and the run log can disagree
    about which barns are out -- and then neither can be trusted.
    """
    import inspect
    src = inspect.getsource(ne.gather)
    assert "barn_report.report_lines(conn)" in src
    assert "MIN_PRESENT" not in src and "OCCURRENCES" not in src, \
        "the email must not reimplement the roster rule"


# ---------------------------------------------------------------------------
# The replay.
# ---------------------------------------------------------------------------

def test_the_email_would_have_stopped_2026_10_05():
    """
    The morning as it actually happened: TX Direct 2,162 head short, ours
    339.5514, both desks 337.76. TWO independent warnings must appear -- the
    dropped row and the peer gap -- because either alone could be the one that
    is broken next time.
    """
    t = _text(ne.build(_base(value=339.5514, ingest_warnings=[TX],
                             live_peers=[("CIH", 337.76), ("COMPASS", 337.76)]),
                       slot="am")[1])
    assert "NOT ingested" in t and "2,162 head" in t
    assert "from a published peer" in t and "1.79" in t
    # and the subject still carries the (wrong) number, so nothing here hides it
    subj = ne.build(_base(value=339.5514), slot="am")[0]
    assert "339.55" in subj


# ---------------------------------------------------------------------------
# Did the number move since we last sent it?
# ---------------------------------------------------------------------------

AM_CALL = {"value": 339.5514, "head": 18039, "slot": "am",
           "at": "08:12", "date": "2026-10-05"}


def test_a_material_move_says_the_earlier_figure_is_out_of_date():
    """
    THE 2026-10-05 GAP. The 07:45 call went to clients at 339.55 and the 13:00
    run settled at 337.74. Nothing said it had moved -- the afternoon email
    simply carried a different number as though it had always been that, and
    the correction came from Ross reading CIH hours later.
    """
    t = _text(ne.build(_base(value=337.7363, head=20201,
                             prior_call=AM_CALL), slot="pm")[1])
    assert "Moved since the am call of" in t
    assert "-1.8151" in t
    assert "out of date" in t
    assert "+2,162 head" in t, "say what arrived, not just that something did"


def test_an_ordinary_settle_does_not_cry_wolf():
    """
    Measured over 44 index dates the median move is 0.0000 and only six moved a
    dime or more. A block that shouts on every pm run is a block nobody reads,
    so under a nickel it reports the move and stops.
    """
    small = dict(AM_CALL, value=337.7326, head=20201)
    t = _text(ne.build(_base(value=337.7363, head=20201,
                             prior_call=small), slot="pm")[1])
    assert "Changed since the am call" in t
    assert "out of date" not in t


def test_no_prior_call_prints_nothing():
    """The first call of the day has nothing to move from."""
    t = _text(ne.build(_base(prior_call=None))[1])
    assert "since the" not in t


def test_the_label_names_the_run_date():
    """
    An index date accumulates snapshots across several days once CME is behind,
    so "the 08:12 am call" alone cannot say which morning. The first version
    omitted the date and was ambiguous exactly when it mattered most.
    """
    t = _text(ne.build(_base(value=337.7363, head=20201,
                             prior_call=AM_CALL), slot="pm")[1])
    assert "10/5/26 08:12" in t


# ---------------------------------------------------------------------------
# Capturing the peer figure, which is the guard that catches most.
# ---------------------------------------------------------------------------

def test_a_cih_post_parses_to_the_estimate():
    """
    The real format from x.com/CIHCattleTeam. Measured against the six index
    dates that moved a dime or more between the morning call and the settle,
    the peer check would have fired on FIVE -- every one where a peer had been
    recorded. The sixth had none, and nothing fired. So capture is the binding
    constraint, not detection.
    """
    from add_peer_estimate import parse_cih_post
    got = parse_cih_post(
        "Feeder Cattle Index +$0.92\n"
        "CIH Est: $337.76; Previous: $336.84\n"
        "8,703 head dropping off (40% of index); 7,082 head traded (35%)\n"
        "#ag #cattle #feedercattle")
    assert got["value"] == 337.76
    assert got["previous"] == 336.84
    assert got["head_traded"] == 7082


def test_a_negative_day_parses_too():
    from add_peer_estimate import parse_cih_post
    got = parse_cih_post("Feeder Cattle Index -$2.13\n"
                         "CIH Est: $336.84; Previous: $338.97\n"
                         "4,849 head dropping off (21% of index); "
                         "3,087 head traded (14%)")
    assert got["value"] == 336.84 and got["head_traded"] == 3087


def test_a_post_that_is_not_the_index_tweet_returns_none():
    """
    Most of that account is not the daily index post, and the seminar advert is
    pinned to the top of the feed -- the obvious way a paste goes wrong.
    Returning None beats guessing: a wrong peer value would silence the one
    check that works, or fire it on nothing.
    """
    from add_peer_estimate import parse_cih_post
    for junk in ("Want to sharpen your approach to managing cattle margins? "
                 "CIH's Beef Margin Management Seminars",
                 "Mexican feeder cattle crossings are back",
                 "WTD slaughter: 548k head", "", None):
        assert parse_cih_post(junk) is None, junk


def test_the_parser_does_not_mistake_the_previous_for_the_estimate():
    """
    Both numbers are on the same line and the PREVIOUS is CME's published
    figure, not CIH's call. add_peer_estimate.py's own docstring warns that
    logging it would credit them with a number they copied.
    """
    from add_peer_estimate import parse_cih_post
    got = parse_cih_post("CIH Est: $337.76; Previous: $336.84")
    assert got["value"] == 337.76, "the estimate is the first figure, not the second"
