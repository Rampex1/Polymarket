"""
Profile-loader + params-schema tests.

Covers:
  * The shipped config/*.toml files load and have the right shapes/modes.
  * Loader validation: missing profile, missing keys, bad mode, unknown
    type, unknown param, omitted param, duplicate names, per-params
    validate().
  * TOML list → tuple coercion for tuple-typed fields.
  * Multiple instances of one algorithm class keep params separate.
"""

import importlib
from dataclasses import fields

import pytest

from algorithms import REGISTRY
from algorithms.copy_trade import CopyTradeAlgorithm, CopyTradeParams
from bot.domain.intents import Mode
from bot.profile_loader import ProfileError, load_profile
from tests.conftest import copy_trade_params, insider_flow_params


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


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (tuple, list)):
        return "[" + ", ".join(f'"{x}"' for x in v) + "]"
    return f'"{v}"'


_BASELINE = {"copy_trade": copy_trade_params, "insider_flow": insider_flow_params}


def _block(algo_type: str, name: str, mode: str, **overrides) -> str:
    """An [[algorithm]] block naming *every* knob — the loader requires it.

    Built from the conftest test baseline, so adding a knob to a schema
    breaks these once (in conftest) rather than in every fixture here.
    """
    _, params_cls = REGISTRY[algo_type]
    baseline = _BASELINE[algo_type]()
    lines = [
        f"{f.name} = {_toml(overrides.get(f.name, getattr(baseline, f.name)))}"
        for f in fields(params_cls) if f.name not in ("name", "mode")
    ]
    return (
        f'[[algorithm]]\ntype = "{algo_type}"\nname = "{name}"\n'
        f'mode = "{mode}"\n[algorithm.params]\n' + "\n".join(lines) + "\n"
    )


VALID = _block("copy_trade", "ct", "paper",
               target_address="0xabc", webhook_url="http://hook")

LIVE_BLOCK = _block("copy_trade", "ct_live", "live",
                    target_address="0xabc", webhook_url="http://hook")


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
        tmp_path, _block("copy_trade", "x", "paper", webhook_url="http://hook"))
    with pytest.raises(ProfileError, match="target"):
        load_profile(name, REGISTRY, config_dir=d)


def test_omitted_param_rejected(tmp_path):
    """A profile must state every knob — there is nothing to inherit."""
    body = "\n".join(
        line for line in VALID.splitlines() if not line.startswith("max_slippage")
    )
    name, d = _write_profile(tmp_path, body)
    with pytest.raises(ProfileError, match="missing 1 required param.*max_slippage"):
        load_profile(name, REGISTRY, config_dir=d)


def test_toml_list_coerced_to_tuple(tmp_path):
    name, d = _write_profile(tmp_path, _block(
        "insider_flow", "if", "paper",
        exclude_title_patterns=["foo", "bar"], webhook_url="http://hook"))
    (algo,) = load_profile(name, REGISTRY, config_dir=d)
    assert algo.params.exclude_title_patterns == ("foo", "bar")


def test_params_validate_failure_names_block(tmp_path):
    name, d = _write_profile(tmp_path, _block(
        "insider_flow", "if", "paper",
        max_entry_odds=1.5, webhook_url="http://hook"))
    with pytest.raises(ProfileError, match="max_entry_odds"):
        load_profile(name, REGISTRY, config_dir=d)


# ---------------------------------------------------------------------------
# Params schema — no defaults, instance separation
# ---------------------------------------------------------------------------


def test_schema_has_no_defaults():
    """Every knob is required, so nothing can be half-configured in code."""
    from dataclasses import MISSING

    with pytest.raises(TypeError, match="required positional argument"):
        CopyTradeParams()
    assert all(
        f.default is MISSING and f.default_factory is MISSING
        for f in fields(CopyTradeParams)
    )


def test_algorithm_requires_params():
    """No fallback to a default params object — a bare construction fails."""
    with pytest.raises(TypeError):
        CopyTradeAlgorithm()


def test_two_instances_have_distinct_names_and_modes():
    """The same class, instantiated twice with different params, must keep
    those params separate — proves no leftover class-level mutable state."""
    from dataclasses import replace

    base = copy_trade_params()
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
