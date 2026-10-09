"""
The Sale Barn Basis chart draws both ends of the ranking and nothing in
between, which is the right default -- twenty bars is already a tall chart --
and the wrong answer for a reader who follows a particular set of barns. The
ones it hides are mid-pack by construction, and mid-pack is where most barns
live: measured 2026-10-09 at 700-749 lb over three months, Carthage MO was the
LARGEST barn in the bracket by head and ranked 36th of 54, so it had never once
appeared on the chart it is a member of.

`pinned_rows` adds the picked barns to the two ends. `keep_selected` is what
stops a pick surviving into a roster it is no longer in, which st.multiselect
raises on rather than quietly resetting the way the old single selectbox did.

BOTH ARE TESTED AGAINST A KNOWN VIOLATION, not just against the happy path.
CLAUDE.md records three checks in this project that could not fail; a pin test
that only ever feeds in a barn already on the chart is a fourth, because it
passes identically against the unpinned code it was written to replace.
"""
import sys
import types

# The SAME streamlit stub test_basis_lookback.py installs, and it has to be
# here for the same reason: `load_barns` is wrapped in st.cache_data, so
# whichever test module imports barn_basis FIRST decides whether that cache is
# real for the whole session. Importing it against the real streamlit made the
# cache real, and the lookback tests' monkeypatched `_conn` was then never
# consulted -- eight of them failed in a full run and passed on their own. This
# file needs no streamlit at all; it just must not be the one to import it.
if "streamlit" not in sys.modules:
    _stub = types.ModuleType("streamlit")
    _stub.cache_data = lambda **kw: (lambda f: f)
    sys.modules["streamlit"] = _stub

import barn_basis as bb


def _rows(*specs):
    return [{"barn": b, "state": "MO", "basis": v, "rel": v - 10}
            for b, v in specs]


def _mid_pack():
    """25 barns, so the two ends hold 20 and five are invisible."""
    return _rows(*[(f"Barn{i:02d}", float(100 - i)) for i in range(25)])


def test_the_chart_really_does_hide_a_mid_pack_barn():
    """
    The premise. If this fails the rest of the file is testing nothing.
    """
    qual = _mid_pack()
    shown = {bb._label(r) for r in bb.pinned_rows(qual, [], "basis")}
    assert len(qual) == 25
    assert len(shown) == 2 * bb.CHART_ENDS
    assert "Barn12 MO" not in shown, (
        "Barn12 ranks 13th of 25 and must be off an unpinned chart")


def test_pinning_a_hidden_barn_puts_it_on_the_chart():
    qual = _mid_pack()
    shown = [bb._label(r) for r in bb.pinned_rows(qual, ["Barn12 MO"], "basis")]
    assert "Barn12 MO" in shown
    assert len(shown) == 2 * bb.CHART_ENDS + 1, \
        "a pinned barn is ADDED to the ends, it does not displace one"


def test_pinning_several_hidden_barns_shows_all_of_them():
    """The Green City / Unionville / Carthage / Kingdom City case."""
    qual = _mid_pack()
    pins = ["Barn11 MO", "Barn12 MO", "Barn13 MO", "Barn14 MO"]
    shown = [bb._label(r) for r in bb.pinned_rows(qual, pins, "basis")]
    assert all(p in shown for p in pins)
    assert len(shown) == 2 * bb.CHART_ENDS + len(pins)


def test_a_pinned_barn_already_on_the_chart_is_not_doubled():
    qual = _mid_pack()
    shown = [bb._label(r) for r in bb.pinned_rows(qual, ["Barn00 MO"], "basis")]
    assert shown.count("Barn00 MO") == 1
    assert len(shown) == 2 * bb.CHART_ENDS


def test_a_short_roster_is_not_listed_twice():
    """Fewer than 2*CHART_ENDS barns: the two slices overlap and must dedupe."""
    qual = _rows(("A", 5.0), ("B", 1.0), ("C", -3.0))
    shown = [bb._label(r) for r in bb.pinned_rows(qual, ["B MO"], "basis")]
    assert shown == ["C MO", "B MO", "A MO"]


def test_the_chart_is_ascending_so_the_best_basis_is_at_the_top():
    qual = _mid_pack()
    vals = [r["basis"] for r in bb.pinned_rows(qual, ["Barn12 MO"], "basis")]
    assert vals == sorted(vals)


def test_pinning_follows_the_active_metric():
    """
    Pins are drawn from `qual`, so they survive a toggle that reorders
    everything else. Ranking on `rel` must still show the pinned barn.
    """
    qual = _mid_pack()
    shown = [bb._label(r) for r in bb.pinned_rows(qual, ["Barn12 MO"], "rel")]
    assert "Barn12 MO" in shown


def test_keep_selected_drops_a_barn_that_left_the_roster():
    """
    The violation this guard exists for. Unionville clears the 100-head floor
    at 700-749 over six months and does not over three, so the roster really
    does lose it on an ordinary change of window.
    """
    options = ["Green City MO", "Carthage MO"]
    kept = bb.keep_selected(["Green City MO", "Unionville MO"], options)
    assert kept == ["Green City MO"], \
        "a pick absent from the options must not reach st.multiselect's default"


def test_keep_selected_returns_the_roster_order_not_the_pick_order():
    options = ["A", "B", "C"]
    assert bb.keep_selected(["C", "A"], options) == ["A", "C"]


def test_keep_selected_handles_an_empty_or_missing_selection():
    assert bb.keep_selected(None, ["A"]) == []
    assert bb.keep_selected([], ["A"]) == []


def test_empty_selection_leaves_the_chart_exactly_as_it_was():
    """
    No pin is not a special case: it must be the old top-and-bottom chart to
    the row, or this change moved a number on a traded page.
    """
    qual = _mid_pack()
    ranked = sorted(qual, key=lambda r: r["basis"], reverse=True)
    old = list({bb._label(r): r
                for r in ranked[:10] + ranked[-10:]}.values())
    old.sort(key=lambda r: r["basis"])
    assert [bb._label(r) for r in bb.pinned_rows(qual, [], "basis")] == \
        [bb._label(r) for r in old]
