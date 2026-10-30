"""
CME's yearly index workbooks -- a RESTATED archive, not the published scorecard.

WHAT THIS IS NOT, first, because the whole risk here is one column being
mistaken for another.

`cme_ftp_daily.fci_value` is the REPORTED INDEX CME printed on the day. It is
the independent number this project's estimate is graded against, and its
entire value is that nobody here computed it.

These workbooks carry something else. Validated 2026-10-30 by parsing FC2015
-- a year we already hold as-printed -- and comparing:

    head counts   261 of 261 dates agree exactly
    index value   worst difference 0.5382; several dates out by 0.01 to 0.54

and CME's reported index appears NOWHERE in the workbook. On 2015-01-08 we
hold 235.22; the workbook's seven-day weighted average is 235.2783, and 235.22
is not in that row at all. So the workbook publishes a computed weighted
average, which is a different quantity from the figure CME reported, and the
two have drifted apart in the restating.

Hence a separate table and deliberately different column names. Nothing here
is called `fci_value`, so no query can quietly treat it as the scorecard.

WHY IT IS WORTH HAVING ANYWAY. The as-printed daily archive floors at 2015 --
CME's year folders run 2015 through 2025, and the loose root directory holds
only the current year. 2013 and 2014 as-printed do not exist to be had. These
workbooks are the only source for them, and for long-horizon work where a
difference of a few hundredths is immaterial, two extra years of
location-level detail is worth more than the discrepancy costs.

THE ARCHIVES FAIL CRC AS CME PUBLISHES THEM. Downloaded sizes match the
server's own SIZE byte for byte, and the deflate stream decompresses cleanly,
so the stored CRC simply disagrees with its own payload -- CME's metadata is
wrong, the data is not. zipfile refuses to read it; this module inflates the
member directly and judges the content instead, which is the check that
matters anyway.

xlrd IS NOT A PIPELINE DEPENDENCY. Nothing in the daily job reads a workbook,
so it is imported lazily and stays out of requirements.txt. Run the backfill
with a throwaway environment:

    uv run --with xlrd python backfill_cme_archive.py --years 2013 2014
"""

import ftplib
import io
import os
import struct
import zipfile
import zlib
from datetime import date, timedelta

#: Where the yearly workbooks live. The as-printed daily files are elsewhere
#: (daily_data/feeder_cattle, year subfolders from 2015) and mean something
#: different -- see this module's docstring.
ARCHIVE_DIR = "cash_settled_commodity_index_prices/historical_data"
FTP_HOST = "ftp.cmegroup.com"

#: Excel's serial origin on Windows. 1899-12-30 rather than 12-31 because of
#: the 1900 leap-year bug Excel preserves for compatibility; getting this
#: wrong shifts every date by one and the result still looks plausible.
EXCEL_EPOCH = date(1899, 12, 30)

#: The workbook marks its aggregate rows in the LOCATION column.
DAILY_MARK = "DAILY TOTALS"
SEVEN_MARK = "SEVEN-DAY"


class ArchiveError(RuntimeError):
    """The archive could not be fetched, inflated or read."""


def archive_name(year: int) -> str:
    return "FC%d.ZIP" % year


def excel_date(serial) -> date:
    """Excel serial to a real date, or None if it is not a number."""
    try:
        n = int(float(serial))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return EXCEL_EPOCH + timedelta(days=n)


def fetch(year: int, timeout: int = 60) -> bytes:
    """The raw .ZIP bytes for a year, straight from CME."""
    buf = io.BytesIO()
    try:
        ftp = ftplib.FTP(FTP_HOST, timeout=timeout)
        try:
            ftp.login()
            ftp.retrbinary("RETR %s/%s" % (ARCHIVE_DIR, archive_name(year)),
                           buf.write)
        finally:
            ftp.close()
    except ftplib.all_errors as e:
        raise ArchiveError("%s: %s" % (archive_name(year), e)) from e
    if not buf.getvalue():
        raise ArchiveError("%s: empty" % archive_name(year))
    return buf.getvalue()


def inflate_only_member(zip_bytes: bytes) -> bytes:
    """The single member, decompressed WITHOUT the CRC check.

    zipfile refuses these archives: CME's stored CRC disagrees with its own
    payload. The download matches the server's SIZE exactly and the deflate
    stream decompresses cleanly, so the metadata is wrong rather than the
    data. Reading the stream directly is the only way in, and the content is
    then checked on its own merits by the caller.
    """
    try:
        names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
    except zipfile.BadZipFile as e:
        raise ArchiveError("not a zip: %s" % e) from e
    if len(names) != 1:
        raise ArchiveError("expected one member, found %d: %s" % (len(names), names))

    info = zipfile.ZipFile(io.BytesIO(zip_bytes)).getinfo(names[0])
    if info.compress_type != zipfile.ZIP_DEFLATED:
        raise ArchiveError("member is not deflated (method %d)" % info.compress_type)

    fh = io.BytesIO(zip_bytes)
    fh.seek(info.header_offset)
    head = fh.read(30)
    sig, _v, _f, _m, _t, _d, _crc, _cs, _us, nlen, elen = \
        struct.unpack("<IHHHHHIIIHH", head)
    if sig != 0x04034B50:
        raise ArchiveError("bad local file header at offset %d" % info.header_offset)
    fh.seek(nlen + elen, os.SEEK_CUR)
    try:
        return zlib.decompressobj(-15).decompress(fh.read(info.compress_size))
    except zlib.error as e:
        raise ArchiveError("inflate failed: %s" % e) from e


def _columns(sheet):
    """{HEADER: column index}, keeping the FIRST column of each name.

    THE WORKBOOKS REUSE HEADER NAMES. FC2014 carries a second block from
    column 34 -- "Location", "Report Date", "Class Description", "Head Count",
    "Average Price" -- alongside the main one, so "LOCATION" appears at both
    column 1 and column 34.

    A dict comprehension over enumerate() keeps the LAST of a repeated key,
    which pointed the parser at column 34 and made it read "Dunlap, IA" where
    a barn name belonged. It then matched no DAILY TOTALS row, found no dated
    aggregates, and reported the year as empty -- a clean, silent, plausible
    nothing. FC2013 and FC2015 have no duplicate and were unaffected, which is
    exactly how a bug like this survives: it breaks one year out of three and
    the other two look like proof it works.
    """
    out = {}
    for i in range(sheet.ncols):
        name = str(sheet.cell_value(0, i)).strip().upper()
        if name and name not in out:
            out[name] = i
    return out


def _num(sheet, row, idx):
    if idx is None:
        return None
    v = sheet.cell_value(row, idx)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def parse(xls_bytes: bytes):
    """(daily_rows, location_rows) out of one year's workbook.

    daily_rows carry the per-date aggregates CME computed; location_rows the
    per-barn detail behind them. NOTHING here is named fci_value -- see the
    module docstring for why that matters.
    """
    try:
        import xlrd                                   # noqa: PLC0415
    except ImportError as e:                          # pragma: no cover
        raise ArchiveError(
            "xlrd is needed to read these legacy .xls workbooks and is "
            "deliberately not a pipeline dependency. Run this with: "
            "uv run --with xlrd python backfill_cme_archive.py") from e

    book = xlrd.open_workbook(file_contents=xls_bytes)
    sheet = book.sheet_by_index(0)
    col = _columns(sheet)
    for required in ("DATE", "LOCATION"):
        if required not in col:
            raise ArchiveError("workbook has no %s column; header reads %s"
                               % (required, sorted(col)))

    head_i = col.get("TTL HEAD")
    wt_i = col.get("TTL WEIGHT")
    px_i = col.get("WTD AVG PRICE")
    awt_i = col.get("WTD AVG WEIGHT")

    per_date, locations = {}, []
    for r in range(1, sheet.nrows):
        d = excel_date(sheet.cell_value(r, col["DATE"]))
        if d is None:
            continue
        loc = str(sheet.cell_value(r, col["LOCATION"])).strip()
        upper = loc.upper()
        agg = per_date.setdefault(d, {"report_date": d})
        if upper.startswith(DAILY_MARK):
            agg["daily_head"] = _num(sheet, r, head_i)
            agg["daily_weight"] = _num(sheet, r, wt_i)
            agg["daily_wtd_price"] = _num(sheet, r, px_i)
        elif upper.startswith(SEVEN_MARK):
            agg["seven_day_head"] = _num(sheet, r, head_i)
            agg["seven_day_weight"] = _num(sheet, r, wt_i)
            agg["seven_day_wtd_price"] = _num(sheet, r, px_i)
            agg["seven_day_wtd_weight"] = _num(sheet, r, awt_i)
        else:
            h = _num(sheet, r, head_i)
            if not h:
                continue          # a barn that filed nothing qualifying
            locations.append({
                "report_date": d,
                "location": loc,
                "state": str(sheet.cell_value(r, col["STATE"])).strip()
                         if "STATE" in col else None,
                "head": h,
                "weight": _num(sheet, r, wt_i),
                "wtd_price": _num(sheet, r, px_i),
                "wtd_weight": _num(sheet, r, awt_i),
            })

    daily = [v for _k, v in sorted(per_date.items())
             if v.get("seven_day_wtd_price") is not None]
    return daily, locations
