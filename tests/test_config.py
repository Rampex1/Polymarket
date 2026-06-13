"""
Webhook resolution — env override vs the committed registry.
"""

from bot.config import resolve_webhook


def _registry(tmp_path, body: str) -> str:
    path = tmp_path / "webhooks.toml"
    path.write_text(body)
    return str(path)


REG = """
[prod]
url = "http://prod-hook"
profiles = ["prod"]

[experimental]
url = "http://paper-hook"
profiles = ["experimental", "experimental2"]
"""


def test_registry_routes_by_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    reg = _registry(tmp_path, REG)
    assert resolve_webhook("prod", reg) == "http://prod-hook"
    assert resolve_webhook("experimental", reg) == "http://paper-hook"
    assert resolve_webhook("experimental2", reg) == "http://paper-hook"


def test_unrouted_profile_gets_no_webhook(tmp_path, monkeypatch):
    """Fail quiet, not loud: an unmapped profile just doesn't notify."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert resolve_webhook("mystery", _registry(tmp_path, REG)) == ""
    assert resolve_webhook("", _registry(tmp_path, REG)) == ""


def test_env_var_overrides_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://override")
    assert resolve_webhook("prod", _registry(tmp_path, REG)) == "http://override"


def test_missing_registry_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert resolve_webhook("prod", str(tmp_path / "absent.toml")) == ""


def test_malformed_registry_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert resolve_webhook("prod", _registry(tmp_path, "not [ toml")) == ""


def test_shipped_registry_is_reference_only(monkeypatch):
    """Routing is now per-algorithm (webhook_url in profile TOMLs).
    The registry is a reference doc; resolve_webhook returns '' for any
    profile since no `profiles` list exists in the new format."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    for profile in ("prod", "experimental"):
        url = resolve_webhook(profile)
        assert url == "", f"Expected no profile-based routing for {profile}"
