"""
One dropped read must not cost a barn for the day.

2026-10-07, in the live morning run:

    [skip] slug 1249 (Ozarks Regional Stockyards Feeder Cattle - West Plains, MO):
      ('Connection broken: IncompleteRead(9634 bytes read, 1 more expected)', ...)
    [skip] slug 1773 (Miles City) ... same

fetch_slug_payload had no retry at all -- a single requests.get, and whatever it
raised propagated to the roster loop, which logged [skip] and moved on. 236 head
(West Plains 223, Miles City 13) never reached mars_sales and 2026-10-06 printed
337.9470 against both desks' 337.8700. With the two slugs recovered it is
337.8693 on 2,128 head, matching Compass's daily head exactly.

91 slugs are fetched serially every run. Two failures in one morning is a rate,
not bad luck, and cme_ftp.py has retried its FTP fetch since it was written.

THE 4xx DIRECTION MATTERS AS MUCH AS THE RETRY. A slug that is genuinely gone,
or a credential that is wrong, is AMS telling us something true; asking three
times only gets us told three times, and it delays a real finding behind two
pointless round trips on every one of 91 slugs.
"""
import pytest
import requests

import update_index as ui


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError("HTTP %d" % self.status_code)
            err.response = self
            raise err

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: truncated body")
        return self._payload


PAYLOAD = {"results": [{"head_count": 223}], "stats": {"returnedRows": 1}}


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """The backoff is real in production and pointless in a test."""
    monkeypatch.setattr(ui.time, "sleep", lambda _s: None)


def _driver(monkeypatch, outcomes):
    """Each call pops the next outcome: an exception instance, or a response."""
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(ui.requests, "get", fake_get)
    return calls


def test_a_dropped_read_is_retried_and_the_barn_is_not_lost(monkeypatch):
    """The 2026-10-07 failure, verbatim in shape."""
    broken = requests.exceptions.ChunkedEncodingError(
        "Connection broken: IncompleteRead(9634 bytes read, 1 more expected)")
    calls = _driver(monkeypatch, [broken, FakeResponse(PAYLOAD)])
    got = ui.fetch_slug_payload(1249, "10/06/2026", "10/06/2026", ("k", ""))
    assert got == PAYLOAD
    assert len(calls) == 2, "it must actually have retried, not swallowed"


def test_a_connection_error_is_retried(monkeypatch):
    calls = _driver(monkeypatch, [requests.exceptions.ConnectionError("reset"),
                                  requests.exceptions.ConnectionError("reset"),
                                  FakeResponse(PAYLOAD)])
    assert ui.fetch_slug_payload(1, "a", "b", ("k", "")) == PAYLOAD
    assert len(calls) == 3


def test_a_timeout_is_retried(monkeypatch):
    calls = _driver(monkeypatch, [requests.exceptions.Timeout("slow"),
                                  FakeResponse(PAYLOAD)])
    assert ui.fetch_slug_payload(1, "a", "b", ("k", "")) == PAYLOAD
    assert len(calls) == 2


def test_a_truncated_body_is_retried(monkeypatch):
    """
    HTTP 200 whose JSON will not parse is the same transient failure wearing a
    different exception -- requests only raises when .json() is reached.
    """
    calls = _driver(monkeypatch, [FakeResponse(None), FakeResponse(PAYLOAD)])
    assert ui.fetch_slug_payload(1, "a", "b", ("k", "")) == PAYLOAD
    assert len(calls) == 2


def test_a_server_error_is_retried(monkeypatch):
    calls = _driver(monkeypatch, [FakeResponse(status=503), FakeResponse(PAYLOAD)])
    assert ui.fetch_slug_payload(1, "a", "b", ("k", "")) == PAYLOAD
    assert len(calls) == 2


def test_a_404_is_NOT_retried(monkeypatch):
    """
    A slug that is gone is a real finding. Retrying buries it behind two round
    trips, on every one of 91 slugs, on every run.
    """
    calls = _driver(monkeypatch, [FakeResponse(status=404)])
    with pytest.raises(requests.exceptions.HTTPError):
        ui.fetch_slug_payload(9999, "a", "b", ("k", ""))
    assert len(calls) == 1, "a 4xx must raise on the first attempt"


def test_a_401_is_NOT_retried(monkeypatch):
    """A wrong key will be wrong all three times."""
    calls = _driver(monkeypatch, [FakeResponse(status=401)])
    with pytest.raises(requests.exceptions.HTTPError):
        ui.fetch_slug_payload(1, "a", "b", ("bad", ""))
    assert len(calls) == 1


def test_exhausting_the_attempts_still_raises(monkeypatch):
    """
    The roster loop's [skip] path must still exist. Retrying is not swallowing:
    a barn that is genuinely unreachable has to reach the barn report, which is
    what names it as missing.
    """
    calls = _driver(monkeypatch, [requests.exceptions.ConnectionError("down")])
    with pytest.raises(requests.exceptions.ConnectionError):
        ui.fetch_slug_payload(1, "a", "b", ("k", ""))
    assert len(calls) == 3, "three attempts, then give up"


def test_the_attempt_count_is_honoured(monkeypatch):
    calls = _driver(monkeypatch, [requests.exceptions.ConnectionError("down")])
    with pytest.raises(requests.exceptions.ConnectionError):
        ui.fetch_slug_payload(1, "a", "b", ("k", ""), attempts=1)
    assert len(calls) == 1


def test_fetch_slug_still_returns_just_the_rows(monkeypatch):
    """
    calf_sales.py calls ui.fetch_slug() and expects the results list. The retry
    went into the payload function precisely so this signature did not move.
    """
    _driver(monkeypatch, [requests.exceptions.ConnectionError("x"),
                          FakeResponse(PAYLOAD)])
    assert ui.fetch_slug(1, "a", "b", ("k", "")) == PAYLOAD["results"]
