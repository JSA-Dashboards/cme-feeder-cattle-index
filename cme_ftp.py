"""
Parses CME's own official daily Feeder Cattle Index calculation files, served
from a public anonymous FTP server:

    ftp://ftp.cmegroup.com/cash_settled_commodity_index_prices/daily_data/feeder_cattle/

Each file is CME's literal settlement-calculation input for one date: every
qualifying location's per-weight-bracket head/weight/price detail, a same-day
"DAILY TOTALS" row, a rolling "SEVEN-DAY TOTALS" row, and the official
"REPORTED INDEX" value CME actually published that day. This is not a
reconstruction or approximation -- it's the exact source data, so there's no
accuracy gap to close and no need to independently pull/filter/aggregate raw
USDA-AMS reports the way the old MARS-based pipeline did.

File layout: the current calendar year's files sit loose in the root
directory (e.g. FC260828.txt); every prior full year is archived under its
own subfolder (e.g. 2024/FC240815.txt). Filenames encode the file's own date
as FC + YYMMDD + .txt.

Format notes (reverse-engineered, not documented by CME):
  - Fixed-width columns. The "Sale Location"/"Stat[e]" prefix's exact
    character width has drifted slightly across years (found 27 chars in a
    2026 sample, 29 chars in a 2024 sample) -- this module derives it fresh
    per file from that file's own first data row rather than hardcoding it.
  - A location's weight/price fields are sometimes jammed together with no
    separating space when the weighted-average weight has a fractional part
    (e.g. "713.35217.51" = weight 713.35, price 217.51) -- both halves are
    consistently exactly 6 characters (DDD.DD), so a targeted regex splits
    any run of two adjacent 6-char decimal groups before whitespace
    tokenizing. Validated against multiple files: recomputing each location's
    own head/weight/dollars from its 8 bracket triples reproduces that row's
    own printed totals (to sub-0.001% floating rounding).
  - Weight-bracket definitions have changed over the index's history (a 2015
    sample used 650-899 lb brackets, not today's 700-899) -- this module
    doesn't care, since it only ever reads CME's own already-computed
    per-location totals and REPORTED INDEX, never recomputes from brackets.
  - A location row's own "Sale Date" can differ from the file's overall date
    (e.g. a Saturday-only auction still shows its true Saturday date inside
    a Monday file) -- CME's own confirmation that per-location display should
    use the true sale date while the file's own date governs the rolling
    window, matching this project's raw_date/report_date split.
  - Rows with no qualifying sale that location that day are sometimes blank
    (no "0" placeholders at all) rather than zero-filled -- these produce
    only 5 trailing summary tokens instead of 29 and are treated as a
    zero-head row, not a parse error.
"""
import ftplib
import time
import io
import re
import zlib
from datetime import date, timedelta

import snowflake_db as db

FTP_HOST = "ftp.cmegroup.com"
FTP_BASE = "cash_settled_commodity_index_prices/daily_data/feeder_cattle"

_DATE_ROW_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{2}\s")
_STATE_RE = re.compile(r"([A-Z]{1,2})\s+(-?\d)")
_CONCAT_RE = re.compile(r"(\d+\.\d{2})(?=\d+\.\d{2})")
_TOKEN_RE = re.compile(r"-?\d+\.?\d*")

# Real 2-letter state codes -- anything else in the state slot (a video
# report's truncated region code, e.g. "N"/"S") gets expanded via this map.
_REGION_MAP = {"N": "North Central", "S": "South Central"}


def file_path_for(d: date) -> str:
    fname = f"FC{d.strftime('%y%m%d')}.txt"
    today = date.today()
    if d.year < today.year:
        return f"{FTP_BASE}/{d.year}/{fname}"
    return f"{FTP_BASE}/{fname}"


class FetchFailed(RuntimeError):
    """
    The file could not be retrieved for a reason that is NOT "it does not
    exist" -- a transient block, a timeout, a reset. Distinct from None,
    which means the server told us the path is genuinely absent.
    """


# Substrings that mark an ftplib.error_perm as "the path is not there"
# rather than "something else went wrong".
_MISSING_MARKERS = ("550", "no such file", "failed to open", "not found",
                    "cannot find")


def fetch_daily_file(d: date, timeout=20, attempts=3) -> str | None:
    """
    Returns the raw file text, or None if that date GENUINELY has no file
    (weekend/holiday/not yet published/before the archive's coverage starts).
    Raises FetchFailed for any other failure.

    This distinction used to be absent: every ftplib error was swallowed
    into None, so a transient block read exactly like an absent file, and
    backfill_ftp.py reported both as "no file (weekend/holiday/unpublished)".
    On 2026-09-09 that made the pipeline report CME as not having published
    2026-09-07 when FC260907.txt had been on the server since 09-08 14:05.
    Reporting a fetch failure as a non-publication is the worst possible
    way to be wrong here, because it looks like news about CME.

    Uses ftplib directly with a fresh connection per call rather than
    urllib's ftp:// support -- urllib caches FTP control connections keyed
    only by (user, passwd, host, port), with no path component, and once
    that cached connection goes bad (confirmed happens under any repeated
    same-process use, not just concurrency) every subsequent fetch in that
    process silently fails by reusing the same broken connection. A fresh
    ftplib connection per call sidesteps that entirely.
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            ftp = ftplib.FTP(FTP_HOST, timeout=timeout)
            try:
                ftp.login()
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {file_path_for(d)}", buf.write)
                content = buf.getvalue()
            finally:
                ftp.close()
            if not content:
                return None
            return content.decode("utf-8", errors="replace")
        except ftplib.error_perm as e:
            # A permanent refusal. If it names a missing path, the date
            # simply has no file; anything else is a real problem.
            if any(mark in str(e).lower() for mark in _MISSING_MARKERS):
                return None
            last_err = e
        except (*ftplib.all_errors, OSError) as e:
            last_err = e
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    raise FetchFailed(
        "%s: %s: %s" % (file_path_for(d), type(last_err).__name__, last_err))


def _tokenize(blob: str) -> list[str]:
    return _TOKEN_RE.findall(_CONCAT_RE.sub(r"\1 ", blob))


def _mdy_to_iso(mdy: str, fallback_year: int) -> str:
    parts = mdy.split("/")
    m, d = int(parts[0]), int(parts[1])
    y = int(parts[2]) if len(parts) > 2 else fallback_year
    if y < 100:
        y += 2000
    return date(y, m, d).isoformat()


def _parse_reported(text: str) -> tuple:
    """
    REPORTED INDEX / REPORTED CHANGE. Neither needs column arithmetic, so
    this works on any file including one with no sales at all.

    The change label is sometimes truncated to "REPORTED CHANG" -- observed
    in FC260907.txt -- so the trailing E is optional. Requiring the full
    spelling silently returned None for the change on those files.
    """
    idx = chg = None
    for ln in text.splitlines():
        m = re.search(r"REPORTED INDEX\s+(-?[\d.]+)", ln)
        if m:
            idx = float(m.group(1))
        m = re.search(r"REPORTED CHANGE?\s+(-?[\d.]+)", ln)
        if m:
            chg = float(m.group(1))
    return idx, chg


def _parse_totals_only(text: str, file_date: date) -> dict:
    """
    Parse a file that has NO location rows -- a day on which no qualifying
    sale was reported anywhere, so every location line is blank.

    parse_daily_file() derives its fixed-width layout from the first data
    row's state code, and such a file has none, so it cannot proceed. This
    is the one case that defeats the width derivation, and also the case
    that needs it least: the seven-day totals and the REPORTED INDEX are
    the only numbers present and neither depends on column positions.

    Observed on FC260907.txt (Labor Day 2026): zero daily head, seven-day
    totals of 10,786 head, REPORTED INDEX 326.98. Before this the file
    parsed to None and was recorded as "no file", i.e. as CME not having
    published at all.

    Slashes are the row's filler; replacing them with spaces lets the
    trailing five figures tokenise without any width assumption.
    """
    daily = seven_day = None
    for ln in text.splitlines():
        if "TOTALS" not in ln:
            continue
        toks = _tokenize(ln.replace("/", " "))
        if len(toks) < 5:
            continue
        head, _w_lbs, avg_w, _dollars, avg_p = (float(t) for t in toks[-5:])
        agg = {"head": int(head), "avg_weight": avg_w, "avg_price": avg_p}
        if "SEVEN-DAY" in ln:
            seven_day = agg
        elif "DAILY" in ln:
            daily = agg
    idx, chg = _parse_reported(text)
    return {
        "date": file_date.isoformat(),
        "reported_index": idx,
        "reported_change": chg,
        "daily": daily,
        "seven_day": seven_day,
        "locations": [],
    }


def parse_daily_file(text: str, file_date: date) -> dict | None:
    """
    Returns {
      "date": iso date string (the file's own date),
      "reported_index": float or None,
      "reported_change": float or None,
      "daily": {"head": int, "avg_weight": float, "avg_price": float} or None,
      "seven_day": {"head": int, "avg_weight": float, "avg_price": float} or None,
      "locations": [{"raw_date": iso str, "location": str, "state": str,
                     "head": int, "avg_weight": float, "avg_price": float}, ...]
    } or None if the file doesn't look like a valid daily calculation file.
    """
    lines = text.splitlines()
    data_lines = [ln for ln in lines if _DATE_ROW_RE.match(ln)]
    if not data_lines:
        return None

    # Derive the location/state prefix width fresh from this file: the first
    # data row's own state-code match tells us exactly where it ends.
    prefix_len = None
    for ln in data_lines:
        m = _STATE_RE.search(ln)
        if m:
            prefix_len = m.end(1)
            break
    if prefix_len is None:
        # No state code anywhere: a day with no qualifying sales at all.
        return _parse_totals_only(text, file_date)

    locations = []
    daily = seven_day = None
    for ln in data_lines:
        prefix, blob = ln[:prefix_len], ln[prefix_len:]
        toks = _tokenize(blob)
        is_totals = "TOTALS" in prefix
        if len(toks) == 5:
            head, w_lbs, avg_w, dollars, avg_p = (float(t) for t in toks)
        elif len(toks) == 29:
            head, w_lbs, avg_w, dollars, avg_p = (float(t) for t in toks[24:29])
        else:
            continue  # unparseable row -- skip rather than guess

        if is_totals:
            agg = {"head": int(head), "avg_weight": avg_w, "avg_price": avg_p}
            if "SEVEN-DAY" in prefix:
                seven_day = agg
            elif "DAILY" in prefix:
                daily = agg
            continue

        # Search the FULL line (not the prefix slice) -- slicing at
        # prefix_len can cut off the trailing digit this pattern needs to
        # complete its match, since prefix_len itself was derived from an
        # end() position that lands mid-match on some rows.
        m = _STATE_RE.search(ln)
        if not m:
            continue
        # Some files pad a single-digit day (or seemingly at random) with a
        # leading space on data rows -- match it as part of the date token
        # so the length used to slice out loc_text stays correct, but strip
        # it before actually parsing the date.
        date_m = re.match(r"^\s*\S+", ln)
        sale_date_str = date_m.group(0) if date_m else ""
        # location text sits between the sale-date token and the state match
        loc_text = ln[len(sale_date_str):m.start(1)].strip()
        state_raw = m.group(1)
        state = _REGION_MAP.get(state_raw, state_raw)
        if head <= 0:
            continue
        try:
            raw_date = _mdy_to_iso(sale_date_str.strip(), file_date.year)
        except Exception:
            raw_date = file_date.isoformat()
        locations.append({
            "raw_date": raw_date,
            "location": loc_text.title(),
            "state": state,
            "head": int(head),
            "avg_weight": avg_w,
            "avg_price": avg_p,
        })

    reported_index, reported_change = _parse_reported(text)

    return {
        "date": file_date.isoformat(),
        "reported_index": reported_index,
        "reported_change": reported_change,
        "daily": daily,
        "seven_day": seven_day,
        "locations": locations,
    }


def location_slug_id(location: str) -> int:
    """Stable synthetic id (not a real MARS slug) -- only needed so the
    mars_sales table's PK stays unique; CME's own files don't expose one."""
    return zlib.crc32(location.encode("utf-8"))


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def init_official_tables(conn):
    """
    Separate from mars_sales/fci_daily (the MARS-based ESTIMATE pipeline,
    still needed for the trailing 1-3 business days CME hasn't published a
    file for yet). These tables hold CME's own exact, already-official
    numbers -- once a date has a row here, app.py should prefer it over any
    estimate for the same date rather than merge the two.
    """
    # Schema lives in snowflake/01_schema.sql, not provisioned at runtime.
    if db.use_snowflake():
        return
    # WAL mode lets Streamlit (or anything else) keep reading the DB while
    # a long-running backfill or the daily scheduled update writes to it --
    # sqlite's default rollback-journal locking otherwise blocks all reads
    # for as long as a write transaction stays open.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cme_ftp_daily (
            report_date TEXT PRIMARY KEY,
            fci_value REAL NOT NULL,
            reported_change REAL,
            n_locations INTEGER NOT NULL,
            total_head INTEGER,
            same_day_price REAL,
            same_day_head INTEGER,
            same_day_avg_weight REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cme_ftp_locations (
            report_date TEXT NOT NULL,
            location TEXT NOT NULL,
            state TEXT NOT NULL,
            head_count INTEGER NOT NULL,
            avg_weight REAL NOT NULL,
            avg_price REAL NOT NULL,
            PRIMARY KEY (report_date, location)
        )
    """)
    conn.commit()


def ingest_range(conn, start: date, end: date, verbose=True):
    """
    Fetches and stores every available official daily file in [start, end].
    Dates CME hasn't published yet (or never will -- weekends, holidays)
    simply return None from fetch_daily_file and are skipped; this is
    expected, not an error, and callers shouldn't treat a gap as failure.
    Returns (n_days_ingested, n_days_missing).
    """
    init_official_tables(conn)
    ingested = missing = 0
    for d in daterange(start, end):
        text = fetch_daily_file(d)
        if text is None:
            missing += 1
            continue
        parsed = parse_daily_file(text, d)
        if parsed is None or parsed["reported_index"] is None:
            missing += 1
            continue
        daily = parsed["daily"] or {}
        seven = parsed["seven_day"] or {}
        db.merge_replace(
            conn, "cme_ftp_daily",
            ["report_date", "fci_value", "reported_change", "n_locations", "total_head",
             "same_day_price", "same_day_head", "same_day_avg_weight"],
            (parsed["date"], parsed["reported_index"], parsed["reported_change"],
             len(parsed["locations"]), seven.get("head"),
             daily.get("avg_price"), daily.get("head"), daily.get("avg_weight")),
            ["report_date"],
        )
        # Keyed by each row's own raw_date (which can differ from this
        # file's date -- a weekend carry-forward row keeps its true sale
        # date), so this upsert alone keeps re-ingestion idempotent without
        # needing a delete-by-file-date pass first.
        for loc in parsed["locations"]:
            db.merge_replace(
                conn, "cme_ftp_locations",
                ["report_date", "location", "state", "head_count", "avg_weight", "avg_price"],
                (loc["raw_date"], loc["location"], loc["state"], loc["head"],
                 loc["avg_weight"], loc["avg_price"]),
                ["report_date", "location"],
            )
        ingested += 1
        if verbose and ingested % 50 == 0:
            print(f"  ...{ingested} days ingested (through {d.isoformat()})")
    conn.commit()
    return ingested, missing
