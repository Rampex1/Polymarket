"""
Profile-loader + params-schema tests.

Covers:
  * The shipped config/*.toml files load and have the right shapes/modes.
  * Loader validation: missing profile, missing keys, bad mode, unknown
    type, unknown param, duplicate names, per-params validate().
  * TOML list → tuple coercion for tuple-typed fields.
  * Multiple instances of one algorithm class keep params separate.
"""

import importlib

import pytest

from algorithms import REGISTRY
from algorithms.copy_trade import CopyTradeAlgorithm, CopyTradeParams
from bot.domain.intents import Mode
from bot.profile_loader import ProfileError, load_profile


# ---------------------------------------------------------------------------
# Shipped profiles
# ---------------------------------------------------------------------------


def test_experimental_profile_loads_all_paper():
    algos = load_profile("experimental", REGISTRY)
    assert len(algos) >= 1
    assert all(a.params.mode == Mode.PAPER for a in algos)


def test_prod_profile_is_paused():
    """Prod is intentionally empty (no live trading) — loader fails fast."""
    with pytest.raises(ProfileError, match="no \\[\\[algorithm\\]\\] blocks"):
        load_profile("prod", REGISTRY)


def test_unset_profile_refuses_to_boot(monkeypatch):
    """No implicit profile: a bare `python main.py` must crash, not trade."""
    monkeypatch.delenv("PROFILE", raising=False)
    import algorithms
    importlib.reload(algorithms)
    try:
        with pytest.raises(ProfileError, match="PROFILE is not set"):
            algorithms.ENABLED
    finally:
        # Reload once more so later tests see a module untainted by the
        # deleted env var (ENABLED is cached at first access).
        monkeypatch.setenv("PROFILE", "experimental")
        importlib.reload(algorithms)


def test_lazy_enabled_resolves(monkeypatch):
    monkeypatch.setenv("PROFILE", "experimental")
    import algorithms
    importlib.reload(algorithms)
    assert len(algorithms.ENABLED) >= 1
    assert all(a.params.mode == Mode.PAPER for a in algorithms.ENABLED)


# ---------------------------------------------------------------------------
# Loader validation — every failure must name the file/block and be fail-fast
# ---------------------------------------------------------------------------


def _write_profile(tmp_path, body: str, name: str = "t") -> tuple[str, str]:
    (tmp_path / f"{name}.toml").write_text(body)
    return name, str(tmp_path)


VALID = """
[[algorithm]]
type = "copy_trade"
name = "ct"
mode = "paper"
[algorithm.params]
target_address = "0xabc"
webhook_url = "http://hook"
"""


LIVE_BLOCK = """
[[algorithm]]
type = "copy_trade"
name = "ct_live"
mode = "live"
[algorithm.params]
target_address = "0xabc"
webhook_url = "http://hook"
"""


def test_live_mode_requires_allow_live_opt_in(tmp_path):
    name, d = _write_profile(tmp_path, LIVE_BLOCK)
    with pytest.raises(ProfileError, match="allow_live"):
        load_profile(name, REGISTRY, config_dir=d)


def test_allow_live_unlocks_live_mode(tmp_path):
    name, d = _write_profile(tmp_path, "allow_live = true\n" + LIVE_BLOCK)
    (algo,) = load_profile(name, REGISTRY, config_dir=d)
    assert algo.params.mode == Mode.LIVE


def test_valid_minimal_profile(tmp_path):
    name, d = _write_profile(tmp_path, VALID)
    (algo,) = load_profile(name, REGISTRY, config_dir=d)
    assert isinstance(algo, CopyTradeAlgorithm)
    assert algo.params.name == "ct"
    assert algo.params.mode == Mode.PAPER
    assert algo.params.target_address == "0xabc"


def test_missing_profile_lists_available(tmp_path):
    _write_profile(tmp_path, VALID, name="exists")
    with pytest.raises(ProfileError, match="exists"):
        load_profile("nope", REGISTRY, config_dir=str(tmp_path))


def test_empty_profile_rejected(tmp_path):
    name, d = _write_profile(tmp_path, "# nothing here\n")
    with pytest.raises(ProfileError, match="no .*algorithm.* blocks"):
        load_profile(name, REGISTRY, config_dir=d)


@pytest.mark.parametrize("missing", ["type", "name", "mode"])
def test_required_block_keys(tmp_path, missing):
    lines = {
        "type": 'type = "copy_trade"',
        "name": 'name = "ct"',
        "mode": 'mode = "paper"',
    }
    del lines[missing]
    body = "[[algorithm]]\n" + "\n".join(lines.values()) + "\n"
    name, d = _write_profile(tmp_path, body)
    with pytest.raises(ProfileError, match=f"missing required key '{missing}'"):
        load_profile(name, REGISTRY, config_dir=d)


def test_unknown_type_rejected(tmp_path):
    name, d = _write_profile(
        tmp_path, '[[algorithm]]\ntype="hodl"\nname="x"\nmode="paper"\n')
    with pytest.raises(ProfileError, match="unknown type 'hodl'"):
        load_profile(name, REGISTRY, config_dir=d)


def test_bad_mode_rejected(tmp_path):
    name, d = _write_profile(
        tmp_path, '[[algorithm]]\ntype="copy_trade"\nname="x"\nmode="yolo"\n')
    with pytest.raises(ProfileError, match="paper.*live"):
        load_profile(name, REGISTRY, config_dir=d)


def test_unknown_param_key_rejected(tmp_path):
    body = VALID + "\ntier1_sze = 5.0\n"  # typo inside [algorithm.params]
    name, d = _write_profile(tmp_path, body)
    with pytest.raises(ProfileError, match="tier1_sze"):
        load_profile(name, REGISTRY, config_dir=d)


def test_duplicate_names_rejected(tmp_path):
    name, d = _write_profile(tmp_path, VALID + VALID)
    with pytest.raises(ProfileError, match="duplicate name 'ct'"):
        load_profile(name, REGISTRY, config_dir=d)


def test_copy_trade_without_target_rejected(tmp_path):
    name, d = _write_profile(
        tmp_path, '[[algorithm]]\ntype="copy_trade"\nname="x"\nmode="paper"\n')
    with pytest.raises(ProfileError, match="target"):
        load_profile(name, REGISTRY, config_dir=d)


def test_toml_list_coerced_to_tuple(tmp_path):
    body = """
[[algorithm]]
type = "insider_flow"
name = "if"
mode = "paper"
[algorithm.params]
exclude_title_patterns = ["foo", "bar"]
webhook_url = "http://hook"
"""
    name, d = _write_profile(tmp_path, body)
    (algo,) = load_profile(name, REGISTRY, config_dir=d)
    assert algo.params.exclude_title_patterns == ("foo", "bar")


def test_params_validate_failure_names_block(tmp_path):
    body = """
[[algorithm]]
type = "insider_flow"
name = "if"
mode = "paper"
[algorithm.params]
max_entry_odds = 1.5
"""
    name, d = _write_profile(tmp_path, body)
    with pytest.raises(ProfileError, match="max_entry_odds"):
        load_profile(name, REGISTRY, config_dir=d)


# ---------------------------------------------------------------------------
# Params schema — fail-safe defaults, instance separation
# ---------------------------------------------------------------------------


def test_default_mode_is_paper():
    """Fail-safe: a bare params object is PAPER unless a profile says LIVE."""
    assert CopyTradeParams().mode == Mode.PAPER


def test_two_instances_have_distinct_names_and_modes():
    """The same class, instantiated twice with different params, must keep
    those params separate — proves no leftover class-level mutable state."""
    from dataclasses import replace

    base = CopyTradeParams()
    a = CopyTradeAlgorithm(
        params=replace(base, name="copy_trade_prod", mode=Mode.LIVE),
    )
    b = CopyTradeAlgorithm(
        params=replace(base, name="copy_trade_paper", mode=Mode.PAPER),
    )

    assert a.params.name == "copy_trade_prod"
    assert a.params.mode == Mode.LIVE
    assert b.params.name == "copy_trade_paper"
    assert b.params.mode == Mode.PAPER

    # Internal caches must also be separate (not shared at class level).
    a.holding_cache.set("m1", 100_000.0)
    assert b.holding_cache.get("m1") is None


def test_name_kwarg_clones_default_params():
    """The shortcut `CopyTradeAlgorithm(name="x")` should produce default
    params under the new name without a full params object."""
    a = CopyTradeAlgorithm(name="copy_trade_variant")
    assert a.params.name == "copy_trade_variant"
