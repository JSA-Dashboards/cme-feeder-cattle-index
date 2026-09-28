"""
The toggle on the Sale Barn Basis tab and the prose describing it must say the
same thing.

They did not, for a while. MODE_BRACKET was renamed from "vs bracket average"
to "vs bracket basis" because the old word promised price-minus-average-price
while the code does basis-minus-basis -- the two differ by exactly the Timing
column. The constant changed; five strings in render() did not. So the radio
button read "vs bracket basis" while the caption underneath it, and the chart
axis, read "vs bracket average". Ross asked what the metric was, and the reason
he had to ask is that the page gave two answers.

Nothing in the suite could have caught that. It is not a crash, not a number,
and both copies of the file were byte-identical -- test_no_drift.py compares the
copies to each other, so a wrong string present in both passes cleanly.

WHY THIS READS THE AST AND NOT THE TEXT. CLAUDE.md records a check in this repo
that banned a string in comments rather than in queries and duly failed on the
comment explaining the rule. Comments do not appear in an AST at all, so that
class of false positive is structurally impossible here rather than merely
avoided -- and the comment above MODE_BRACKET stays free to quote the old label,
which is where the explanation belongs.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BARN_BASIS = REPO / "barn_basis.py"

# The LABEL, not the word "average". The first version of this file banned
# "bracket average" and flagged two correct sentences on its first run --
# "bracket averaged $363.12" on a tile and "the whole bracket averages only
# $17.15/cwt" in a caption -- where "averages" is an ordinary verb about the
# index and not the name of a control. An over-broad ban that fires on correct
# copy is a ban that gets switched off, which is how a check stops existing.
BANNED = "vs bracket average"


def rendered_strings(src):
    """
    Every string literal the USER could see: f-string parts included, comments
    absent by construction, docstrings removed because they are prose for the
    next developer rather than page copy.
    """
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def _offenders(path):
    return [s for s in rendered_strings(path.read_text(encoding="utf-8"))
            if BANNED in s.lower()]


def test_no_page_copy_says_vs_bracket_average():
    bad = _offenders(BARN_BASIS)
    assert not bad, (
        "barn_basis.py page copy still says {!r}: {}. The toggle is "
        "MODE_BRACKET, and the metric is a barn's basis minus the bracket's "
        "basis -- not its price minus the bracket's average price. The two "
        "differ by exactly the Timing column.".format(BANNED, bad))


def test_captions_name_the_toggle_by_interpolating_the_constant():
    """
    The structural half of the fix. A caption that REPEATS the toggle's text is
    free to drift from it; one that interpolates MODE_FCI / MODE_BRACKET cannot.
    Assert the interpolations are really there, so someone inlining them for
    readability gets a failure rather than a slow-motion repeat of this bug.
    """
    src = BARN_BASIS.read_text(encoding="utf-8")
    for const, n in (("{MODE_FCI}", 3), ("{MODE_BRACKET}", 4)):
        got = src.count(const)
        assert got >= n, (
            "barn_basis.py interpolates {} only {} times, expected at least "
            "{}. A caption naming the toggle in literal text will drift from "
            "it.".format(const, got, n))


def test_the_label_check_can_actually_fail():
    """
    Guard the guard, in both directions. Prose and ordinary verbs must pass;
    page copy naming the dead label must not.
    """
    prose_only = (
        '"""Module doc mentioning vs bracket average."""\n'
        '# NOT "vs bracket average" -- see the caption.\n'
        'MODE = "vs bracket basis"\n'
        'def f(x):\n'
        '    """Docstring saying vs bracket average again."""\n'
        '    return x\n')
    assert not [s for s in rendered_strings(prose_only) if BANNED in s.lower()], \
        "comments and docstrings must not trip the check"

    # The verbs, which the first version of this file wrongly flagged. Both are
    # real sentences from barn_basis.py, about the index and not about a toggle.
    for ok in ('note = f"bracket averaged ${x:,.2f}"\n',
               'cap = f"the whole bracket averages only {v}/cwt"\n'):
        assert not [s for s in rendered_strings(ok) if BANNED in s.lower()], \
            "an ordinary use of the verb must not trip the check: " + ok

    for real in ('cap = "vs bracket average is each barn\'s basis"\n',
                 'cap = f"switch to **vs bracket average** to see the barn"\n',
                 'label = "basis vs FCI" if m else "basis vs bracket average"\n'):
        assert [s for s in rendered_strings(real) if BANNED in s.lower()], \
            "page copy must trip the check: " + real

    # f-strings specifically: the stale axis label lived inside one, and a
    # walker that missed JoinedStr parts would have passed straight over it.
    fstring = 'x = f"Avg {metric} vs bracket average ($/cwt)"\n'
    assert [s for s in rendered_strings(fstring) if BANNED in s.lower()], \
        "f-string parts must be reachable or the axis label is invisible here"


def test_both_copies_carry_the_fix():
    """
    barn_basis.py is shared. test_no_drift.py proves the copies match each
    other; this proves the one they match is the corrected one -- two identical
    copies of a wrong string is exactly the case that passes there.
    """
    portal = (REPO.parent / "livestock-portal" / "apps" / "cme_feeder_cattle"
              / "barn_basis.py")
    if not portal.is_file():
        pytest.skip("livestock-portal not checked out beside this repo")
    bad = _offenders(portal)
    assert not bad, "the portal copy still says {!r}: {}".format(BANNED, bad)
