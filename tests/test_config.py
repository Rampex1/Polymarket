"""
Webhook resolution — the profile-level summary channel.
"""

from bot.config import resolve_summary_webhook


# ---------------------------------------------------------------------------
# resolve_summary_webhook — profile-level daily summary channel
# ---------------------------------------------------------------------------

_SUMMARY_REG = """
[experimental_summary]
url = "http://exp-summary"
type = "summary"
profile = "experimental"

[prod_summary]
url = "http://prod-summary"
type = "summary"
profile = "prod"

[algo_webhook]
url = "http://algo-hook"
profile = "prod"
"""


def _sreg(tmp_path, body: str) -> str:
    path = tmp_path / "webhooks.toml"
    path.write_text(body)
    return str(path)


def test_summary_webhook_routes_by_profile(tmp_path, monkeypatch):
    reg = _sreg(tmp_path, _SUMMARY_REG)
    assert resolve_summary_webhook("prod", reg) == "http://prod-summary"
    assert resolve_summary_webhook("experimental", reg) == "http://exp-summary"


def test_summary_webhook_ignores_non_summary_blocks(tmp_path, monkeypatch):
    """Blocks without type = "summary" must not be returned."""
    reg = _sreg(tmp_path, _SUMMARY_REG)
    # "algo_webhook" block has profile="prod" but no type="summary"
    # resolve_webhook would find it; resolve_summary_webhook must not.
    url = resolve_summary_webhook("prod", reg)
    assert url == "http://prod-summary"  # only the typed block


def test_summary_webhook_missing_profile_returns_empty(tmp_path, monkeypatch):
    reg = _sreg(tmp_path, _SUMMARY_REG)
    assert resolve_summary_webhook("mystery", reg) == ""
    assert resolve_summary_webhook("", reg) == ""


def test_summary_webhook_missing_registry_is_not_fatal(tmp_path, monkeypatch):
    assert resolve_summary_webhook("prod", str(tmp_path / "absent.toml")) == ""


def test_shipped_summary_webhooks_cover_both_profiles(monkeypatch):
    """The real webhooks.toml must have summary entries for both profiles."""
    for profile in ("prod", "experimental"):
        url = resolve_summary_webhook(profile)
        assert url.startswith("https://discord.com/api/webhooks/"), profile
