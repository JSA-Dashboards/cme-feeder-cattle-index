"""
Parses USDA AMS "Feeder Cattle Internet & Video Reports" PDFs: Superior
Livestock Video/Internet Auction (Fort Worth, TX), by far the largest
video-auction platform (~200k head/week vs. a few thousand/week for the
whole sale-barn roster combined), plus Cattle Country Video (Torrington,
WY), CMS (Amarillo, TX), LiveAg (Fort Worth, TX), and Northern Livestock
(Billings, MT) -- confirmed 2026-08-29 to use the identical AMS report
template (same region headers, same weight-bracket table layout), so no
parser changes were needed to add them, just their slugs below. The
smaller per-city video add-ons listed on that same page (for cities
already in the auction roster -- Carthage MO, West Plains MO, Bassett/
Burwell/Crawford/Ericson/Valentine/Kearney NE, Apache/Beaver OK, Wildorado
TX, a 2nd Billings MT company) were checked too and skipped: their MARS
API rows are narrative-only stubs (all fields None, same limitation the
Direct Cattle Reports had before PDF-parsing), and spot-checking their
actual PDFs showed tiny, often non-feeder-steer volume (e.g. West Plains'
report that week was a 90-head bred-heifer replacement sale in the
Southeast region) -- not worth building out unless one of them turns out
to matter later. Western Video Market and Overland/Producers (CA/FL) are
outside the 12-state region, not relevant regardless.

Same position-based parsing technique as direct_reports.py (no visible
table structure -- clusters pdfplumber word coordinates into rows/columns),
different column x-bands (this report's own template).

Two things distinguish this report family from direct_reports.py's:
  - REGION-based state attribution, not per-report state: one PDF covers
    the whole country in named regions. Only "North Central" (CO, IA, MT,
    ND, NE, SD, WY) and "South Central" (KS, MO, NM, OK, TX) sections fall
    in the CME 12-state region -- together they're an exact match, no
    partial-region ambiguity. Southeast/Northeast/West sections are
    skipped entirely.
  - No separate FOB/DEL freight column -- the whole report's narrative
    states prices are FOB-quoted uniformly, so only delivery TIMING needs
    filtering (only "Current" qualifies, matching CME's 14-day pickup
    rule -- confirmed by this report's own narrative explicitly defining
    "Current delivery" as the 14-day window from the video's last day).

Report date: "Livestock Weighted Average Report for <start> - <end> (Final)"
-- stamped with the end date, matching CME's "final day" convention for
multi-day sales without separate daily reports.
"""
import io
import re
from datetime import date

import pdfplumber
import requests

VIDEO_REPORT_SLUGS = {
    "SUPERIOR": 2713,  # Superior Livestock Video/Internet Auction - Fort Worth, TX (Mon)
    "CATTLE_COUNTRY": 3241,  # Cattle Country Livestock Video/Internet Auction - Torrington, WY (Monthly)
    "CMS": 3907,  # CMS Video/Internet Livestock Auction - Amarillo, TX (Monthly)
    "LIVEAG": 3892,  # LiveAg Video Auction - Fort Worth, TX (Monthly)
    "NORTHERN_LIVESTOCK": 2772,  # Northern Livestock Video/Internet Auction - Billings, MT (Seasonal)
    "CATTLE_DRIVE": 3791,  # Cattle Drive Livestock Video/Internet Auction - Salina, UT (Tue)
    # Based in UT (outside the 12-state region) but its own sales are
    # region-tagged like the others, and CME's real published report does
    # include it under "North Central" -- confirmed via a live Compass Ag
    # report showing a "CATTLE DRIVE (NC)" line item. Frequently reports
    # zero qualifying feeder-steer rows (many weeks are slaughter cows /
    # replacement cattle only), same as CMS.
    "WESTERN_VIDEO": 3242,  # Western Video Market Livestock Video/Internet Auction - Cottonwood, CA
    # Also outside the region (CA), but same "don't judge by HQ state" logic
    # as Cattle Drive -- its report covers regional sales (e.g. a Wyoming
    # sale) under the same North Central/South Central headers. Uses a
    # GENUINELY DIFFERENT report template than the other 5 (which all share
    # Superior's exact layout) -- continuous per-delivery-month price lists
    # under class/frame/grade section headers, not fixed 50lb brackets, and
    # a delivery label that's often omitted on a row (continuation of the
    # last one seen). Needs its own parser -- see parse_western_video_pdf().
}

REPORT_PDF_URL = "https://www.ams.usda.gov/mnreports/ams_{slug}.pdf"

REGION_STATES = {
    "North Central": {"CO", "IA", "MT", "ND", "NE", "SD", "WY"},
    "South Central": {"KS", "MO", "NM", "OK", "TX"},
    # Southeast / Northeast / West / other regions: outside the CME
    # 12-state sample, intentionally not mapped -- rows under them are
    # skipped (no entry in REGION_STATES to attribute them to).
}
QUALIFYING_REGIONS = set(REGION_STATES)

SECTION_RE = re.compile(
    r"^(Steers|Heifers|Beef/Dairy Steers|Beef/Dairy Heifers) - Medium and Large (\d(?:-\d)?) "
    r"\(Per Cwt",
    re.IGNORECASE,
)
DATE_RANGE_RE = re.compile(
    r"Weighted Average Report for (\d{1,2}/\d{1,2}/\d{4})(?:\s*-\s*(\d{1,2}/\d{1,2}/\d{4}))?"
)
TARGET_GRADES = {"1", "1-2"}
TARGET_BRACKETS = {700, 750, 800, 850}

# Derived from inspecting actual word positions in the Superior report.
COLUMNS = [
    ("delivery", 25, 95),
    ("head", 98, 125),
    ("wt_range", 150, 222),
    ("avg_wt", 222, 260),
    ("price_range", 275, 350),
    ("avg_price", 360, 405),
    ("notes", 445, 760),
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


def _parse_report_date(text):
    m = DATE_RANGE_RE.search(text)
    if not m:
        return None
    end = m.group(2) or m.group(1)
    mm, dd, yyyy = end.split("/")
    return date(int(yyyy), int(mm), int(dd))


def fetch_video_pdf(name, timeout=60):
    slug = VIDEO_REPORT_SLUGS[name]
    resp = requests.get(
        REPORT_PDF_URL.format(slug=slug),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.content


def parse_video_pdf(pdf_bytes):
    """
    Returns (report_date: date | None, rows: list[dict]) where each row also
    carries a 'state' key set to the specific state's 2-letter code the
    surrounding region maps to would be ambiguous (video sales aren't
    attributed to one state within a region) -- so 'state' is set to the
    region name itself (e.g. "North Central") for a synthetic per-region
    location tag, not a single state. Rows already match update_index.py's
    qualifying-row shape otherwise.
    """
    rows = []
    report_date = None
    cur_region = None
    cur_class = cur_grade = cur_timing = None
    in_qualifying_region = False

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if report_date is None:
                report_date = _parse_report_date(text)

            words = page.extract_words()
            for row_words in _group_rows(words):
                row_words.sort(key=lambda w: w["x0"])
                line = " ".join(w["text"] for w in row_words)

                if line.strip() in REGION_STATES or line.strip() in (
                    "Southeast", "Northeast/Upper Midwest (Great Lakes)", "West",
                    "North Central", "South Central",
                ):
                    region_name = line.strip()
                    if region_name in QUALIFYING_REGIONS:
                        cur_region = region_name
                        in_qualifying_region = True
                    else:
                        cur_region = None
                        in_qualifying_region = False
                    cur_class = cur_grade = cur_timing = None
                    continue

                m = SECTION_RE.match(line)
                if m:
                    cur_class = m.group(1).title().replace("Beef/Dairy", "Beef/Dairy ").replace("  ", " ").strip()
                    cur_grade = m.group(2)
                    cur_timing = None
                    continue

                if not in_qualifying_region or line.startswith("Delivery") or not cur_class:
                    continue

                cells = {}
                for w in row_words:
                    col = _assign_column(w["x0"])
                    if col:
                        cells.setdefault(col, []).append(w["text"])

                if cells.get("delivery"):
                    cur_timing = cells["delivery"][0]

                if "head" not in cells or "avg_wt" not in cells or "avg_price" not in cells:
                    continue
                try:
                    head = int(cells["head"][0].replace(",", ""))
                    avg_wt = float(cells["avg_wt"][0])
                    avg_price = float(cells["avg_price"][-1])
                except (ValueError, IndexError):
                    continue

                notes = " ".join(cells.get("notes", []))

                if "Steers" not in cur_class or "Beef/Dairy" in cur_class:
                    continue
                if cur_grade not in TARGET_GRADES:
                    continue
                if cur_timing != "Current":
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
                    "region": cur_region,
                })

    return report_date, rows


_WV_SECTION_RE = re.compile(
    r"^(STEERS|HEIFERS|DAIRY STEERS|DAIRY HEIFERS|BEEF/DAIRY STEERS|BEEF/DAIRY HEIFERS) - "
    r"(Medium and Large 1-2|Medium and Large 1|Large 1-2|Large 1|Medium 1-2|Medium 1) "
    r"\(Per (?:Cwt|Head)",
)
_WV_DELIVERY_RE = re.compile(r"^(Current|[A-Za-z]{3}(?:-[A-Za-z]{3})?)\s+(\d.*)$")
_WV_GRADE_MAP = {"Medium and Large 1": "1", "Medium and Large 1-2": "1-2"}


def _wv_consume_value_group(tokens, i):
    """
    A "Wt Range"/"Price Range" column is either one bare number (the row's
    own average, appearing twice -- e.g. "780 780") or a "low - high" range
    followed by the average as a 4th token (e.g. "900 - 940 921"). Returns
    (avg_value_str, next_index).
    """
    if i + 1 < len(tokens) and tokens[i + 1] == "-":
        return tokens[i + 3], i + 4
    return tokens[i + 1], i + 2


def parse_western_video_pdf(pdf_bytes):
    """
    Western Video Market's report shares CME's/AMS's region convention
    (North Central/South Central qualify, same 12-state coverage as every
    other video report) but is otherwise a different template from
    Superior's -- see the VIDEO_REPORT_SLUGS comment for why. Continuous
    per-delivery-month price lists under class/frame/grade section headers
    (e.g. "STEERS - Medium and Large 1 (Per Cwt / Est. Wt )"), plain text
    (no fixed word-coordinate columns to exploit the way Superior's report
    has), and a delivery-month label that's frequently omitted on a row --
    it then continues whatever label the last row in this SAME section
    carried (a flattened rowspan). Only "Current" delivery rows qualify
    (CME's 14-day pickup rule, same as every other source).
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    report_date = _parse_report_date(full_text)

    rows = []
    cur_region = None
    cur_class = cur_grade = None
    cur_delivery = None  # carries forward across rows within one section

    for line in full_text.splitlines():
        stripped = line.strip()

        if stripped in REGION_STATES or stripped in ("Southeast", "West", "North Central", "South Central"):
            cur_region = stripped if stripped in QUALIFYING_REGIONS else None
            cur_class = cur_grade = cur_delivery = None
            continue

        if stripped == "REPLACEMENT CATTLE":
            cur_region = None  # only FEEDER CATTLE rows qualify; stop until next region header
            continue

        m = _WV_SECTION_RE.match(stripped)
        if m:
            cur_class, descriptor = m.group(1), m.group(2)
            cur_grade = _WV_GRADE_MAP.get(descriptor)  # None for Large-only/Medium-only -- correctly excluded
            cur_delivery = None
            continue

        if not cur_region or cur_class != "STEERS" or cur_grade not in TARGET_GRADES:
            continue
        if stripped.startswith("Delivery") or not stripped or stripped[0].isalpha() and not _WV_DELIVERY_RE.match(stripped):
            continue

        dm = _WV_DELIVERY_RE.match(stripped)
        if dm:
            cur_delivery = dm.group(1)
            data = dm.group(2)
        else:
            data = stripped

        if cur_delivery != "Current":
            continue

        tokens = data.split()
        try:
            head = int(tokens[0].replace(",", ""))
            avg_wt_str, price_start = _wv_consume_value_group(tokens, 1)
            avg_price_str, note_start = _wv_consume_value_group(tokens, price_start)
            avg_wt = float(avg_wt_str.replace(",", ""))
            avg_price = float(avg_price_str.replace(",", ""))
        except (ValueError, IndexError):
            continue

        notes = " ".join(tokens[note_start:])
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
            "region": cur_region,
        })

    return report_date, rows


_PARSERS = {"WESTERN_VIDEO": parse_western_video_pdf}


def fetch_all_video_rows(verbose=True):
    """Returns {name: (report_date, rows)} for every configured video report."""
    out = {}
    for name in VIDEO_REPORT_SLUGS:
        parser = _PARSERS.get(name, parse_video_pdf)
        try:
            pdf_bytes = fetch_video_pdf(name)
            report_date, rows = parser(pdf_bytes)
        except Exception as e:
            if verbose:
                print(f"  [skip] {name} video report: {e}")
            continue
        out[name] = (report_date, rows)
        if verbose:
            print(f"  {name} VIDEO  {report_date}  +{len(rows)} qualifying rows")
    return out
