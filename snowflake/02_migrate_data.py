"""
One-time (re-runnable) migration: bulk-loads every row from the current
SQLite database (data/mars_history.db) into the JSA.CME_FEEDER_CATTLE
Snowflake schema. Uses write_pandas with TRUNCATE-then-load per table, so
it's safe to re-run (idempotent) if SQLite picks up newer data before the
Snowflake cutover is fully confirmed.

    python snowflake/02_migrate_data.py
"""
import os
import sqlite3
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent.parent
DB_PATH = HERE / "data" / "mars_history.db"

TABLES = ["fci_daily", "mars_sales", "cme_ftp_daily", "cme_ftp_locations"]


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


def main():
    import snowflake.connector as sc
    from snowflake.connector.pandas_tools import write_pandas

    sf_kw = dict(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "JSA"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "CME_FEEDER_CATTLE"),
        login_timeout=30,
    )
    _pkey = _load_private_key()
    if _pkey is not None:
        sf_kw["private_key"] = _pkey
    else:
        sf_kw["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    sf_conn = sc.connect(**sf_kw)
    sqlite_conn = sqlite3.connect(DB_PATH)

    for table in TABLES:
        df = pd.read_sql(f"SELECT * FROM {table}", sqlite_conn)
        sqlite_count = len(df)
        # Snowflake column names are case-insensitive when unquoted, but
        # write_pandas matches against the table's actual (uppercase) column
        # names -- uppercase the DataFrame's columns so they line up.
        df.columns = [c.upper() for c in df.columns]

        cur = sf_conn.cursor()
        cur.execute(f"TRUNCATE TABLE {table}")
        success, nchunks, nrows, _ = write_pandas(sf_conn, df, table.upper())
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        sf_count = cur.fetchone()[0]

        status = "OK" if sf_count == sqlite_count else "MISMATCH"
        print(f"{table}: sqlite={sqlite_count} snowflake={sf_count} [{status}]")

    sf_conn.close()
    sqlite_conn.close()


if __name__ == "__main__":
    main()
