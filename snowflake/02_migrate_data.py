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


def main():
    import snowflake.connector as sc
    from snowflake.connector.pandas_tools import write_pandas

    sf_conn = sc.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "JSA"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA", "CME_FEEDER_CATTLE"),
        login_timeout=30,
    )
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
