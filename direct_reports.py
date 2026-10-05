"""
Parses USDA AMS "Direct Feeder Cattle Report" PDFs (per-state, published
weekly on Fridays) into the same qualifying-row shape update_index.py
already uses for sale-barn auction data.

THE CLAIM THAT USED TO SIT HERE WAS WRONG, and it cost something to find out.
It read: "These reports are NOT exposed as structured JSON via the MARS API
(only narrative text)". That is a true statement about ONE endpoint and a false
one about the API, and it was believed for long enough to shape a whole
analysis -- the heifer-share series was held to auction receipts alone partly on
the strength of it.

MARS serves these slugs in SECTIONS, unlike the auction slugs:

    GET /reports/1955                     auction   -> sectionNames []
    GET /reports/2710                     direct    -> ["Report Header",
                                                       "Report Details",
                                                       "Report Receipts"]

Calling the bare /reports/{slug} on a sectioned slug returns HTTP 200 with
narrative rows and NO head_count key -- exactly the impression recorded above,
and exactly what feeder_sex_mix.fetch_slug() would produce if pointed here.
The data is one path segment away:

    GET /reports/2710/Report%20Details?q=report_begin_date=2023-06-01:2023-06-30

which returns per-line-item rows carrying class, head_count, frame,
muscle_grade, weights and freight, weekly and unbroken from 2020-09-21 for
direct and 2020-05-06 for video. Date filtering is real: an out-of-range window
returns 200 with zero rows rather than silently falling back to the current
week.

THIS PARSER IS STILL THE RIGHT TOOL FOR THE CURRENT WEEK, which is what the
index needs, and the PDF remains the only place the weight-bracket price table
appears in its published form. The paragraph above is kept because the failure
mode generalises: an endpoint that answers 200 with plausible rows is not
evidence that it is the endpoint holding your data.

The tables have no visible ruling lines, so generic table-extraction
(pdfplumber's line/text strategies) doesn't work. This parses by clustering
pdfplumber's per-word x/y coordinates into rows and fixed column bands instead,
which reflects the PDF's real (invisible) grid.

CME's published methodology (cmegroup.com, confirmed against the workbook's
own Sheet1 note) requires, for direct/video/internet trade to qualify:
  - Quoted FOB with 3% standing shrink (or equivalent)
  - Pickup within 14 days -- so only "Current" (spot) delivery timing
    counts, not forward months ("Nov FOB", "Oct FOB", etc.)
  - Excludes dairy/exotic/Brahman-influenced and non-U.S.-origin cattle
  - 700-899 lb, Medium & Large Frame #1 or #1-2 Steers only (same as auctions)

Report date: CME's rule treats direct-trade reports as Friday sales, which
matches these reports' own "week ending <Friday>" framing -- so every
qualifying row from one report is stamped with that Friday's date.
"""
import io
import re
from datetime import date, timedelta

import pdfplumber
import requests

# state -> MARS slug_id / AMS report id (same numeric id used in both
# marsapi.ams.usda.gov and ams.usda.gov/mnreports/ams_<id>.pdf).
# WY-NE is one combined report, stored under state "WY" (NE's own share of
# the 12-state region is otherwise covered by the Wyoming-Nebraska report).
DIRECT_REPORT_SLUGS = {
    "CO": 2906,
    "IA": 3455,
    "KS": 3097,
    "MO": 2808,
    "MT": 2770,
    "NM": 2708,
    "OK": 3098,
    "SD": 3184,
    "TX": 2710,
    "WY": 3237,  # Wyoming-Nebraska Direct Cattle Report
}

REPORT_PDF_URL = "https://www.ams.usda.gov/mnreports/ams_{slug}.pdf"

SECTION_RE = re.compile(
    r"^(Steers|Heifers|Beef/Dairy Steers|Beef/Dairy Heifers) - Medium and Large (\d(?:-\d)?) \(Per Cwt\)$"
)
DATE_RE = re.compile(r"week ending (\d{1,2}/\d{1,2}/\d{4})")
TARGET_GRADES = {"1", "1-2"}

# A Delivery/Freight label is "<timing...> <basis>" -- "Current FOB",
# "Current DEL", "Oct DEL", "Oct - Nov FOB". The basis is always the LAST token
# and the timing always starts with one of these, which is what lets a real
# label be told from the page furniture that lands in the same column.
#
# WHY THAT MATTERS. cur_timing/cur_freight are STICKY -- a label applies to
# every weight row under it until the next label -- so anything that overwrites
# them silently drops cattle. "USDA AMS Livestock, Poultry & Grain Market News"
# and "Email us with accessibility issues with this report." both sit in the
# freight column on a page break and both used to be read as labels.
FREIGHT_BASES = {"FOB", "DEL"}
TIMINGS = {"Current", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
TARGET_BRACKETS = {700, 750, 800, 850}

# (column name, x0 lower bound, x0 upper bound) -- derived from inspecting
# actual word positions in several states' reports (TX/KS/MO/CO); consistent
# across states since these are the same auto-generated report template.
COLUMNS = [
    ("freight_label", 20, 100),
    ("head", 108, 142),
    ("wt_range", 160, 222),
    ("avg_wt", 244, 280),
    ("price_range", 296, 372),
    ("avg_price", 384, 428),
    ("notes", 460, 760),
]


def _assign_column(x0):
    for name, lo, hi in COLUMNS:
        if lo <= x0 < hi:
            return name
    return None


def _group_rows(words, tol=2.5):
    rows = {}
    for w in words:
        key = round(w["top"] / tol) * tol
        rows.setdefault(key, []).append(w)
    return [rows[k] for k in sorted(rows)]


def fetch_direct_pdf(state, timeout=30):
    slug = DIRECT_REPORT_SLUGS[state]
    resp = requests.get(
        REPORT_PDF_URL.format(slug=slug),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.content


def parse_direct_pdf(pdf_bytes, state):
    """
    Returns (report_date: date | None, rows: list[dict],
    unlabelled: list[dict]).

    UNLABELLED is the completeness check, and it is the reason this returns a
    third thing. A row excluded because its label says "Current DEL" or
    "Nov FOB" is an intentional exclusion; a row excluded because this parser
    could not work out what its label WAS is a parse failure wearing the same
    clothes, and on 2026-10-02 that silence cost 2,162 head of Texas Direct and
    put a number $1.82 wrong in front of clients.

    No threshold and nothing to calibrate -- it is the difference between "I
    know this row does not qualify" and "I do not know what this row is", which
    is exact. Measured over every direct state on 2026-10-05 it is empty, which
    is what makes a non-empty one worth stopping for.

    A STATISTICAL CHECK WAS TRIED FIRST AND DOES NOT WORK. Comparing each
    barn's pounds against its own 12-occurrence median, TX Direct that day was
    62% of normal -- utterly unremarkable, when the 25th percentile of all
    barn-days is 51% and barns routinely print at 2-3% of their median. Any
    cutoff that caught this one would have printed two or more lines of noise
    every sale day, and the module it would live in says plainly what happens
    then: "a census that prints two lines every morning gets ignored inside a
    week and then the real one is invisible."
    rows already match update_index.py's qualifying-row shape:
    class/frame/muscle_grade/weight_break_low/head_count/avg_weight/avg_price.
    report_date is None if the PDF's own date couldn't be parsed (caller
    should skip rather than guess).
    """
    rows = []
    unlabelled = []
    report_date = None
    cur_class = cur_grade = cur_timing = cur_freight = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if report_date is None:
                m = DATE_RE.search(text)
                if m:
                    mm, dd, yyyy = m.group(1).split("/")
                    report_date = date(int(yyyy), int(mm), int(dd))

            words = page.extract_words()
            for row_words in _group_rows(words):
                row_words.sort(key=lambda w: w["x0"])
                line = " ".join(w["text"] for w in row_words)

                m = SECTION_RE.match(line)
                if m:
                    # A section header REPEATED at the top of a page is a
                    # continuation, not a new group, and its first rows inherit
                    # the label from the previous page. Resetting there dropped
                    # 2,162 head of Texas Direct on 2026-10-02 -- the whole of a
                    # $1.79 error against CIH, on a number that had gone to
                    # clients. Reset only when the section genuinely changes; if
                    # the next row does carry its own label it overwrites this
                    # anyway, so inheriting costs nothing when it is wrong.
                    section = (m.group(1), m.group(2))
                    if section != (cur_class, cur_grade):
                        cur_timing = cur_freight = None
                    cur_class, cur_grade = section
                    continue
                if line.startswith("Delivery/Freight") or not cur_class:
                    continue

                cells = {}
                for w in row_words:
                    col = _assign_column(w["x0"])
                    if col:
                        cells.setdefault(col, []).append(w["text"])

                if cells.get("freight_label"):
                    label_tokens = " ".join(cells["freight_label"]).split()
                    # Both ends must look like a label, and the BASIS is the last
                    # token, not the second: "Oct - Nov FOB" is a four-token
                    # label whose basis is FOB. Reading token[1] made its basis
                    # "-", which happened to exclude the row for the wrong
                    # reason -- and would have included it had the dash ever
                    # been absent.
                    if (len(label_tokens) >= 2
                            and label_tokens[0] in TIMINGS
                            and label_tokens[-1] in FREIGHT_BASES):
                        cur_timing = " ".join(label_tokens[:-1])
                        cur_freight = label_tokens[-1]

                if "head" not in cells or "avg_wt" not in cells or "avg_price" not in cells:
                    continue
                try:
                    head = int(cells["head"][0].replace(",", ""))
                    avg_wt = float(cells["avg_wt"][0])
                    avg_price = float(cells["avg_price"][-1])
                except (ValueError, IndexError):
                    continue

                notes = " ".join(cells.get("notes", []))

                if cur_class != "Steers":
                    continue
                if cur_grade not in TARGET_GRADES:
                    continue
                bracket_now = int(avg_wt // 50 * 50)
                if cur_timing is None or cur_freight is None:
                    # Would have been judged on its label, and there is none to
                    # judge. Recorded rather than counted so the caller can name
                    # the row, because "something was dropped" sends a human
                    # back to the PDF and "2,162 head at 820 lb" does not.
                    if bracket_now in TARGET_BRACKETS:
                        unlabelled.append({
                            "muscle_grade": cur_grade,
                            "weight_break_low": bracket_now,
                            "head_count": head,
                            "avg_weight": avg_wt,
                            "avg_price": avg_price,
                        })
                    continue
                if cur_timing != "Current" or cur_freight != "FOB":
                    continue
                if "Mexican" in notes or "Origin" in notes:
                    continue
                bracket = int(avg_wt // 50 * 50)
                if bracket not in TARGET_BRACKETS:
                    continue

                rows.append({
                    "class": "Steers",
                    "frame": "Medium and Large",
                    "muscle_grade": cur_grade,
                    "weight_break_low": bracket,
                    "head_count": head,
                    "avg_weight": avg_wt,
                    "avg_price": avg_price,
                    "final_ind": "Final",
                })

    return report_date, rows, unlabelled


def fetch_all_direct_rows(states=None, verbose=True):
    """
    Pulls THIS WEEK's report for every state (these PDFs always show the
    current week -- there's no historical-date parameter, so this only
    extends the dataset forward from whenever it's first run, same as the
    original auction-data backfill's own limitation applies here too).
    Returns {state: (report_date, rows)}.
    """
    states = states or list(DIRECT_REPORT_SLUGS)
    out = {}
    for state in states:
        try:
            pdf_bytes = fetch_direct_pdf(state)
            report_date, rows, unlabelled = parse_direct_pdf(pdf_bytes, state)
        except Exception as e:
            if verbose:
                print(f"  [skip] {state} direct report: {e}")
            continue
        out[state] = (report_date, rows, unlabelled)
        if verbose:
            print(f"  {state} DIRECT  {report_date}  +{len(rows)} qualifying rows")
            # Loud, inline, and naming the rows. This is the line that would
            # have caught 2026-10-02 on the morning it happened.
            for u in unlabelled:
                print(f"    *** UNLABELLED ROW NOT INGESTED: {u['head_count']:,} head "
                      f"at {u['avg_weight']:.0f} lb, ${u['avg_price']:.2f}, "
                      f"grade {u['muscle_grade']}, bracket {u['weight_break_low']} "
                      f"-- the parser could not resolve its Delivery/Freight "
                      f"label, so it is NOT an intentional exclusion")
    return out
