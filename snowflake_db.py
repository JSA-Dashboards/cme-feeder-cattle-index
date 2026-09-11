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


DEFAULT_KEY_FILE = "~/.snowflake/keys/snowflake_rsa_key.p8"


def _key_pair_kwargs():
    """
    Connector kwargs for key-pair auth, or {} if no key material is configured.

    This account has NO SAML IdP (an authenticator request returns 390190), so
    authenticator="externalbrowser" cannot work and key-pair is the only
    passwordless option. Two key sources are supported because the two runtime
    environments differ:

      1. A key FILE -- SNOWFLAKE_PRIVATE_KEY_FILE, else DEFAULT_KEY_FILE. Used
         for local runs and the scheduled job. Passed as private_key_file +
         private_key_file_pwd, both str: the connector does NOT read
         SNOWFLAKE_PRIVATE_KEY_PASSPHRASE itself (that name is honoured only by
         the `snow` CLI), so omitting it raises "Password was not given but
         private key is encrypted".

      2. PEM TEXT in SNOWFLAKE_PRIVATE_KEY -- for Streamlit Community Cloud,
         whose secrets are TOML text with no filesystem to hold a .p8. The
         connector's inline `private_key` wants DER, not PEM, so the PEM is
         decrypted and re-serialised to unencrypted PKCS8 DER here rather than
         handed over raw.

    The file path wins when both are present: it is the better-tested path.
    """
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")

    key_file = os.path.expanduser(
        os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE", DEFAULT_KEY_FILE)
    )
    if os.path.exists(key_file):
        kw = {"private_key_file": key_file, "authenticator": "SNOWFLAKE_JWT"}
        if passphrase:
            kw["private_key_file_pwd"] = passphrase
        return kw

    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    if pem:
        from cryptography.hazmat.primitives import serialization

        loaded = serialization.load_pem_private_key(
            pem.encode(),
            password=passphrase.encode() if passphrase else None,
        )
        der = loaded.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return {"private_key": der, "authenticator": "SNOWFLAKE_JWT"}

    return {}


def get_conn():
    if use_snowflake():
        import snowflake.connector as sc

        kwargs = dict(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            role=os.environ.get("SNOWFLAKE_ROLE"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
            database=os.environ.get("SNOWFLAKE_DATABASE", "JSA"),
            schema=os.environ.get("SNOWFLAKE_SCHEMA", "CME_FEEDER_CATTLE"),
            login_timeout=30,
        )
        auth = _key_pair_kwargs()
        if auth:
            kwargs.update(auth)
        elif os.environ.get("SNOWFLAKE_PASSWORD"):
            # Retained so an existing password-configured deployment keeps
            # working; key-pair is preferred whenever key material is present.
            kwargs["password"] = os.environ["SNOWFLAKE_PASSWORD"]
        else:
            raise RuntimeError(
                "No Snowflake credentials. Provide a key-pair via "
                "SNOWFLAKE_PRIVATE_KEY_FILE (default " + DEFAULT_KEY_FILE + ") "
                "or SNOWFLAKE_PRIVATE_KEY (PEM text), plus "
                "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE if the key is encrypted. "
                "SNOWFLAKE_PASSWORD is accepted as a fallback."
            )
        return sc.connect(**kwargs)
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
