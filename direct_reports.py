"""
Parses USDA AMS "Direct Feeder Cattle Report" PDFs (per-state, published
weekly on Fridays) into the same qualifying-row shape update_index.py
already uses for sale-barn auction data.

These reports are NOT exposed as structured JSON via the MARS API (only
narrative text) -- the real weight-bracket price tables only exist in the
PDF, without visible ruling lines, so generic table-extraction (pdfplumber's
line/text strategies) doesn't work. This parses by clustering pdfplumber's
per-word x/y coordinates into rows and fixed column bands instead, which
reflects the PDF's real (invisible) grid.

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
    Returns (report_date: date | None, rows: list[dict]).
    rows already match update_index.py's qualifying-row shape:
    class/frame/muscle_grade/weight_break_low/head_count/avg_weight/avg_price.
    report_date is None if the PDF's own date couldn't be parsed (caller
    should skip rather than guess).
    """
    rows = []
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
                    cur_class, cur_grade = m.group(1), m.group(2)
                    cur_timing = cur_freight = None
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
                    if len(label_tokens) >= 2:
                        cur_timing, cur_freight = label_tokens[0], label_tokens[1]

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

    return report_date, rows


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
            report_date, rows = parse_direct_pdf(pdf_bytes, state)
        except Exception as e:
            if verbose:
                print(f"  [skip] {state} direct report: {e}")
            continue
        out[state] = (report_date, rows)
        if verbose:
            print(f"  {state} DIRECT  {report_date}  +{len(rows)} qualifying rows")
    return out
