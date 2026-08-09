"""
params.py

Knob-discovery CLI: `python -m bot.params [type] [--effective]`.
Reads the params dataclasses, so it can't go stale the way prose docs do.
"""

import argparse
import sys
from dataclasses import fields


def _fmt(v) -> str:
    return getattr(v, "value", v).__repr__() if hasattr(v, "value") else repr(v)


def list_types(registry) -> None:
    print("Algorithm types (use as `type = \"...\"` in config/<profile>.toml):\n")
    for type_name, (algo_cls, _) in sorted(registry.items()):
        doc = (algo_cls.__doc__ or "").strip().splitlines()
        summary = doc[0] if doc else ""
        print(f"  {type_name:<14} {summary}")
    print("\nDetails: python -m bot.params <type>")


def show_schema(registry, algo_type: str) -> None:
    if algo_type not in registry:
        print(f"Unknown type '{algo_type}'. Known: {', '.join(sorted(registry))}",
              file=sys.stderr)
        sys.exit(1)
    _, params_cls = registry[algo_type]
    print(f"{algo_type} — every one of these is required in "
          f"[algorithm.params]\n")
    skip = {"name", "mode"}  # set at block level, not under params
    rows = [
        (f.name, getattr(f.type, "__name__", str(f.type)), f.metadata.get("doc", ""))
        for f in fields(params_cls) if f.name not in skip
    ]
    w_name = max(len(r[0]) for r in rows)
    w_type = max(len(r[1]) for r in rows)
    for name, type_name, doc in rows:
        print(f"  {name:<{w_name}}  {type_name:<{w_type}}  {doc}")


def show_effective() -> None:
    import algorithms

    from bot.profile_loader import ProfileError

    try:
        enabled = algorithms.ENABLED
    except ProfileError as e:
        sys.exit(f"error: {e}")
    print(f"PROFILE={algorithms.PROFILE} → config/{algorithms.PROFILE}.toml\n")
    for algo in enabled:
        p = algo.params
        algo_type = next(
            (t for t, (_, pc) in algorithms.REGISTRY.items() if isinstance(p, pc)),
            "?",
        )
        print(f"[{p.name}]  type={algo_type}  mode={p.mode.value}")
        for f in fields(p):
            if f.name in ("name", "mode"):
                continue
            print(f"    {f.name} = {_fmt(getattr(p, f.name))}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot.params")
    parser.add_argument("type", nargs="?", help="algorithm type to describe")
    parser.add_argument("--effective", action="store_true",
                        help="print the resolved config for the current PROFILE")
    args = parser.parse_args()

    if args.effective:
        show_effective()
        return

    from algorithms import REGISTRY
    if args.type:
        show_schema(REGISTRY, args.type)
    else:
        list_types(REGISTRY)


if __name__ == "__main__":
    main()
