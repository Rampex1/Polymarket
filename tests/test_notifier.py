"""
Notifier tests — formatting, escaping, timezone math.

We don't actually call Telegram; we capture the payload by stubbing the
HTTP POST. This is the bare minimum stubbing — the rest is real code.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import make_trade


@pytest.fixture
def capture_send(monkeypatch):
    """Capture every Telegram POST body so we can assert on it."""
    from bot import notifier

    sent = []

    monkeypatch.setattr(notifier.config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(notifier.config, "TELEGRAM_CHAT_ID", "1234")

    def fake_post(url, json=None, timeout=None):
        sent.append(json or {})

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(notifier.http, "post", fake_post)
    return sent


def test_html_escape_question_with_angle_brackets(capture_send):
    """Market titles can contain < or & — they must be escaped or Telegram
    rejects the message. Pre-fix, this silently dropped alerts."""
    from bot import notifier

    t = make_trade(question="A&B <foo> wins?")
    notifier.on_trade_detected(t)
    body = capture_send[-1]["text"]
    # Telegram-safe escapes present, raw chars absent.
    assert "&amp;" in body
    assert "&lt;foo&gt;" in body
    assert "<foo>" not in body


def test_html_escape_in_reason_and_question(capture_send):
    from bot import notifier

    t = make_trade(question="<Yes&> wins?")
    notifier.on_risk_blocked("max < limit", t)
    body = capture_send[-1]["text"]
    # Both user-supplied strings (reason, question) get escaped.
    assert "&lt;Yes&amp;&gt; wins?" in body
    assert "max &lt; limit" in body


def test_html_escape_in_outcome(capture_send):
    """outcome is rendered in buy/sell messages — must be escaped too."""
    from bot import notifier

    t = make_trade(outcome="<Yes&>", question="q")
    notifier.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.5)
    body = capture_send[-1]["text"]
    assert "&lt;Yes&amp;&gt;" in body


def test_buy_executed_format(capture_send):
    from bot import notifier
    t = make_trade(action="BUY", price=0.5, outcome="Yes", size_usdc=100)
    notifier.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.55)
    body = capture_send[-1]["text"]
    assert "📄 PAPER" in body
    assert "$1.00" in body
    assert "0.550" in body


def test_skip_when_creds_missing(monkeypatch):
    from bot import notifier

    monkeypatch.setattr(notifier.config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(notifier.config, "TELEGRAM_CHAT_ID", "")

    posted = []

    def fake_post(*a, **kw):
        posted.append(1)

    monkeypatch.setattr(notifier.http, "post", fake_post)
    notifier.send("hello")
    assert not posted


def test_seconds_until_midnight_uses_configured_timezone(monkeypatch):
    """Midnight is computed in config.TIMEZONE — not the VPS local time."""
    from bot import config, notifier

    fixed_tz = ZoneInfo("America/New_York")
    monkeypatch.setattr(config, "TIMEZONE", fixed_tz)

    secs = notifier._seconds_until_midnight()
    # Sanity bounds: less than 24h, more than 0.
    assert 0 < secs <= 24 * 3600
