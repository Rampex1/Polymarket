"""
Discord message tests — alert formatting, summaries, escaping, timezone math.

We don't actually call Discord; we capture the payload by stubbing the
HTTP POST. This is the bare minimum stubbing — the rest is real code.
"""

from zoneinfo import ZoneInfo

import pytest

from tests.conftest import make_trade


HOOK = "https://discord.com/api/webhooks/123/abc"


@pytest.fixture
def capture_send(monkeypatch):
    """Capture every Discord webhook POST body so we can assert on it."""
    from bot.discord import threads, webhook

    # No bot token -> threads.send_or_open takes the plain-send branch, so
    # every alert lands in the captured list rather than trying to open one.
    monkeypatch.setattr(threads.config, "DISCORD_BOT_TOKEN", "")

    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json or {})

        class R:
            status_code = 200

        return R()

    monkeypatch.setattr(webhook.http, "post", fake_post)
    return sent


def test_markdown_escape_question_with_special_chars(capture_send):
    """Market titles can contain Discord markdown chars (`*`, `_`, etc.)
    — they must be escaped so formatting can't be broken by a hostile
    or unlucky title."""
    from bot.discord import alerts, webhook

    t = make_trade(question="A*B _foo_ wins?")
    alerts.on_trade_detected(t, webhook_url=HOOK)
    body = capture_send[-1]["content"]
    # Escaped versions present, raw markdown chars absent in title section.
    assert r"\*" in body
    assert r"\_" in body


def test_markdown_escape_in_reason_and_question(capture_send):
    from bot.discord import alerts, webhook

    t = make_trade(question="*Yes_* wins?")
    alerts.on_risk_blocked("max | limit", t, webhook_url=HOOK)
    body = capture_send[-1]["content"]
    # Both user-supplied strings (reason, question) get escaped.
    assert r"\*Yes\_\* wins?" in body
    assert r"max \| limit" in body


def test_markdown_escape_in_outcome(capture_send):
    """outcome is rendered in buy/sell messages — must be escaped too."""
    from bot.discord import alerts, webhook

    t = make_trade(outcome="*Yes_*", question="q")
    alerts.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.5, webhook_url=HOOK)
    body = capture_send[-1]["content"]
    assert r"\*Yes\_\*" in body


def test_buy_executed_format(capture_send):
    from bot.discord import alerts, webhook
    t = make_trade(action="BUY", price=0.5, outcome="Yes", size_usdc=100)
    alerts.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.55, webhook_url=HOOK)
    body = capture_send[-1]["content"]
    assert "📄 **PAPER BUY**" in body
    assert "$1.00" in body
    assert "0.550" in body


def test_buy_executed_shows_shares_and_drift(capture_send):
    """Fill detail: how many shares the spend bought and how far the fill
    drifted from the signal price."""
    from bot.discord import alerts, webhook

    t = make_trade(action="BUY", price=0.50, outcome="Yes")
    alerts.on_buy_executed(t, spent_usdc=1.0, paper=True, fill_price=0.55, webhook_url=HOOK)
    body = capture_send[-1]["content"]
    assert "1.82 shares" in body                  # 1.0 / 0.55
    assert "+10.0% slip" in body


def test_signal_message_includes_reason_and_features(capture_send):
    """The detection message must say WHY — wallet context is the whole
    point of the alert."""
    import time

    from bot.discord import alerts, webhook
    from bot.domain.intents import OpenIntent

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
    alerts.on_signal(intent, "insider_flow_paper", webhook_url=HOOK)
    body = capture_send[-1]["content"]
    assert "fresh wallet" in body
    assert "0.1d old" in body                     # 7200s ≈ 0.083d → 0.1
    assert "1 prior trades" in body
    assert "portfolio $2,500" in body
    assert "politics" in body
    assert "resolves in 7d" in body


def test_signal_message_without_features_stays_compact(capture_send):
    """Copy-trade intents carry no features — no empty detail lines."""
    from bot.discord import alerts, webhook
    from bot.domain.intents import OpenIntent

    intent = OpenIntent(
        market_id="m1", asset_id="a1", usdc_amount=1.0, signal_price=0.50,
        question="Q?", outcome="Yes", signal_id="s1",
    )
    alerts.on_signal(intent, "copy_trade", webhook_url=HOOK)
    body = capture_send[-1]["content"]
    assert "↳" not in body


def test_skip_when_webhook_missing(monkeypatch):
    from bot.discord import alerts, webhook

    posted = []

    def fake_post(*a, **kw):
        posted.append(1)

    monkeypatch.setattr(webhook.http, "post", fake_post)
    webhook.send("hello")          # no webhook → no global fallback → no post
    assert not posted


def test_summary_text_format(fresh_db):
    """/summary includes per-algo blocks and a combined total."""
    from bot.discord import views
    from bot.storage.ledger import Ledger
    from tests.conftest import make_trade

    # Seed one position into the DB for algo "a1".
    t = Ledger(algo="a1")
    t.init_paper_balance(100.0)
    trade = make_trade(action="BUY", market_id="m1", outcome="YES", price=0.40)
    t.record_buy(trade, spent_usdc=2.0, shares=5.0, fill_price=0.40, paper=True)

    body = views._summary_text([("a1", True)], profile="experimental")

    assert "experimental" in body
    assert "**a1**" in body
    assert "PAPER" in body
    assert "`YES`" in body
    assert "Total" in body
    assert "────" in body   # separator present


def test_send_skips_when_no_webhook(monkeypatch):
    """No webhook configured -> no POST, whatever the text."""
    from bot.discord import webhook
    sent = []
    monkeypatch.setattr(webhook.http, "post", lambda *a, **kw: sent.append(1))
    webhook.send("anything", webhook_url="")
    assert not sent


def test_summary_text_combined_total(fresh_db):
    """Combined P&L/exposure sums across all algos."""
    from bot.discord import views
    from bot.storage.ledger import Ledger
    from tests.conftest import make_trade

    for name, usdc in [("b1", 3.0), ("b2", 5.0)]:
        t = Ledger(algo=name)
        t.init_paper_balance(100.0)
        trade = make_trade(action="BUY", market_id="m1", outcome="YES", price=0.50)
        t.record_buy(trade, spent_usdc=usdc, shares=usdc * 2, fill_price=0.50, paper=True)

    body = views._summary_text([("b1", True), ("b2", True)])

    assert "2 algorithms" in body
    assert "**b1**" in body
    assert "**b2**" in body
    # Combined exposure = $3 + $5 = $8
    assert "$8.00" in body


def test_heartbeat_timestamp_uses_config_timezone(monkeypatch):
    """The heartbeat stamps its time in config.TIMEZONE. To prove the setting
    is consulted rather than silently falling through to VPS local time,
    render in two zones and check the tz abbreviation differs."""
    from bot import config
    from bot.discord import heartbeat, webhook

    sent = []
    monkeypatch.setattr(webhook.http, "post",
                        lambda url, json=None, timeout=None: sent.append(json["content"]))

    monkeypatch.setattr(config, "TIMEZONE", ZoneInfo("America/New_York"))
    heartbeat.ping("http://test", profile="p", n_algos=1)

    monkeypatch.setattr(config, "TIMEZONE", ZoneInfo("Asia/Tokyo"))
    heartbeat.ping("http://test", profile="p", n_algos=1)

    assert len(sent) == 2
    assert sent[0] != sent[1]
    assert "JST" in sent[1]
    assert "1 algorithm running" in sent[0]
