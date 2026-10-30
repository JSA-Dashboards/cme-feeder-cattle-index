"""`--only` has to actually gate, and a bad name has to be fatal.

A flag that silently accepted an unknown source would run NOTHING and still
exit 0 — the exact failure mode this whole job exists to end, since that is
how the 07:45 border run looked for weeks while collecting yesterday's report.
"""
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

import update_imports as ui  # noqa: E402


class _Conn:
    def close(self):
        pass


def _stub(monkeypatch, ran):
    """Replace every source module with one that records that it ran."""
    monkeypatch.setattr(ui.db, "get_conn", lambda *a, **k: _Conn())

    def mod(name, *fns):
        m = types.ModuleType(name)
        for fn in fns:
            setattr(m, fn, lambda *a, **k: ran.append(name) or 0)
        m.current_status = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, name, m)

    mod("border_reports", "init_tables", "ingest", "init_receipt_tables",
        "ingest_receipts", "ingest_volumes", "ingest_prices")
    mod("calf_sales", "init_tables", "ingest")
    mod("corn_bids", "init_tables", "ingest")
    mod("feed_bids", "init_tables", "ingest_distillers", "ingest_hay")
    mod("census_imports", "init_tables", "ingest")


def test_only_ams_runs_the_border_reports_and_nothing_else(monkeypatch, capsys):
    """What deploy/run_imports.sh asks for on the droplet."""
    ran = []
    _stub(monkeypatch, ran)
    assert ui.main(["--only", "ams"]) == 0
    assert set(ran) == {"border_reports"}, ran
    out = capsys.readouterr().out
    assert "skipping calf, corn, feed, census" in out


def test_no_flag_still_runs_everything(monkeypatch):
    """The desktop's call is unchanged, which is the point of the default."""
    ran = []
    _stub(monkeypatch, ran)
    assert ui.main([]) == 0
    assert set(ran) == {"border_reports", "calf_sales", "corn_bids",
                        "feed_bids", "census_imports"}, ran


def test_a_subset_runs_exactly_that_subset(monkeypatch):
    ran = []
    _stub(monkeypatch, ran)
    assert ui.main(["--only", "ams,census"]) == 0
    assert set(ran) == {"border_reports", "census_imports"}, ran


def test_an_unknown_source_is_fatal_before_any_work(monkeypatch):
    """Silently running nothing and exiting 0 is the failure being designed out.

    It must also refuse BEFORE opening a connection, so a typo in a crontab
    line cannot touch Snowflake at all.
    """
    ran = []
    opened = []
    _stub(monkeypatch, ran)
    monkeypatch.setattr(ui.db, "get_conn",
                        lambda *a, **k: opened.append(1) or _Conn())
    assert ui.main(["--only", "border"]) == 2        # a plausible typo for ams
    assert ran == [] and opened == []


def test_only_with_no_value_is_fatal(monkeypatch):
    ran = []
    _stub(monkeypatch, ran)
    assert ui.main(["--only"]) == 2
    assert ran == []


def test_a_skipped_source_is_not_reported_as_a_failure(monkeypatch, capsys):
    """_Skip is caught before the generic handler.

    Without that split every skipped source would print a traceback and the
    run would read as four failures and one success.
    """
    ran = []
    _stub(monkeypatch, ran)
    ui.main(["--only", "ams"])
    out = capsys.readouterr().out
    assert "FAILED" not in out
    assert "Traceback" not in out
    assert "import refresh OK (ams)" in out


def test_the_droplet_wrapper_asks_for_the_flag_this_module_supports():
    """The wrapper and the flag are in different files and must not drift.

    A crontab calling `--only ams` against a build without the flag would get
    an unrecognised-argument no-op or a full five-source run, depending on how
    it failed — and either way it would exit 0.
    """
    here = os.path.join(_HERE, "..", "deploy", "run_imports.sh")
    sh = open(here, encoding="utf-8").read()
    assert "--only ams" in sh
    assert "ams" in ui.SOURCES


@pytest.mark.parametrize("name", ui.SOURCES)
def test_every_named_source_can_be_asked_for_alone(monkeypatch, name):
    """A name in SOURCES that no block honours would skip everything silently."""
    ran = []
    _stub(monkeypatch, ran)
    assert ui.main(["--only", name]) == 0
    # a SET: the stub records once per function the block calls, and
    # border_reports alone makes six calls
    assert len(set(ran)) == 1, (name, ran)
