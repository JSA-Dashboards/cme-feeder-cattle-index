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
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

HERE = Path(__file__).parent.parent
DB_PATH = HERE / "data" / "mars_history.db"

TABLES = ["fci_daily", "mars_sales", "cme_ftp_daily", "cme_ftp_locations"]


def main():
    from snowflake.connector.pandas_tools import write_pandas

    # Auth goes through snowflake_db.get_conn() so there is exactly one
    # credential path in the codebase (key-pair, with a password fallback).
    # USE_SNOWFLAKE is forced on here: this script's whole job is the upload,
    # regardless of which backend the app itself is pointed at.
    sys.path.insert(0, str(HERE))
    os.environ["USE_SNOWFLAKE"] = "1"
    import snowflake_db as db

    sf_conn = db.get_conn()
    sqlite_conn = sqlite3.connect(DB_PATH)

    for table in TABLES:
        df = pd.read_sql(f"SELECT * FROM {table}", sqlite_conn)
        sqlite_count = len(df)
        # Snowflake column names are case-insensitive when unquoted, but
        # write_pandas matches against the table's actual (uppercase) column
        # names -- uppercase the DataFrame's columns so they line up.
        df.columns = [c.upper() for c in df.columns]

        cur = sf_conn.cursor()

        # Only upload columns the target table actually has. update_index.py can
        # add a column to SQLite (init_db migrates it) while the Snowflake table
        # still lacks it, because ALTER there needs MODIFY, which SYSADMIN was
        # not granted. Without this, write_pandas fails on the whole table for
        # one absent column -- and none of the extras are read by app.py, so
        # dropping them costs the dashboard nothing.
        target_cols = {r[0].upper() for r in cur.execute(f"DESC TABLE {table}")}
        extra = [c for c in df.columns if c not in target_cols]
        if extra:
            print(f"{table}: skipping column(s) absent in Snowflake: {', '.join(extra)}")
            df = df[[c for c in df.columns if c in target_cols]]

        # DELETE, not TRUNCATE: Snowflake gates TRUNCATE behind its own
        # privilege, which SYSADMIN was not granted on these tables (it has
        # SELECT/INSERT/UPDATE/DELETE only, and ACCOUNTADMIN owns them).
        # DELETE is also transactional, which TRUNCATE-then-load was not --
        # wrapping the swap means a failed upload can no longer leave the
        # dashboard reading an empty table.
        cur.execute("BEGIN")
        try:
            cur.execute(f"DELETE FROM {table}")
            success, nchunks, nrows, _ = write_pandas(sf_conn, df, table.upper())
            if not success:
                raise RuntimeError(f"write_pandas reported failure for {table}")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            print(f"{table}: FAILED - rolled back, table left as it was")
            raise
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        sf_count = cur.fetchone()[0]

        status = "OK" if sf_count == sqlite_count else "MISMATCH"
        print(f"{table}: sqlite={sqlite_count} snowflake={sf_count} [{status}]")

    sf_conn.close()
    sqlite_conn.close()


if __name__ == "__main__":
    main()
