-- Schema for the CME Feeder Cattle Index app's Snowflake backend.
-- Mirrors data/mars_history.db's SQLite schema exactly (natural keys, no
-- surrogate/auto-increment ids needed anywhere in this app).

CREATE SCHEMA IF NOT EXISTS JSA.CME_FEEDER_CATTLE;
USE SCHEMA JSA.CME_FEEDER_CATTLE;

-- Full-rebuild table (recompute_fci_daily() truncates and reinserts every
-- run) -- JSA's own MARS/Direct/Video estimate, only actually load-bearing
-- for the trailing 1-3 days CME hasn't published an official file for yet.
CREATE TABLE IF NOT EXISTS fci_daily (
    report_date DATE PRIMARY KEY,
    fci_value FLOAT NOT NULL,
    n_locations INTEGER NOT NULL,
    total_head INTEGER NOT NULL,
    same_day_price FLOAT,
    same_day_head INTEGER,
    same_day_avg_weight FLOAT
);

-- Insert-if-new (mirrors "INSERT OR IGNORE") -- raw per-location/per-bracket
-- qualifying sale rows feeding the MARS/Direct/Video reconstruction above.
-- Competitors' published FCI estimates, hand-entered from their daily sheets.
-- index_date is CME's index date (what the sheet estimates), not the sheet's
-- own issue date. Created 2026-09-09 by SYSADMIN, which owns it -- note that
-- Snowflake uses only the PRIMARY role for DDL, so this needed
-- SNOWFLAKE_ROLE=SYSADMIN rather than a secondary-role grant.
-- Our own estimate frozen at each run, so the head-to-head against
-- peer_estimates compares like with like. fci_daily holds only the latest value
-- per date and is rewritten every run; see snapshots.py.
CREATE TABLE IF NOT EXISTS fci_snapshots (
    index_date DATE NOT NULL,
    run_date DATE NOT NULL,
    run_slot VARCHAR NOT NULL,
    captured_at VARCHAR NOT NULL,
    fci_value FLOAT NOT NULL,
    total_head NUMBER,
    n_locations NUMBER,
    PRIMARY KEY (index_date, run_date, run_slot)
);

CREATE TABLE IF NOT EXISTS peer_estimates (
    index_date DATE NOT NULL,
    source VARCHAR NOT NULL,
    fci_value FLOAT NOT NULL,
    note VARCHAR,
    PRIMARY KEY (index_date, source)
);

CREATE TABLE IF NOT EXISTS mars_sales (
    report_date DATE NOT NULL,
    raw_date DATE NOT NULL,
    slug_id INTEGER NOT NULL,
    location VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    weight_low INTEGER NOT NULL,
    muscle_grade VARCHAR NOT NULL,
    head_count INTEGER NOT NULL,
    avg_weight FLOAT NOT NULL,
    avg_price FLOAT NOT NULL,
    -- Date the source report was PUBLISHED, when that lags the sale
    -- (video/internet auctions). NULL for auction and direct rows.
    published_date DATE,
    PRIMARY KEY (report_date, slug_id, weight_low, muscle_grade, avg_price, head_count)
);

-- Migration for deployments created before published_date existed.
ALTER TABLE mars_sales ADD COLUMN IF NOT EXISTS published_date DATE;

-- Upsert (mirrors "INSERT OR REPLACE") -- CME's own exact daily settlement
-- files (cme_ftp.py/backfill_ftp.py), wins over the estimate above for any
-- date CME has actually published.
CREATE TABLE IF NOT EXISTS cme_ftp_daily (
    report_date DATE PRIMARY KEY,
    fci_value FLOAT NOT NULL,
    reported_change FLOAT,
    n_locations INTEGER NOT NULL,
    total_head INTEGER,
    same_day_price FLOAT,
    same_day_head INTEGER,
    same_day_avg_weight FLOAT
);

-- Upsert (mirrors "INSERT OR REPLACE") -- per-location detail behind the
-- official cme_ftp_daily rows above.
CREATE TABLE IF NOT EXISTS cme_ftp_locations (
    report_date DATE NOT NULL,
    location VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    head_count INTEGER NOT NULL,
    avg_weight FLOAT NOT NULL,
    avg_price FLOAT NOT NULL,
    PRIMARY KEY (report_date, location)
);
