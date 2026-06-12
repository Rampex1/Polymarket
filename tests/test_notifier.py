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


def test_buy_executed_shows_shares_and_drift(capture_send):
    """Fill detail: how many shares the spend bought and how far the fill
    drifted from the signal price."""
    from bot import notifier

    t = make_trade(action="BUY", price=0.50, outcome="Yes")
    notifier.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.55)
    body = capture_send[-1]["content"]
    assert "1.82 shares" in body                  # 1.0 / 0.55
    assert "signal 0.500" in body
    assert "+10.0%" in body


def test_signal_message_includes_reason_and_features(capture_send):
    """The detection message must say WHY — wallet context is the whole
    point of the alert."""
    import time

    from bot import notifier
    from bot.algorithm import OpenIntent

    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=2.0, signal_price=0.20,
        question="Will X happen?", outcome="Yes", signal_id="s1",
        reason="fresh wallet 0x8b18…ed9 bet $10,000 @ 0.20",
        features={
            "wallet_age_seconds": 7200.0,
            "trade_count": 1,
            "portfolio_value_usdc": 2500.0,
            "market_category": "politics",
            "cash_usdc": 10_000.0,
            "market_end_ts": time.time() + 7 * 86_400,
        },
    )
    notifier.on_signal(intent, "insider_flow_paper")
    body = capture_send[-1]["content"]
    assert "fresh wallet" in body
    assert "0.1d old" in body                     # 7200s ≈ 0.083d → 0.1
    assert "1 prior trades" in body
    assert "portfolio $2,500" in body
    assert "politics" in body
    assert "resolves in 7d" in body


def test_signal_message_without_features_stays_compact(capture_send):
    """Copy-trade intents carry no features — no empty detail lines."""
    from bot import notifier
    from bot.algorithm import OpenIntent

    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=1.0, signal_price=0.50,
        question="Q?", outcome="Yes", signal_id="s1",
    )
    notifier.on_signal(intent, "copy_trade")
    body = capture_send[-1]["content"]
    assert "↳" not in body


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
