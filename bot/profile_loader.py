"""
Declarative profile loader — config/<profile>.toml → list of Algorithms.

A profile file declares *what runs*: which algorithm types, under which
names, in which mode. How each is tuned lives in Python, in that type's
params dataclass — one source of truth per knob.

File shape:

    allow_live = true          # required (top-level) before any mode="live"
                               # block is accepted — a profile must opt in
                               # to real money explicitly

    [[algorithm]]
    type = "copy_trade"        # registry key → algorithms/<type>/
    name = "copy_trade_prod"   # DB partition key — keep stable once set
    mode = "live"              # "paper" or "live"; always explicit

Knob values are NOT here — they live in algorithms/<type>/params.py. A
profile declares only what runs; an [algorithm.params] block is rejected.

Validation is fail-fast and boot-time: unknown keys, duplicate names, bad
modes, a params block, and per-algorithm `validate()` failures all raise
ProfileError naming the file and block — a typo can never silently no-op.
"""

import os
import tomllib

from bot.domain.mode import Mode


CONFIG_DIR = "config"


class ProfileError(RuntimeError):
    """A profile file is missing or invalid. Message says what and where."""


def available_profiles(config_dir: str = CONFIG_DIR) -> list[str]:
    """Profile names runnable via PROFILE=<name>.

    config/ also holds non-profile TOML (e.g. the webhooks registry), so
    only files with [[algorithm]] blocks count.
    """
    if not os.path.isdir(config_dir):
        return []
    names = []
    for f in sorted(os.listdir(config_dir)):
        if not f.endswith(".toml"):
            continue
        try:
            with open(os.path.join(config_dir, f), "rb") as fh:
                if tomllib.load(fh).get("algorithm"):
                    names.append(f[: -len(".toml")])
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return names


def load_profile(profile: str, registry: dict, config_dir: str = CONFIG_DIR) -> list:
    """Build the algorithm list for `profile` from config/<profile>.toml.

    `registry` maps type string → (AlgorithmCls, ParamsCls); it is passed
    in (rather than imported) so this module stays import-cycle-free and
    tests can load profiles from a temp dir with a fake registry.
    """
    path = os.path.join(config_dir, f"{profile}.toml")
    if not os.path.exists(path):
        avail = ", ".join(available_profiles(config_dir)) or "none found"
        raise ProfileError(
            f"Profile '{profile}' not found at {path}. Available: {avail}."
        )

    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{path}: invalid TOML — {e}") from e

    blocks = data.get("algorithm", [])
    if not blocks:
        raise ProfileError(f"{path}: no [[algorithm]] blocks defined.")

    algorithms = []
    seen_names: set[str] = set()
    for i, block in enumerate(blocks):
        where = f"{path} [[algorithm]] #{i + 1}"

        for required in ("type", "name", "mode"):
            if required not in block:
                raise ProfileError(f"{where}: missing required key '{required}'.")

        algo_type, name = block["type"], block["name"]
        if algo_type not in registry:
            raise ProfileError(
                f"{where}: unknown type '{algo_type}'. "
                f"Known types: {', '.join(sorted(registry))}."
            )
        if name in seen_names:
            raise ProfileError(
                f"{where}: duplicate name '{name}' — names partition the DB "
                f"and must be unique within a profile."
            )
        seen_names.add(name)

        try:
            mode = Mode(str(block["mode"]).lower())
        except ValueError:
            raise ProfileError(
                f"{where}: mode must be 'paper' or 'live', got '{block['mode']}'."
            ) from None

        if mode is Mode.LIVE and data.get("allow_live") is not True:
            raise ProfileError(
                f"{where}: mode='live' but the profile does not set "
                f"`allow_live = true` (top level). A profile must opt in "
                f"to real-money trading explicitly."
            )

        algo_cls, params_cls = registry[algo_type]

        # Knob values live in algorithms/<type>/params.py, full stop. A
        # profile declares *what runs*, never how it is tuned — so a params
        # block here is a mistake, and a silently ignored one would be worse.
        if "params" in block:
            raise ProfileError(
                f"{where}: [algorithm.params] is not accepted. Knob values "
                f"live in algorithms/{algo_type}/params.py; a profile only "
                f"declares type, name, and mode."
            )

        params = params_cls(name=name, mode=mode)
        validate = getattr(params, "validate", None)
        if validate is not None:
            try:
                validate()
            except ValueError as e:
                raise ProfileError(f"{where}: {e}") from e

        algorithms.append(algo_cls(params=params))

    return algorithms
