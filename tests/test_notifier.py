"""
Notifier tests — formatting, escaping, timezone math.

We don't actually call Discord; we capture the payload by stubbing the
HTTP POST. This is the bare minimum stubbing — the rest is real code.
"""

from zoneinfo import ZoneInfo

import pytest

from tests.conftest import make_trade


@pytest.fixture
def capture_send(monkeypatch):
    """Capture every Discord webhook POST body so we can assert on it."""
    from bot import notifier

    sent = []

    monkeypatch.setattr(
        notifier.config,
        "DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123/abc",
    )

    def fake_post(url, json=None, timeout=None):
        sent.append(json or {})

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(notifier.http, "post", fake_post)
    return sent


def test_markdown_escape_question_with_special_chars(capture_send):
    """Market titles can contain Discord markdown chars (`*`, `_`, etc.)
    — they must be escaped so formatting can't be broken by a hostile
    or unlucky title."""
    from bot import notifier

    t = make_trade(question="A*B _foo_ wins?")
    notifier.on_trade_detected(t)
    body = capture_send[-1]["content"]
    # Escaped versions present, raw markdown chars absent in title section.
    assert r"\*" in body
    assert r"\_" in body


def test_markdown_escape_in_reason_and_question(capture_send):
    from bot import notifier

    t = make_trade(question="*Yes_* wins?")
    notifier.on_risk_blocked("max | limit", t)
    body = capture_send[-1]["content"]
    # Both user-supplied strings (reason, question) get escaped.
    assert r"\*Yes\_\* wins?" in body
    assert r"max \| limit" in body


def test_markdown_escape_in_outcome(capture_send):
    """outcome is rendered in buy/sell messages — must be escaped too."""
    from bot import notifier

    t = make_trade(outcome="*Yes_*", question="q")
    notifier.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.5)
    body = capture_send[-1]["content"]
    assert r"\*Yes\_\*" in body


def test_buy_executed_format(capture_send):
    from bot import notifier
    t = make_trade(action="BUY", price=0.5, outcome="Yes", size_usdc=100)
    notifier.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.55)
    body = capture_send[-1]["content"]
    assert "📄 PAPER" in body
    assert "$1.00" in body
    assert "0.550" in body


def test_skip_when_webhook_missing(monkeypatch):
    from bot import notifier

    monkeypatch.setattr(notifier.config, "DISCORD_WEBHOOK_URL", "")

    posted = []

    def fake_post(*a, **kw):
        posted.append(1)

    monkeypatch.setattr(notifier.http, "post", fake_post)
    notifier.send("hello")
    assert not posted


def test_seconds_until_midnight_differs_between_timezones(monkeypatch):
    """Midnight is computed in config.TIMEZONE. To prove the timezone is
    actually consulted (rather than ignored and falling through to local),
    measure the seconds-until-midnight in two timezones whose UTC offsets
    differ by 4+ hours and verify the values disagree by a corresponding
    margin. A bug where TIMEZONE is silently dropped would return the same
    value for both."""
    from bot import config, notifier

    monkeypatch.setattr(config, "TIMEZONE", ZoneInfo("America/New_York"))
    ny_secs = notifier._seconds_until_midnight()

    monkeypatch.setattr(config, "TIMEZONE", ZoneInfo("Asia/Tokyo"))
    tokyo_secs = notifier._seconds_until_midnight()

    assert 0 < ny_secs <= 24 * 3600
    assert 0 < tokyo_secs <= 24 * 3600
    # NY and Tokyo are 13–14h apart depending on DST. The seconds-until-
    # midnight values must differ by *at least* a few hours, modulo the
    # 24-hour wraparound. We compare the *minimum* circular distance.
    diff = abs(ny_secs - tokyo_secs)
    circular_diff = min(diff, 24 * 3600 - diff)
    assert circular_diff > 3 * 3600, (
        f"ny={ny_secs}, tokyo={tokyo_secs} — timezone appears not to "
        f"affect the computation"
    )
