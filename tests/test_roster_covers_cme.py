"""
Does data/mars_roster.json still cover every sale barn CME counts?

WHY THIS EXISTS. On 2026-09-23 our index read $336.9103 against $336.86 from
both CIH and Compass. The gap was 233 head from Central Plains Stockyards,
Humeston IA -- MARS slug 2018, a barn CME has counted 9 times since 2015, and
simply not in the roster, so no run had ever asked for it. 1,415 qualifying
head uncounted in 2026 across 29 index dates, worst single date 19 cents. It
surfaced only because Compass happened to publish on a day a normally dormant
barn woke up. A diff of cme_ftp_locations against the roster then found four
more (Mobridge SD, both Brush CO barns, Fort Collins CO).

The roster is 89 hand-maintained slugs and NOTHING verified that it covered
CME's sample. Same shape as every trap in CLAUDE.md: silent, no error, exit 0.

WHAT IT ASKS. For every location CME has EVER printed in cme_ftp_locations,
can our pipeline produce that location AT ALL? Never produced is the signal. A
barn we normally produce that missed one date is late publication, a different
problem, and barn_report.py's business -- this check never looks at a single
date, only at whether a name is reachable.

WHAT IT COMPARES AGAINST, and why it is the roster and not mars_sales. The
roster is the defect under guard: it is the list of slugs a run fetches, so it
is exactly "what the pipeline can produce". mars_sales is downstream and lags
-- Brush, Mobridge and Fort Collins are correctly rostered today and have zero
mars_sales rows, because all three last sold before the roster gained them.
Matching on mars_sales would fire on every dormant-but-rostered barn (Sheldon
IA has 8 sale dates in two and a half years) and the check would be noise
inside a month.

WHERE IT RUNS, and what that costs. Here, in the suite, and not in the daily
job or a standalone script. The daily job prints to logs/update_<date>.log,
and today showed what that is worth: the barn report had been printing the
wrong index date and nobody had noticed. A standalone script only helps if
someone remembers it exists, which is the same failure one step earlier. The
suite is the only place in this repo where a wrong answer already stops work.

The price is the one the suite pays: it now depends on data that changes
daily, so CME legitimately adding a barn to its sample turns the suite red on
a morning when nobody changed any code. That is deliberate, and the fix is one
line either way -- add the slug to data/mars_roster.json, or, if AMS publishes
no report for it, add a line to OPEN_GAPS below saying so. The failure message
prints the name, the dates, the head and the states, which is everything
needed to decide which.

IT FAILS IN BOTH DIRECTIONS. The assertion is not "no gaps" plus a list of
excuses -- a suppression list rots, and a check that can only ever be silenced
joins the three in CLAUDE.md that could not fail. It is "the gaps are exactly
OPEN_GAPS", so closing a gap without deleting its entry is also a failure, and
the same equality is applied to GONE.

THERE IS NO WINDOW ANY MORE, and that is the substance of the 2026-09-24
rewrite. The first version of this file only compared the last 730 days. The
window was documented as "dead yards fall out of scope"; what it was actually
hiding was two matching defects, and widening it past two years made the check
accuse barns we demonstrably have:

    norm() knew "Saint" -> "St" and no other abbreviation CME uses, so
    "Ft. Pierre" (156 dates, 134,648 head) did not reach roster slug 2021
    "Ft. Pierre Livestock Auction (Friday) - Ft. Pierre, SD", and the four
    "Lajunta ..." spellings did not reach slugs 1901/1903 at La Junta CO.

    the exclusion list was keyed on the names CME prints TODAY, so every
    earlier spelling of a report we already ingest was reported as a missing
    barn -- "Wy-Ne Direct" (165 dates, 68,965 head), "Ok Direct", and CME's
    own typo "Colorado Idrect". Replayed month by month with today's roster,
    the old check fired in every month from 2024-09 to 2026-08.

Both are fixed below, by norm()'s ABBREVIATIONS table and by direct_state() /
video_report() matching CME's naming PATTERN instead of its current spelling.
With them fixed the comparison is quiet over all 3,207 dates from 2015-01-02
to today, against all 251 names CME has ever printed, minus GONE -- 28 names
CME stopped printing years ago, each written down with the date it stopped.
DEAD_AFTER_DAYS still exists but no longer decides any answer; see its comment.

PROVEN TO FAIL. Removing Humeston from a SANDBOX COPY of the roster makes
test_the_check_fires_when_a_barn_goes_missing catch an AssertionError that
says exactly "Humeston", and that test calls the same helper the check calls,
which is the other half of the rewrite: before it, replacing the check's
assertion with `assert True` left every test in this file passing, because
each proof called uncovered() directly and none routed through the assertion.

A KNOWN LIMIT, written down because it is invisible by design. This answers
yes or no about a NAME, never about a slug, so when two slugs print one name
(Torrington 2101/2103, Billings 1774/1777, La Junta 1901/1903, Brush
1906/3090) losing one of the pair is silent -- the survivor still makes the
name reachable. That is the right answer for a location-level check and it is
not free. Measured on mars_sales since 2024-09-24, the largest single slug
that could vanish this way is Torrington 2103 at 71 sale dates and 28,154
head; La Junta 1901 is 47 dates and 12,838 head, Billings 1777 is 77 dates and
7,785 head. Nothing in this file would notice any of them. Catching it needs a
slug-level comparison against mars_sales, which is a different check.
"""
import ast
import json
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from direct_reports import DIRECT_REPORT_SLUGS       # noqa: E402
from video_reports import VIDEO_REPORT_SLUGS         # noqa: E402

DB = REPO / "data" / "mars_history.db"
ROSTER = REPO / "data" / "mars_roster.json"


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

# The word-level rules, and the only ones. Each is here because CME's own file
# spells one place both ways and the roster picked one; each is pinned by
# test_normalisation_merges against the real rows, and by
# test_normalisation_keeps_apart against the names it must NOT merge.
#
#   saint -> st    "Saint Onge" since 2020, "St. Onge" and "St Onge" before.
#   ft    -> fort  "Ft. Pierre" 156 dates / 134,648 head to 2023-09-15 and
#                  "Ft. Collins" 47 dates / 4,333 head to 2019-11-21, against
#                  roster cities "Fort Pierre" and "Fort Collins".
#   lajunta -> la junta
#                  "Lajunta Livestock" 140 dates, "Lajunta Winter" 161 dates,
#                  "Lajunta" once -- 60,421 head in all -- against roster
#                  slugs 1901/1903, both cities "La Junta".
#
# EVERY PATTERN IS \b-ANCHORED AND THAT MATTERS, though not for the reason the
# first version of this file gave. It claimed the anchor is what keeps
# "Sterling" away from "St. Onge"; it is not -- the table maps saint -> st and
# not st -> saint, so "sterling" is never rewritten at all and dropping the \b
# there changes nothing. The anchor earns its place on "ft", which is a common
# pair of letters inside words: unanchored, "Craft" becomes "Crafort".
# test_the_abbreviation_rules_are_word_anchored pins that.
ABBREVIATIONS = {
    r"\bsaint\b": "st",
    r"\bft\b": "fort",
    r"\blajunta\b": "la junta",
}


def norm(name):
    """
    The comparison key. Case, punctuation and spacing, plus the ABBREVIATIONS
    table above -- no fuzzy distance, no token dropping, nothing that could
    quietly map one barn onto another.
    """
    s = name.strip().lower()
    for pattern, replacement in ABBREVIATIONS.items():
        s = re.sub(pattern, replacement, s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


# --------------------------------------------------------------------------
# the direct-trade and video reports, matched by pattern
# --------------------------------------------------------------------------
#
# CME prints the direct-trade and video/internet auction reports as locations
# alongside the sale barns, under names that are ITS names, not ours: "Texas
# Direct" where we produce "TX DIRECT", "Superior Video (Sc)" where we produce
# "SUPERIOR VIDEO (South Central)". Those are naming artifacts, not roster
# gaps -- the reports behind them come from DIRECT_REPORT_SLUGS and
# VIDEO_REPORT_SLUGS, which are code constants, not this hand-kept JSON.
#
# WHY NOT A LIST OF NAMES. That is what the first version did, and it is why
# the check could not be run over more than two years: a list of today's
# spellings does not know yesterday's. CME has printed the Wyoming-Nebraska
# direct report as "Wy-Ne Direct", "Wy Ne Direct", "Wy/Ne Direct", "Wy & Ne
# Direct", "Wy/Ne/Nd/Sd Direct", "Wyoming-Nebrask", "Wyoming-Nebraska D",
# "Wyoming-Nebraska Dire" and "Wyoming-Nebraska Direc" -- nine spellings of
# one report, and a tenth arrives whenever the fixed-width column moves.
#
# WHERE THE BOUNDARY IS. Both rules below require the name to OPEN with
# something we can name -- a state of CME's region, or the brand of a video
# auction we ingest -- and then require every remaining token to be accounted
# for. An unexplained token means not an aggregate, which is the direction
# that keeps a real barn visible. "Ok Range Sales" opens with a state and is
# still reported, because "range" and "sales" are not direct-report words.
# "Browning Video Auction" is a video-shaped name with a brand we do not
# ingest, so it is reported too -- it is only silenced by being written into
# NOT_INGESTED by hand, which test_not_ingested_is_pinned_independently
# guards.

# Spelled out and as the postal code, because CME uses both. NE and ND map to
# WY: the Wyoming-Nebraska report is one report, filed under "WY" in
# DIRECT_REPORT_SLUGS, and CME has headed it with all four codes.
STATE_ALIASES = {
    "colorado": "CO", "co": "CO",
    "iowa": "IA", "ia": "IA",
    "kansas": "KS", "ks": "KS",
    "missouri": "MO", "mo": "MO",
    "montana": "MT", "mt": "MT",
    "new mexico": "NM", "nm": "NM",
    "oklahoma": "OK", "ok": "OK",
    "south dakota": "SD", "sd": "SD",
    "texas": "TX", "tx": "TX",
    "wyoming": "WY", "wy": "WY",
    "nebraska": "WY", "ne": "WY",
    "north dakota": "WY", "nd": "WY",
}

# CME's one typo for the word, printed once (2024-01-12, 38 head, as "Colorado
# Idrect"). Listed rather than reached by a distance measure, because a
# distance measure is how "Greeley" becomes "Greely" becomes covered.
DIRECT_TYPOS = {"idrect"}


# The day CME sometimes tags a direct report with, as in "Iowa Direct (Wed)".
DAY_TAGS = {"mon", "tue", "tues", "wed", "weds", "thu", "thur", "thurs",
            "fri", "sat", "sun", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday"}


def _is_direct_marker(token):
    """
    Any truncation or extension of the word CME writes after the state:
    "d", "dire", "direc", "direct" (the column has cut it at all four widths)
    and "directo" (2023-02-17, "Missouri Directo"), plus DIRECT_TYPOS.
    """
    return (token in DIRECT_TYPOS
            or token.startswith("direct")
            or "direct".startswith(token))


def _is_state_fragment(token):
    """
    A state alias, or the start of a spelled-out one -- "nebrask" for
    "nebraska". Only the spelled-out aliases may be matched by a fragment: a
    two-letter code is short enough already, and letting a one-character token
    stand in for one would make "d" a state as well as a marker.

    THE MINIMUM LENGTH IS LOAD-BEARING and was missing while this docstring
    already claimed it. Without it "oklahoma c" -- the ten-character cut of
    Oklahoma City, the largest barn in CME's sample at 718,980 head, 4.7% of
    every head CME has ever printed -- reads as the Oklahoma direct report and
    can never be reported as a gap. The cut is not hypothetical: CLAUDE.md
    records CME renaming and widening this column on 2026-09-14, and CME has
    already printed a name cut to five characters ("Winds" for Windsor).
    Every direct spelling CME actually prints cuts to "Oklahoma D", so nothing
    legitimate needs a one-character fragment. Measured cost of the minimum at
    2, 3 or 4: zero -- no name CME has ever printed changes its answer, and
    "Wyoming-Nebrask" still resolves to WY.
    """
    return (token in STATE_ALIASES
            or (len(token) >= 2
                and any(alias.startswith(token)
                        for alias in STATE_ALIASES if len(alias) > 2)))


def direct_state(cme):
    """
    Which state's direct report is CME printing, or None. `cme` is normalised.

    A direct name OPENS with a state of the region, and then EVERY remaining
    token has to be one of four things: the word "Direct" at some width, one
    of CME's typos for it, another state of the region (the Wyoming-Nebraska
    report is headed with up to four), or a day tag. One unexplained token and
    this returns None, which is what keeps "Oklahoma City", "Ok Range Sales"
    and "Missouri Direct Cattle Barn" out.

    The marker may also be missing altogether -- "Wyoming-Nebrask" is the
    column cutting the name before the word "Direct" ever appeared -- so a
    name that is nothing but the state phrase counts too.
    """
    tokens = cme.split()
    for width in (2, 1):                      # "new mexico" before "new"
        if len(tokens) >= width and " ".join(tokens[:width]) in STATE_ALIASES:
            state = STATE_ALIASES[" ".join(tokens[:width])]
            rest = tokens[width:]
            break
    else:
        return None

    if not all(_is_direct_marker(t) or _is_state_fragment(t) or t in DAY_TAGS
               for t in rest):
        return None
    if any(_is_direct_marker(t) for t in rest):
        return state
    # No marker at all: accept only if what is left is a further state, cut or
    # whole. A bare "Texas" on its own is a place, not a report.
    if rest and all(_is_state_fragment(t) for t in rest):
        return state
    return None


# The brand each video/internet auction prints under, mapped to the key we
# ingest it as in VIDEO_REPORT_SLUGS. test_every_video_stem_names_a_report_we
# _ingest checks every value is real, so this table cannot silence a name by
# inventing a source.
#
# It is SHORTER than VIDEO_REPORT_SLUGS on purpose. The per-city add-ons --
# Apache, Beaver, Bassett, Burwell, Crawford, Ericson, Valentine, Billings,
# Lonestar/Wildorado, Ozarks-at-West-Plains -- print as "<city> Video (Sc)",
# and the roster's own city already reaches those through covered_by's
# added-words clause. Only the brands that do not print as one of our cities
# need a stem here.
VIDEO_STEMS = {
    "superior": "SUPERIOR",
    "northern": "NORTHERN_LIVESTOCK",       # "Northern Video", "Northern Livest"
    "nothern": "NORTHERN_LIVESTOCK",        # CME's typo, 2021-01-08, 3,908 hd
    "cattle country": "CATTLE_COUNTRY",
    "cattle drive": "CATTLE_DRIVE",
    "cms": "CMS",
    "liveag": "LIVEAG",
    "ozarks": "OZARKS",
    "ozark": "OZARKS",                      # 2021-08-31, missing the s
    "western": "WESTERN_VIDEO",
    "car jop": "JOPLIN",                    # and "Car-Jop", same rows
    "carthag jopline": "JOPLIN",            # 2020-12-03, 131 hd
    "carthage joplin": "JOPLIN",
    "joplin stockyard": "JOPLIN",           # 2026-05-11 only; NOT a rename --
    # "Car Jop Video (Sc)" kept printing either side of it (2026-04-06 and
    # 2026-07-02), so this is one day's alternate spelling of the same sale.
    "huss lexington": "HUSS_LEXINGTON",
}

# CME's North Central / South Central tags, at every width the column has cut
# them to.
REGION_TAGS = {"n", "s", "nc", "sc"}

# The words that may follow a video brand. Truncations included because the
# column cuts them: "Northern Livestock V", "Joplin Stockyard Vi", "Western
# Video Marke", "Cattle Country Lives".
VIDEO_WORDS = {"vi", "vid", "vide", "video", "lives", "livest", "livestock",
               "website", "internet", "auction", "auc", "market", "marke"}


def video_report(cme):
    """
    Which video/internet auction is CME printing, or None. `cme` is normalised.

    Opens with a brand from VIDEO_STEMS (after an optional leading region tag,
    as in "Sc Superior Video"), and then must be followed by a video word, or
    by nothing but region tags ("Superior (Sc)", where the column dropped the
    word "Video" entirely).

    A ONE-WORD BRAND MUST BE FOLLOWED BY SOMETHING. "Superior", "Western" and
    "Northern" are all plausible town names, so a bare one is reported rather
    than swallowed; a bare two-word brand ("Cattle Country", 2024-07-02) is
    not a town and is accepted.
    """
    tokens = cme.split()
    if tokens and tokens[0] in REGION_TAGS:
        tokens = tokens[1:]
    for width in (3, 2, 1):
        # len() guarded, or " ".join(tokens[:3]) of a one-token name reads as
        # a three-word stem and "Superior" is silenced as a two-word brand.
        if len(tokens) < width:
            continue
        stem = " ".join(tokens[:width])
        if stem not in VIDEO_STEMS:
            continue
        rest = tokens[width:]
        if any(word in VIDEO_WORDS for word in rest):
            return VIDEO_STEMS[stem]
        if rest and all(word in REGION_TAGS for word in rest):
            return VIDEO_STEMS[stem]
        if not rest and width > 1:
            return VIDEO_STEMS[stem]
        return None
    return None


# Video-shaped names CME printed once each with no AMS report behind them we
# ingest. This is the ONE hand-kept escape hatch left in the exclusion side,
# so it carries its own guards: the count is pinned, each name must still be
# one CME prints, and each must read as a video auction. A bare barn name
# ("Greeley") fails that last rule, which is the edit this is here to refuse
# -- see test_a_bare_barn_name_cannot_be_filed_as_not_ingested.
NOT_INGESTED = frozenset({
    "Browning Video Auction",       # 2026-03-18, 192 hd, region SC
    "New Video Auction Rep",        # 2026-01-15, 195 hd, region XX
})
_NOT_INGESTED_NORM = {norm(n) for n in NOT_INGESTED}

# What a name has to read like before it may be filed under NOT_INGESTED.
VIDEO_OR_AUCTION = re.compile(r"\b(video|vid|auction|auc)\b", re.IGNORECASE)


def is_aggregate(cme):
    """True if CME's name is a direct or video report rather than a barn."""
    return (direct_state(cme) is not None
            or video_report(cme) is not None
            or cme in _NOT_INGESTED_NORM)


# --------------------------------------------------------------------------
# matching CME's printed name to a roster name
# --------------------------------------------------------------------------

# The shortest CME name the truncation clause may answer for. The two names
# that need the clause are 20 and 21 characters ("Mid Missouri Stockya",
# "Mid Missouri Stockyar"); the shortest name CME has ever printed is 3
# ("Ava", "Tul"). 10 sits between them with room either side, and stops the
# clause absorbing "Mid", "Mid M" or "M" into the Mid Missouri title, which it
# did before -- a one-letter name matching a barn is the "too loose, stays
# quiet" failure in its purest form.
MIN_TRUNCATION = 10


def covered_by(cme, ours, truncatable=False):
    """
    Does roster name `ours` account for CME's printed name `cme`? Both already
    normalised.

    Two ways normally, both forced by real rows:

      equal                     "Dodge City"            -> "Dodge City"
      cme adds words            "Kearney Huss"          -> "Kearney"
                                "North Platte Stock"    -> "North Platte"
                                "Billings Pays"         -> "Billings"

    The added-words case needs the trailing space. Without it "Green City"
    would answer for a barn CME called "Greencastle", and the gap would never
    be printed.

    AND, only when `truncatable`, a third: CME's name is a prefix of ours,
    cut mid-word by the fixed-width column -- "Mid Missouri Stockya" against
    the roster title "Mid Missouri Stockyards Cattle Auction - Phillipsburg,
    MO". That clause is off by default because it is the dangerous one: a
    short new barn name is a prefix of plenty of roster names ("Green" of
    "Green City", "Union" of "Unionville"), and a rule that absorbs those is
    the "too loose, matches a real gap to an unrelated barn and stays quiet"
    failure. roster_names decides which entries may use it -- see there --
    and MIN_TRUNCATION decides which CME names are long enough to ask.

    ONE CME NAME MAY BE BACKED BY TWO SLUGS and that is fine: this answers yes
    or no about a name, never about a slug. Torrington 2101/2103, Billings
    1774/1777, La Junta 1901/1903 and Brush 1906/3090 each print one name.
    The module docstring records what that costs.
    """
    return (cme == ours
            or cme.startswith(ours + " ")
            or (truncatable
                and len(cme) >= MIN_TRUNCATION
                and ours.startswith(cme)))


def roster_names(roster_path=ROSTER):
    """
    {normalised roster name: may CME truncate it?}.

    update_index.py stores `loc["city"] or loc["title"]`, so the city is the
    name CME usually prints, and the title is carried alongside it.

    TRUNCATION IS ALLOWED ONLY FOR AN ENTRY WITH NO CITY. That is not a
    special case for one barn, it is the shape of the problem: when the roster
    knows the city, CME prints the city and covered_by's first two clauses
    reach it; when the roster does not, all we have to match against is a long
    market title, and CME's fixed-width column cuts it mid-word. Mid Missouri
    Stockyards (slug 3654) is the only such entry today.

    ALLOWING IT FOR TITLED ENTRIES TOO WAS CONSIDERED AND MEASURED. The case
    for it was Ft. Pierre, whose roster title literally contains CME's printed
    name -- but that is now reached by norm()'s ft -> fort rule, and over all
    251 names CME has ever printed the loose variant absorbs nothing at all
    that the shipped rule does not. It is therefore pure added risk, and
    test_allowing_truncation_for_titled_entries_would_buy_nothing keeps that
    conclusion measured rather than asserted.
    """
    roster = json.loads(Path(roster_path).read_text(encoding="utf-8"))
    names = {}
    for e in roster:
        titleless = not e.get("city")
        for n in (e.get("city"), e["title"]):
            if n:
                names[norm(n)] = names.get(norm(n), False) or titleless
    return names


# --------------------------------------------------------------------------
# what is left over: names we will never chase, and names we still owe
# --------------------------------------------------------------------------

# How long CME must have been silent about a name before it may be filed under
# GONE rather than OPEN_GAPS.
#
# THIS CONSTANT DECIDES NOTHING. It was LOOKBACK_DAYS in the first version of
# this file, where it WAS the answer -- it windowed the comparison, and
# widening it broke the check. It does not window anything now: every name CME
# has ever printed is compared, and every one that the roster cannot produce
# is written down either in GONE or in OPEN_GAPS by hand. All this does is
# keep those two lists from being used for each other's job.
#
# It is pinned on BOTH sides by test_the_dead_line_falls_where_nothing_sits.
# Measured 2026-09-24: the most recent GONE name last printed 2023-11-01
# (1,058 days ago) and the oldest OPEN_GAPS name last printed 2026-01-06 (261
# days ago), so any value in 262..1058 gives exactly these answers and 730
# sits in the middle of that. Mutating it to 365 changes nothing, which is the
# point of the rewrite, not a hole in it -- the day it becomes load-bearing
# again is the day that test fails and names the two dates that closed on it.
DEAD_AFTER_DAYS = 730

# Names CME printed and stopped printing, with the date it stopped. None of
# them is a barn we are going to chase: the yard closed, or the name was a
# one-off spelling of something else, or there is no AMS report behind it.
#
# An entry is not a suppression. Every one is held to three things, each by
# its own test: the recorded date must still be CME's last print of that name
# (so a reopened yard fails, by name), the date must be at least
# DEAD_AFTER_DAYS old (so nothing live can hide here), and the name must still
# be one the roster cannot produce (so rostering it forces the line's
# deletion). GONE and OPEN_GAPS together must equal the leftovers exactly.
GONE = {
    # --- yards CME counted and no longer prints ---------------------------
    "St. Joseph":             ("2020-02-12", "St Joseph MO, closed; 229 dates to 2020"),
    "St Joseph":              ("2021-05-19", "the same yard, respelled in 2020"),
    "Rushville-Sheridan":     ("2020-02-12", "Rushville NE, closed"),
    "Rushville":              ("2022-08-31", "the same yard, short name"),
    "Rushville (Sheridan)":   ("2015-08-26", "the same yard, one date"),
    "Sedalia":                ("2021-09-27", "Sedalia MO, closed"),
    "West Fargo":             ("2020-03-11", "West Fargo ND, closed"),
    "Loup City":              ("2018-08-21", "Loup City NE, closed"),
    "Passaic-Butler":         ("2020-01-16", "Passaic MO; Butler is rostered, Passaic is gone"),
    "Amarillo":               ("2021-09-27", "Amarillo TX, closed"),
    "Ava-Douglas County":     ("2020-01-30", "Ava MO, closed"),
    "Ava":                    ("2022-08-04", "the same yard, short name"),
    "Boonville":              ("2018-04-17", "Boonville MO, closed"),
    "Booneville":             ("2015-01-06", "the same yard, misspelled, one date, 14 hd"),
    "Patton Junction":        ("2020-02-10", "Patton MO, closed"),
    "Patton":                 ("2021-06-28", "the same yard, short name"),
    "Maryville":              ("2016-01-12", "Maryville MO, closed; 8 dates in 2015"),
    "Sioux Center":           ("2021-02-26", "Sioux Center IA, 3 dates, 230 hd, gone"),
    "Fort Worth":             ("2022-12-15", "Fort Worth TX, 6 dates, gone"),
    # --- one date each, and not a barn we could roster --------------------
    "Winter Fc Special":      ("2021-08-09", "a Winter Livestock one-off; no slug of its own"),
    "Ok Range Sales":         ("2020-02-21", "an OK line, not a barn; opens with a state but "
                                             "'range sales' is not a direct report"),
    "Cloviis":                ("2019-11-06", "Clovis NM misspelled -- Clovis has no row that "
                                             "date and is rostered"),
    "Winds":                  ("2023-11-01", "almost certainly Windsor MO cut to 5 characters: "
                                             "Windsor has no row that Wednesday, and this is the "
                                             "only state='OR' row in the whole table"),
    "Tul":                    ("2016-02-18", "3 characters, state IA, 707 hd; no IA barn it "
                                             "resolves to and nothing like it before or since"),
    # --- a video line with no AMS report we ingest ------------------------
    # Printed 2018-2020 and never since. Not Apache: "Apache Video (Sc)" ran
    # right through the same period and still does.
    "Southern Ok Video (Sc)": ("2019-07-24", "no AMS video report of this name"),
    "Southern Ok Video (S":   ("2019-12-04", "same, column cut one narrower"),
    "Southern Ok Video":      ("2020-02-26", "same, column cut wider"),
    "Southern Ok Vid":        ("2020-02-19", "same, column cut wider still"),
}

# Sale barns CME counts that the roster cannot produce, with why. Empty is the
# goal; an entry is a debt, not a settlement. The comparison must equal this
# set exactly, so closing one means deleting its line here in the same commit.
OPEN_GAPS = {
    # Found by this check on 2026-09-24, the day it was written, and NOT by the
    # hand diff that added the other five. Real barn rows, not a parse
    # artifact: 103 hd at 726 lb on 2025-01-14 and 157 hd at 780 lb on
    # 2026-01-06. Both are early January, which is when Fort Collins slug 1863
    # (Centennial's Stock Show Special) also sells, so a Greeley special sale
    # is the likeliest reading -- but the first version of this file called
    # both dates "Stock Show week" and that is not checked: the National
    # Western's dates are not in this database and 2026-01-06 is a week
    # earlier in January than 2025-01-14. No MARS slug identified yet --
    # needs someone to search the AMS report list for a Greeley CO feeder
    # cattle report.
    "Greeley": "no MARS slug identified yet (2 dates, 260 hd, 2025-01-14 and 2026-01-06)",
}


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------

def cme_locations(conn):
    """
    {printed name: (dates, head, first, last, states)} over the whole table.

    READ-ONLY, by connection and by query: this opens the live pushed database
    the 07:30 and 13:00 runs write to.
    """
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT location, COUNT(DISTINCT report_date), SUM(head_count), "
        "MIN(report_date), MAX(report_date), GROUP_CONCAT(DISTINCT state) "
        "FROM cme_ftp_locations GROUP BY location")}


def last_printed(conn):
    """{printed name: the last date CME printed it}."""
    return dict(conn.execute(
        "SELECT location, MAX(report_date) FROM cme_ftp_locations "
        "GROUP BY location"))


def uncovered(conn, roster_path=ROSTER, drop_gone=True):
    """
    {CME's printed name: (dates, head, first, last, states)} for every sale
    barn the roster cannot produce. Sorted by head descending, because that is
    the order they cost money in.

    `drop_gone=False` keeps the names written down in GONE, which is how
    test_gone_lists_exactly_the_leftovers checks that list is neither short
    nor stale.
    """
    ours = roster_names(roster_path)
    found = {name: stats for name, stats in cme_locations(conn).items()
             if not (drop_gone and name in GONE)
             and not is_aggregate(norm(name))
             and not any(covered_by(norm(name), o, trunc)
                         for o, trunc in ours.items())}
    return dict(sorted(found.items(), key=lambda kv: (-(kv[1][1] or 0), kv[0])))


def assert_gaps_are_written_down(found):
    """
    THE ASSERTION, on its own so that a test can prove it fires.

    It lived inline in test_roster_covers_every_barn_cme_counts until
    2026-09-24, and replacing it with `assert True` left all 39 tests in this
    file passing: every proof called uncovered() directly, so they proved the
    FUNCTION worked and nothing proved the CHECK asserted on it.
    test_the_check_fires_when_a_barn_goes_missing now calls this helper with a
    roster that is missing Humeston and requires the AssertionError.
    """
    detail = "\n".join(
        f"    {name!r:<26} {stats[0]:>3} dates  {stats[1]:>7} hd  "
        f"{stats[2]}..{stats[3]}  [{stats[4]}]" for name, stats in found.items())
    assert set(found) == set(OPEN_GAPS), (
        "CME's sample and data/mars_roster.json disagree.\n"
        f"  CME locations the roster cannot produce ({len(found)}):\n{detail}\n"
        f"  written down in OPEN_GAPS: {sorted(OPEN_GAPS)}\n"
        "  Add the MARS slug to data/mars_roster.json, or add a line to "
        "OPEN_GAPS saying why there is none. If a gap was just closed, delete "
        "its OPEN_GAPS line.")


def open_db():
    return sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)


@pytest.fixture(scope="module")
def conn():
    if not DB.exists():
        pytest.skip("data/mars_history.db not present")
    c = open_db()
    yield c
    c.close()


def _sandbox_roster(tmp_path, slug_ids):
    """A COPY of the roster with `slug_ids` removed. Never the tracked file --
    the mutation harness written on 2026-09-23 reported every mutant surviving
    because pytest was loading the tracked roster instead of the sandbox, so
    the path is passed in explicitly everywhere below."""
    roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    kept = [e for e in roster if e["slug_id"] not in slug_ids]
    assert len(kept) == len(roster) - len(slug_ids), \
        f"not all of {slug_ids} are in the roster"
    sandbox = tmp_path / f"roster_without_{'_'.join(map(str, slug_ids))}.json"
    sandbox.write_text(json.dumps(kept), encoding="utf-8")
    return sandbox


def _drop_slugs(conn, tmp_path, slug_ids):
    """What the check newly reports when `slug_ids` leave a COPY of the roster."""
    sandbox = _sandbox_roster(tmp_path, slug_ids)
    return set(uncovered(conn, sandbox)) - set(uncovered(conn))


# --------------------------------------------------------------------------
# the check itself
# --------------------------------------------------------------------------

def test_roster_covers_every_barn_cme_counts(conn):
    """
    THE CHECK. Every sale barn CME has ever printed is one the roster can
    produce, except the ones written down in GONE and OPEN_GAPS.

    Equality, not emptiness, so this fails when a gap opens AND when one is
    closed without deleting its line.
    """
    assert_gaps_are_written_down(uncovered(conn))


def test_the_check_fires_when_a_barn_goes_missing(conn, tmp_path):
    """
    PROOF THE ASSERTION IS LOAD-BEARING. Not that uncovered() finds Humeston
    -- the test below does that -- but that the helper the check calls raises
    when it does. Mutate assert_gaps_are_written_down's assertion to
    `assert True` and this is the test that dies.
    """
    sandbox = _sandbox_roster(tmp_path, [2018])
    with pytest.raises(AssertionError) as raised:
        assert_gaps_are_written_down(uncovered(conn, sandbox))
    assert "Humeston" in str(raised.value)


def _reaches_the_helper(source, func_name="test_roster_covers_every_barn_cme_counts"):
    """Does `func_name` in `source` call assert_gaps_are_written_down? Parsed,
    not grepped -- a guard that matches a string matches it in a comment."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return any(isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Name)
                       and n.func.id == "assert_gaps_are_written_down"
                       for n in ast.walk(node))
    return False


def test_the_check_routes_through_the_asserting_helper():
    """
    The other half of the same hole: factoring the assertion out only helps
    while the check still calls it. And the guard is fed a known violation, as
    CLAUDE.md requires of any guard.
    """
    assert _reaches_the_helper(Path(__file__).read_text(encoding="utf-8"))
    assert not _reaches_the_helper(
        "def test_roster_covers_every_barn_cme_counts(conn):\n"
        "    assert True\n")


def test_known_good_barn_removed_from_the_roster_is_named(conn, tmp_path):
    """
    Humeston is the barn that started this; drop it from a COPY of the roster
    and the check must name Humeston, and only Humeston.
    """
    sandbox = _sandbox_roster(tmp_path, [2018])
    assert set(uncovered(conn, sandbox)) - set(OPEN_GAPS) == {"Humeston"}


@pytest.mark.parametrize("slug_id,newly_reported", [
    # the other four barns added on 2026-09-24
    (2025, {"Mobridge"}),
    # Fort Collins comes back under BOTH spellings, which is the regression
    # pin for norm()'s ft -> fort rule: before it, "Ft. Collins" was reported
    # as a missing barn while slug 1863 was sitting in the roster.
    (1863, {"Fort Collins", "Ft. Collins"}),
    # Brush is TWO slugs printing one name. Removing one must report NOTHING:
    # the survivor still makes "Brush" reachable, and that is the right answer
    # for a location-level check. Removing both must report it.
    (1906, set()),
    (3090, set()),
    # the roster entry with no city, matched through its title, which CME
    # prints truncated at three different widths
    (3654, {"Mid Missouri Stockya", "Mid Missouri Stockyar", "Mid Missouri Stockyards"}),
    # a barn CME prints under two names -- both must come back
    (1848, {"Kearney", "Kearney Huss"}),
    # and one CME prints under three truncations of one name
    (3653, {"North Platte Stock", "North Platte Stockyar", "North Platte Stockyard"}),
    # the other half of the ft -> fort pin: 134,648 head printed as "Ft.
    # Pierre" and 211,480 as "Fort Pierre", one roster slug behind both
    (2021, {"Fort Pierre", "Ft. Pierre"}),
])
def test_removing_any_one_barn_names_exactly_that_barn(conn, tmp_path,
                                                       slug_id, newly_reported):
    """
    The same proof as above for the awkward shapes, with the expected answer
    written out by hand rather than computed -- a computed expectation is how
    a check ends up printing the same variable under both labels.
    """
    assert _drop_slugs(conn, tmp_path, [slug_id]) == newly_reported


@pytest.mark.parametrize("slug_ids,newly_reported", [
    ([1906, 3090], {"Brush"}),
    # the lajunta -> la junta pin: five spellings, two slugs, and the roster
    # has to lose both before any of them is reported
    ([1901, 1903], {"La Junta", "La Junta Winter", "Lajunta",
                    "Lajunta Livestock", "Lajunta Winter"}),
    ([2101, 2103], {"Torrington", "Torrington Livesto", "Torrington Livestock"}),
])
def test_losing_both_slugs_of_a_shared_name_reports_it(conn, tmp_path,
                                                       slug_ids, newly_reported):
    """The other half of the two-slug case: lose both and the name is gone."""
    assert _drop_slugs(conn, tmp_path, slug_ids) == newly_reported


def test_the_biggest_gap_is_named_first(conn, tmp_path):
    """
    uncovered() sorts on head descending so the failure message opens with the
    barn that costs the most, not the one that sorts first alphabetically.
    Drop Mobridge (90,869 hd all-time) and Fort Collins (6,135) together:
    alphabetical order would put Fort Collins first.
    """
    sandbox = _sandbox_roster(tmp_path, [2025, 1863])
    found = uncovered(conn, sandbox)
    assert [n for n in found if n in ("Mobridge", "Fort Collins")] == [
        "Mobridge", "Fort Collins"]


# --------------------------------------------------------------------------
# the exclusion rules, which are the part that could hide a real gap
# --------------------------------------------------------------------------

def test_every_video_stem_names_a_report_we_ingest():
    """
    A stem silences a family of CME names, so it has to name the thing that
    covers them. Inventing "OZARKS_2" does not compile.
    """
    unknown = {k: v for k, v in VIDEO_STEMS.items()
               if v not in VIDEO_REPORT_SLUGS}
    assert not unknown, f"video stems naming a report we do not ingest: {unknown}"


def test_no_direct_name_resolves_to_a_state_we_do_not_ingest(conn):
    """The direct rule may only silence a name by naming a report we fetch."""
    stray = {n: direct_state(norm(n)) for n in cme_locations(conn)
             if direct_state(norm(n)) is not None
             and direct_state(norm(n)) not in DIRECT_REPORT_SLUGS}
    assert not stray, f"direct names resolving to a state we do not ingest: {stray}"


def test_every_direct_report_is_reached_by_a_name_cme_actually_prints(conn):
    """
    WHAT THIS IS FOR. Its predecessor asserted

        {v for v in AGGREGATES.values() if v in DIRECT_REPORT_SLUGS}
            == set(DIRECT_REPORT_SLUGS)

    which held while "Wy-Ne Direct", "Ok Direct" and "Colorado Idrect" were
    all being reported as missing barns -- because WY, OK and CO each already
    had SOME entry. It proved every ingested state had a stem, not that every
    printed name matched one. This resolves the names CME has actually
    printed and requires the ten states to come back out.
    """
    reached = {direct_state(norm(n)) for n in cme_locations(conn)}
    reached.discard(None)
    assert reached == set(DIRECT_REPORT_SLUGS), (
        f"states no printed name resolves to: "
        f"{sorted(set(DIRECT_REPORT_SLUGS) - reached)}")


@pytest.mark.parametrize("printed,state", [
    # the three the old list missed, and the reason the window could not be
    # widened past two years
    ("Wy-Ne Direct", "WY"),             # 165 dates, 68,965 hd, to 2024-04-12
    ("Ok Direct", "OK"),                # 4,051 hd
    ("Colorado Idrect", "CO"),          # CME's typo, 38 hd
    # and the rest of the spellings of the one report, written out because
    # nine spellings of one thing is the whole argument for a pattern
    ("Wy Ne Direct", "WY"),
    ("Wy & Ne Direct", "WY"),
    ("Wy/Ne/Nd/Sd Direct", "WY"),
    ("Wyoming-Nebrask", "WY"),          # no marker at all, column cut it off
    ("Wyoming-Nebraska D", "WY"),       # 16 dates, still printing in 2026
    ("Wyoming-Nebraska Direc", "WY"),
    ("New Mexico Dire", "NM"),
    ("Oklahoma Direc", "OK"),
    ("South Dakota Direc", "SD"),
    ("Missouri Directo", "MO"),
    ("Iowa Direct (Wed)", "IA"),
])
def test_every_spelling_cme_has_used_resolves(printed, state):
    assert direct_state(norm(printed)) == state


@pytest.mark.parametrize("printed,report", [
    ("Superior Video (Sc)", "SUPERIOR"),
    ("Superior Vid Website", "SUPERIOR"),
    ("Superior (Sc)", "SUPERIOR"),          # no video word left at all
    ("Sc Superior Video", "SUPERIOR"),      # region tag in front
    ("Nothern Video (Nc)", "NORTHERN_LIVESTOCK"),   # CME's typo
    ("Northern Livestock V", "NORTHERN_LIVESTOCK"),
    ("Cattle Country", "CATTLE_COUNTRY"),   # bare two-word brand
    ("Cattle Drive Video (N", "CATTLE_DRIVE"),
    ("Ozark Video (Sc)", "OZARKS"),         # missing the s
    ("Carthag Jopline Vide", "JOPLIN"),
    ("Joplin Stockyard Vi", "JOPLIN"),
    ("Car-Jop Video (Sc)", "JOPLIN"),
    ("Western Video Market Video Auc", "WESTERN_VIDEO"),
    ("Huss Lexington Video (", "HUSS_LEXINGTON"),
])
def test_every_video_spelling_cme_has_used_resolves(printed, report):
    assert video_report(norm(printed)) == report


@pytest.mark.parametrize("printed", [
    "Ok Range Sales",       # opens with a state, but not a direct report
    "Oklahoma City",        # a barn, and the biggest one in the sample
    "Oklahoma City Speci",
    # THE CUTS, not just the full names. CME's location column is fixed-width
    # and was widened on 2026-09-14 (CLAUDE.md); it has already printed a name
    # cut to five characters ("Winds" for Windsor). A ten-character cut of the
    # biggest barn in the sample read as the Oklahoma direct report and could
    # never be reported as a gap -- 718,980 head, 4.7% of everything CME has
    # printed. Pinning the full name did not pin the cut.
    "Oklahoma C",
    "Colorado S",
    "Texas M",
    "Missouri Direct Cattle Barn",   # a state and the word, and other words
    "Superior",             # a bare one-word brand could be a town
    "Western",
    "Northern",
    "Greeley Superior Video",   # a stem in the MIDDLE is not a stem
    "Greeley Video",
    "Browning Video Auction",   # video-shaped, but the brand is not ours
    "New Video Auction Rep",
    # REAL TOWNS THAT SHARE A FIRST LETTER WITH A MARKER. Dighton KS, Delta CO,
    # Dillon MT and Dodge City KS are all feeder-cattle towns, so "<state>
    # <town>" is a shape CME could print. Loosening DIRECT_TYPOS from the one
    # known typo to anything starting with "d" reads all four as direct
    # reports, and before these cases nothing noticed.
    "Kansas Dighton",
    "Colorado Delta",
    "Montana Dillon",
    "Texas Dodge",
    # A BRAND PLUS AN ORDINARY WORD. Adding "sale" or "special" to VIDEO_WORDS
    # turns every one of these into a video report, which would silence a barn
    # that happened to be named this way.
    "Superior Sale",
    "Northern Sale",
    "Western Special",
])
def test_names_the_pattern_must_not_swallow(printed):
    """
    The boundary, from the side that matters. Everything here is video- or
    direct-SHAPED and none of it may be silenced by a rule -- the last two are
    silenced, but only by being written into NOT_INGESTED by hand.
    """
    assert direct_state(norm(printed)) is None
    assert video_report(norm(printed)) is None


def test_no_prefix_cut_of_a_real_barn_reads_as_an_aggregate(conn):
    """
    THE PROPERTY, rather than another hand-written list of names.

    CME's location column is fixed-width and it truncates -- CLAUDE.md records
    the column being renamed and widened on 2026-09-14, and CME has printed a
    name cut to five characters ("Winds" for Windsor). So every prefix of a real
    barn name is a name CME might one day print, and none of them may read as a
    direct or video aggregate. Aggregates are excluded from the comparison, so a
    barn swallowed that way can never be reported as a gap however much head it
    carries.

    Being derived from the data, this constrains the token vocabularies --
    STATE_ALIASES, DIRECT_TYPOS, DAY_TAGS, VIDEO_WORDS -- without pinning their
    membership. Widen any of them far enough to swallow a barn and this dies,
    which a list of negative examples cannot promise: it only ever tests the
    names someone thought of.

    MUTATION: remove the `len(token) >= 2` guard from _is_state_fragment and
    this names Oklahoma City, the biggest barn in CME's sample at 718,980 head,
    whose ten-character cut "oklahoma c" reads as the Oklahoma direct report.

    WHAT IS NOT CONSTRAINED, measured rather than assumed: widening DAY_TAGS
    (say with "city", "range", "sales") survives every test here, and it
    survives because it is inert -- it changes the answer for 0 of the 159 real
    barn names CME has ever printed, and only for contrived strings like
    "Oklahoma Direct City" that already carry the direct marker. DAY_TAGS is
    only consulted for a name that already reads as <state> + <direct marker>,
    so widening it cannot pull in a barn. No test is written for a name that
    cannot occur.
    """
    printed = [r[0] for r in
               conn.execute("SELECT DISTINCT location FROM cme_ftp_locations")]
    barns = [p for p in printed
             if not direct_state(norm(p)) and not video_report(norm(p))]
    swallowed = {}
    for p in barns:
        n = norm(p)
        for k in range(3, len(n)):
            cut = n[:k].strip()
            if cut and (direct_state(cut) or video_report(cut)):
                swallowed[p] = cut
                break
    assert not swallowed, (
        "a prefix cut of these real barns reads as an aggregate, so the barn "
        "could never be reported missing: "
        + ", ".join(f"{b!r} cut to {c!r}" for b, c in sorted(swallowed.items())))


def test_a_name_that_merely_contains_a_stem_is_not_an_aggregate():
    """
    The old rule was `cme.startswith(stem)` and its guard tested the same
    prefix semantics, so widening it to `stem in cme` survived the suite. Both
    rules here anchor at the start of the name (after at most one region tag),
    and this is the case that dies if either stops doing so.
    """
    assert is_aggregate(norm("Superior Video (Sc)"))
    assert not is_aggregate(norm("Greeley Superior Video"))
    assert not is_aggregate(norm("New Cambria Cattle Country Sale"))
    assert is_aggregate(norm("Texas Direct"))
    assert not is_aggregate(norm("Kingsville Texas Direct"))


def test_not_ingested_is_pinned_independently(conn):
    """
    NOT_INGESTED is the last hand-kept name list on the exclusion side, so it
    is pinned three ways that do not depend on each other. Replacing the
    frozenset literal with a comprehension over the rest of this file -- the
    tautology CLAUDE.md records -- leaves all three standing.
    """
    assert len(NOT_INGESTED) == 2, \
        f"a third unsourced aggregate is a deliberate edit, not a shrug: {NOT_INGESTED}"
    printed = {r[0] for r in
               conn.execute("SELECT DISTINCT location FROM cme_ftp_locations")}
    assert NOT_INGESTED <= printed, \
        f"names CME does not print: {sorted(NOT_INGESTED - printed)}"
    unnamed = {n for n in NOT_INGESTED if not VIDEO_OR_AUCTION.search(n)}
    assert not unnamed, \
        f"NOT_INGESTED is for video/auction lines, and these are not: {unnamed}"


def test_a_bare_barn_name_cannot_be_filed_as_not_ingested():
    """
    THE EDIT THIS REFUSES. Three changes together used to silence a real gap
    with the whole suite green: add "Greeley" to the exclusion list with no
    source, add it to NOT_INGESTED, delete its OPEN_GAPS line. Each alone
    failed; the combination did not, and it is exactly what someone clearing a
    red suite would write. The escape hatch now costs a name that reads as a
    video auction, and a barn name does not.
    """
    assert not VIDEO_OR_AUCTION.search("Greeley")
    assert not VIDEO_OR_AUCTION.search("Humeston")
    assert VIDEO_OR_AUCTION.search("Browning Video Auction")
    assert VIDEO_OR_AUCTION.search("New Video Auction Rep")


def test_no_exclusion_rule_swallows_a_rostered_barn():
    """
    The rules match on the name, so a careless stem could swallow a barn --
    "Cattle" would take "Cattle Country" and nothing else today, but a stem
    like "Cu" would take Cuba tomorrow. No name the roster carries, city or
    title, may be read as an aggregate.
    """
    bad = {o for o in roster_names() if is_aggregate(o)}
    assert not bad, f"exclusion rules that shadow a roster barn: {bad}"


def test_every_video_stem_matches_something_cme_has_printed(conn):
    """
    A stem that matches nothing is either a typo or an over-broad guess
    waiting to swallow the next new name.
    """
    printed = [norm(r[0]) for r in
               conn.execute("SELECT DISTINCT location FROM cme_ftp_locations")]
    dead = {stem for stem in VIDEO_STEMS
            if not any(p == stem or p.startswith(stem + " ")
                       or p.startswith("nc " + stem) or p.startswith("sc " + stem)
                       for p in printed)}
    assert not dead, f"video stems CME has never printed: {dead}"


# --------------------------------------------------------------------------
# the two written-down lists
# --------------------------------------------------------------------------

def test_gone_lists_exactly_the_leftovers(conn):
    """
    GONE and OPEN_GAPS together are every name the roster cannot produce --
    no more and no less. Rostering a GONE barn fails this until its line goes,
    and so does a new dead name nobody wrote down.
    """
    leftovers = set(uncovered(conn, drop_gone=False))
    assert leftovers == set(GONE) | set(OPEN_GAPS), (
        f"  not written down: {sorted(leftovers - set(GONE) - set(OPEN_GAPS))}\n"
        f"  written down but now reachable, delete the line: "
        f"{sorted((set(GONE) | set(OPEN_GAPS)) - leftovers)}")


def test_gone_and_open_gaps_do_not_overlap():
    assert not set(GONE) & set(OPEN_GAPS)


def test_gone_names_are_still_gone(conn):
    """
    The anti-rot guard, and the one that makes GONE an observation rather than
    an excuse: the date written beside each name must still be CME's last
    print of it. A reopened yard moves its date and is named here.
    """
    last = last_printed(conn)
    moved = {name: (recorded, last.get(name))
             for name, (recorded, _) in GONE.items()
             if last.get(name) != recorded}
    assert not moved, (
        f"GONE names CME has printed since the date written down "
        f"(name: written, actual): {moved}")


def test_gone_names_are_dead_by_the_written_rule(conn):
    """Nothing live may hide in GONE."""
    cutoff = (date.today() - timedelta(days=DEAD_AFTER_DAYS)).isoformat()
    live = {n: d for n, (d, _) in GONE.items() if d >= cutoff}
    assert not live, (
        f"GONE entries CME printed within {DEAD_AFTER_DAYS} days -- these "
        f"belong in OPEN_GAPS: {live}")


def test_open_gaps_are_still_live(conn):
    """
    And nothing dead may hide in OPEN_GAPS. This is what stops it becoming the
    suppression list the docstring says it is not.
    """
    last = last_printed(conn)
    cutoff = (date.today() - timedelta(days=DEAD_AFTER_DAYS)).isoformat()
    stale = {n: last.get(n) for n in OPEN_GAPS
             if last.get(n) is None or last[n] < cutoff}
    assert not stale, (
        f"OPEN_GAPS entries CME has stopped printing -- move them to GONE or "
        f"delete them: {stale}")


def test_the_dead_line_falls_where_nothing_sits(conn):
    """
    DEAD_AFTER_DAYS, pinned on both sides so that the day it starts deciding
    something is the day this fails. The two lists are separated by a gap with
    no name in it; the constant has to land inside that gap, and today the gap
    is wide enough that the exact value cannot matter.
    """
    last = last_printed(conn)
    youngest_dead = max(last[n] for n in GONE)
    oldest_live_gap = min(last[n] for n in OPEN_GAPS)
    cutoff = (date.today() - timedelta(days=DEAD_AFTER_DAYS)).isoformat()
    assert youngest_dead < cutoff <= oldest_live_gap, (
        f"DEAD_AFTER_DAYS={DEAD_AFTER_DAYS} puts the line at {cutoff}, but the "
        f"newest name in GONE last printed {youngest_dead} and the oldest name "
        f"in OPEN_GAPS last printed {oldest_live_gap}. The two lists no longer "
        f"straddle the line: move a name, or choose a value between them.")


def test_every_gone_name_is_a_name_cme_printed(conn):
    """A GONE entry for a name CME never printed is a typo silencing nothing."""
    printed = set(last_printed(conn))
    assert set(GONE) <= printed, f"GONE names CME never printed: {sorted(set(GONE) - printed)}"


# --------------------------------------------------------------------------
# normalisation: what it merges, and what it must keep apart
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cme,ours", [
    ("Mcalester", "McAlester"),                 # case only
    ("Mccook", "McCook"),
    ("Saint Onge", "St. Onge"),                 # saint -> st
    ("St Onge", "Saint Onge"),
    ("Ft. Pierre", "Fort Pierre"),              # ft -> fort
    ("Ft. Collins", "Fort Collins"),
    ("Lajunta", "La Junta"),                    # lajunta -> la junta
    ("Lajunta Winter", "La Junta"),
    ("Lajunta Livestock", "La Junta"),
    ("La Junta", "La  Junta"),                  # spacing
    ("Kearney Huss", "Kearney"),                # CME adds the market name
    ("Billings Pays", "Billings"),
    ("North Platte Stockyar", "North Platte"),
    ("Crawford Livestock", "Crawford"),
])
def test_normalisation_merges(cme, ours):
    assert covered_by(norm(cme), norm(ours))


@pytest.mark.parametrize("cme,ours", [
    ("Greeley", "Green City"),        # the live gap, against its nearest name
    ("Greeley", "Greeley Video"),     # a barn is not its own video sale
    ("Ada", "Aberdeen"),
    ("Mitchell", "Miles City"),
    ("West Point", "West Plains"),
    ("Windsor", "Winter Livestock - Dodge City, KS"),
    # "st" must not eat "sterling". NOT because the saint rule is \b-anchored
    # -- the table maps saint -> st and not the other way, so "sterling" is
    # never rewritten at all. See test_the_abbreviation_rules_are_word_anchored
    # for what the anchor actually buys.
    ("Sterling", "St. Onge"),
    # ft -> fort merges spellings of ONE place, not two places that share it
    ("Fort Worth", "Fort Pierre"),
    ("Ft. Worth", "Fort Collins"),
    ("Green", "Green City"),
    ("Union", "Unionville"),
])
def test_normalisation_keeps_apart(cme, ours):
    assert not covered_by(norm(cme), norm(ours))


def test_the_abbreviation_rules_are_word_anchored():
    """
    What the \b actually buys. "saint" is rare inside a word, so dropping its
    anchor changes nothing measurable -- but "ft" is not, and unanchored it
    rewrites the middle of ordinary words. Both directions pinned here so the
    anchor cannot be dropped as noise.
    """
    assert norm("Craft Livestock") == "craft livestock"      # not "crafort"
    assert norm("Clifton") == "clifton"                      # not "clifororton"
    assert norm("Sainte Genevieve") == "sainte genevieve"    # not "ste genevieve"
    assert norm("Ft. Pierre") == "fort pierre"
    assert norm("Saint Onge") == "st onge"


def test_normalisation_needs_the_word_boundary():
    """
    covered_by's added-words case appends a space on purpose. Without it any
    roster name would answer for every longer name that merely starts with its
    letters, and a real barn would go unreported.
    """
    assert not covered_by(norm("Greencastle"), norm("Green City"))
    assert not covered_by(norm("Adair"), norm("Ada"))


def test_napoleon_is_reached_by_its_city_and_not_by_its_title():
    """
    Replaces a parametrised case that asserted against
    "Napoleon Livestock Auction - Mandan, ND", a roster string that does not
    exist. The real entry is slug 2098, "Napoleon Livestock Auction -
    Napoleon, ND", and CME's "Napoleon" IS covered -- through the city, which
    is the point: the title alone would not reach it, because the truncation
    clause is off for every entry that has a city.
    """
    roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    entry, = [e for e in roster if e["slug_id"] == 2098]
    assert entry["city"] == "Napoleon"
    assert entry["title"] == "Napoleon Livestock Auction - Napoleon, ND"

    assert covered_by(norm("Napoleon"), norm(entry["city"]))
    assert not covered_by(norm("Napoleon"), norm(entry["title"]))
    assert roster_names()[norm(entry["title"])] is False    # not truncatable


def test_the_truncation_clause_is_what_reaches_mid_missouri():
    """
    Both halves of the one loose clause: it is needed, and it is off unless
    asked for.
    """
    cme, ours = norm("Mid Missouri Stockya"), norm(
        "Mid Missouri Stockyards Cattle Auction - Phillipsburg, MO")
    assert covered_by(cme, ours, truncatable=True)
    assert not covered_by(cme, ours)


@pytest.mark.parametrize("cme", ["M", "Mid", "Mid M", "Mid Misso"])
def test_the_truncation_clause_refuses_a_short_name(cme):
    """
    It used to accept all of these against the titleless Mid Missouri entry,
    "M" included -- so a new one- or two-word barn whose name happened to open
    the same way would have been absorbed in silence. MIN_TRUNCATION is 10 and
    the two names that need the clause are 20 and 21 characters.
    """
    ours = norm("Mid Missouri Stockyards Cattle Auction - Phillipsburg, MO")
    assert not covered_by(norm(cme), ours, truncatable=True)
    assert covered_by(norm("Mid Missou"), ours, truncatable=True)


def test_only_a_roster_entry_without_a_city_may_be_truncated():
    """
    If a second titleless entry is ever added, this fails and the risk in
    covered_by's third clause gets looked at again rather than inherited.
    """
    roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    titleless = {e["slug_id"] for e in roster if not e.get("city")}
    assert titleless == {3654}, f"new roster entries with no city: {titleless}"
    assert {n for n, trunc in roster_names().items() if trunc} == {
        norm("Mid Missouri Stockyards Cattle Auction - Phillipsburg, MO")}


def test_allowing_truncation_for_titled_entries_would_buy_nothing(conn):
    """
    THE QUESTION THE ft -> fort RULE RAISED. Roster slug 2021's title
    literally contains CME's "Ft. Pierre", and covered_by throws that away
    because the entry has a city. Should it?

    Measured over all 251 names CME has ever printed: allowing the clause
    everywhere changes not one answer. It is therefore free of benefit and not
    free of risk -- the second half shows it accepting a name that is not the
    barn, which is the Humeston failure mode exactly.
    """
    ours = roster_names()
    loose = {name for name, _ in cme_locations(conn).items()
             if name not in GONE and not is_aggregate(norm(name))
             and not any(covered_by(norm(name), o, True) for o in ours)}
    assert loose == set(uncovered(conn)), (
        "allowing truncation for titled roster entries changes the answer; "
        "the reasoning in roster_names() needs redoing, not inheriting")

    green_city = norm("Green City Livestock Auction - Green City, MO")
    assert covered_by(norm("Green City Liv"), green_city, truncatable=True)
    assert not covered_by(norm("Green City Liv"), green_city)


# --------------------------------------------------------------------------
# run it by hand: .venv/Scripts/python.exe tests/test_roster_covers_cme.py
# --------------------------------------------------------------------------

if __name__ == "__main__":
    with open_db() as c:
        found = uncovered(c, drop_gone=False)
    print(f"CME locations the roster cannot produce: {len(found)}")
    for name, (dates, head, first, last, states) in found.items():
        if name in GONE:
            note = f"GONE -- {GONE[name][1]}"
        else:
            note = OPEN_GAPS.get(name, "NEW -- written down nowhere")
        print(f"  {name!r:<26} {dates:>3} dates  {head:>7} hd  "
              f"{first}..{last}  [{states}]\n      {note}")
