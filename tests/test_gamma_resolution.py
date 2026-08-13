"""Gamma market lookup — the query behind every settlement."""

import bot.polymarket.api as api


class FakeResp:
    def __init__(self, rows): self.status_code, self._rows = 200, rows
    def json(self): return self._rows


def test_a_resolved_market_is_found_behind_the_closed_filter(monkeypatch):
    """`/markets` hides closed markets by default, so the plain query returns
    nothing for exactly the markets settlement needs. Without the retry,
    nothing ever settles."""
    seen = []
    row = {"conditionId": "0xABC", "closed": True, "outcomePrices": '["0", "1"]'}

    def fake_get(url, params, timeout):
        seen.append(params)
        return FakeResp([row] if params.get("closed") == "true" else [])

    monkeypatch.setattr(api.SESSION, "get", fake_get)
    assert api.fetch_market_resolution("0xabc") is row
    assert any(p.get("closed") == "true" for p in seen)


def test_an_open_market_is_found_without_the_extra_call(monkeypatch):
    """Open markets are the common case — every sweep re-checks them."""
    calls = []
    row = {"conditionId": "0xabc", "closed": False}

    def fake_get(url, params, timeout):
        calls.append(params)
        return FakeResp([row])

    monkeypatch.setattr(api.SESSION, "get", fake_get)
    assert api.fetch_market_resolution("0xabc") is row
    assert len(calls) == 1


def test_an_unfiltered_page_never_settles_the_wrong_market(monkeypatch):
    """Gamma answers 200 with an arbitrary first page for a parameter spelling
    it does not know. Returning one of those rows would settle a position
    against a different market's outcome."""
    monkeypatch.setattr(api.SESSION, "get", lambda url, params, timeout: FakeResp(
        [{"conditionId": "0xsomethingelse", "closed": True, "outcomePrices": '["1", "0"]'}]))
    assert api.fetch_market_resolution("0xabc") is None
