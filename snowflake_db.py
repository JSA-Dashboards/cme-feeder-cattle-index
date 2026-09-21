"""
Shared DB backend for the CME Feeder Cattle Index app: SQLite (default,
local dev) or Snowflake (USE_SNOWFLAKE=1), toggled by one env flag -- same
pattern already proven for basis-tracker-streamlit/river-fob-portal, but
deliberately simplified for this app's much smaller schema (4 tables,
natural keys, no auto-increment ids, no bulk multi-row upserts needed).

Every read in this app goes through pd.read_sql(), never raw
cursor.fetchone()/dict access, so there's no need for a DictCursor/lowercase
shim at the cursor level the way basis-tracker's database.py has one --
read_sql_lower() covers it at the DataFrame level instead (Snowflake
returns UPPERCASE column names; SQLite already returns lowercase, so the
lowercase step is a harmless no-op there).
"""
import os
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "mars_history.db"


def use_snowflake() -> bool:
    return os.getenv("USE_SNOWFLAKE", "").strip().lower() in ("1", "true", "yes", "on")


def _load_private_key():
    """RSA private key for Snowflake key-pair auth (the account enforces MFA on
    password sign-ins), as DER bytes; None if not configured (falls back to password).
    Source: SNOWFLAKE_PRIVATE_KEY_PATH (.p8 file) or SNOWFLAKE_PRIVATE_KEY (PEM text)."""
    path = (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH") or "").strip()
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY") or ""
    if not path and not pem.strip():
        return None
    from cryptography.hazmat.primitives import serialization
    data = open(path, "rb").read() if path else pem.replace("\\n", "\n").encode()
    pwd = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PWD") or None
    key = serialization.load_pem_private_key(data, password=pwd.encode() if pwd else None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())


def get_conn():
    if use_snowflake():
        import snowflake.connector as sc
        kw = dict(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            role=os.environ.get("SNOWFLAKE_ROLE"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
            database=os.environ.get("SNOWFLAKE_DATABASE", "JSA"),
            schema=os.environ.get("SNOWFLAKE_SCHEMA", "CME_FEEDER_CATTLE"),
            login_timeout=30,
        )
        pkey = _load_private_key()
        if pkey is not None:
            kw["private_key"] = pkey
        else:
            kw["password"] = os.environ["SNOWFLAKE_PASSWORD"]
        return sc.connect(**{k: v for k, v in kw.items() if v is not None})
    return sqlite3.connect(DB_PATH)


def read_sql_lower(query: str, conn) -> pd.DataFrame:
    df = pd.read_sql(query, conn)
    df.columns = df.columns.str.lower()
    return df


def iso(v):
    """
    Normalizes a DATE value to an ISO string regardless of backend. SQLite
    has no native DATE type and always returns the TEXT it was stored as
    (already an ISO string); Snowflake's connector returns a real
    datetime.date object. Code that uses a date as a dict key or re-parses
    it with date.fromisoformat() needs this applied at the point of read so
    it behaves identically on both backends.
    """
    return v.isoformat() if hasattr(v, "isoformat") else v


def iso_row(row):
    """Applies iso() to every value in a fetched row/tuple that looks like a date."""
    return tuple(iso(v) for v in row)


def merge_ignore(conn, table: str, cols: list[str], values: tuple, key_cols: list[str]) -> None:
    """INSERT OR IGNORE equivalent -- skip a row if its key already exists."""
    if not use_snowflake():
        ph = ",".join("?" * len(cols))
        conn.execute(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})", values)
        return
    using = ", ".join(f"%s AS {c}" for c in cols)
    on = " AND ".join(f"t.{k}=s.{k}" for k in key_cols)
    ins_cols = ",".join(cols)
    ins_vals = ",".join(f"s.{c}" for c in cols)
    conn.cursor().execute(
        f"MERGE INTO {table} t USING (SELECT {using}) s ON {on} "
        f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) VALUES ({ins_vals})",
        values,
    )


def merge_replace(conn, table: str, cols: list[str], values: tuple, key_cols: list[str]) -> None:
    """INSERT OR REPLACE equivalent -- upsert keyed by key_cols."""
    if not use_snowflake():
        ph = ",".join("?" * len(cols))
        conn.execute(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})", values)
        return
    update_cols = [c for c in cols if c not in key_cols]
    using = ", ".join(f"%s AS {c}" for c in cols)
    on = " AND ".join(f"t.{k}=s.{k}" for k in key_cols)
    ins_cols = ",".join(cols)
    ins_vals = ",".join(f"s.{c}" for c in cols)
    setc = ", ".join(f"t.{c}=s.{c}" for c in update_cols)
    conn.cursor().execute(
        f"MERGE INTO {table} t USING (SELECT {using}) s ON {on} "
        f"WHEN MATCHED THEN UPDATE SET {setc} "
        f"WHEN NOT MATCHED THEN INSERT ({ins_cols}) VALUES ({ins_vals})",
        values,
    )


def placeholders(n: int) -> str:
    """Backend-appropriate parameter placeholders for a plain INSERT (no
    upsert semantics needed -- e.g. inserting into a table just truncated)."""
    return ",".join(["?" if not use_snowflake() else "%s"] * n)


def truncate(conn, table: str) -> None:
    """Full-table clear before a bulk reinsert (fci_daily's recompute path)."""
    if use_snowflake():
        conn.cursor().execute(f"TRUNCATE TABLE {table}")
    else:
        conn.execute(f"DELETE FROM {table}")
