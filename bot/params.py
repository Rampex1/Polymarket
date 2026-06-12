"""
Knob discovery CLI — answers "what can I configure?" and "what is actually
running?" without reading algorithm code.

    python -m bot.params                  # list algorithm types
    python -m bot.params copy_trade       # every knob: name, type, default, doc
    python -m bot.params --effective      # fully-resolved config for $PROFILE

`--effective` marks knobs that differ from the schema default with `*` —
that column is the entire diff between a profile and stock behavior.
"""

import argparse
import sys
from dataclasses import MISSING, fields


def _default_of(f):
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()        # type: ignore[misc]
    return None


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
    print(f"{algo_type} — knobs for [algorithm.params] in config/<profile>.toml\n")
    skip = {"name", "mode"}  # set at block level, not under params
    rows = [
        (f.name, getattr(f.type, "__name__", str(f.type)), _fmt(_default_of(f)),
         f.metadata.get("doc", ""))
        for f in fields(params_cls) if f.name not in skip
    ]
    w_name = max(len(r[0]) for r in rows)
    w_def = max(len(r[2]) for r in rows)
    for name, _type, default, doc in rows:
        print(f"  {name:<{w_name}}  {default:<{w_def}}  {doc}")


def show_effective() -> None:
    import algorithms  # profile resolves here — may exit with ProfileError

    enabled = algorithms.ENABLED
    print(f"PROFILE={algorithms.PROFILE} → config/{algorithms.PROFILE}.toml\n")
    for algo in enabled:
        p = algo.params
        algo_type = next(
            (t for t, (_, pc) in algorithms.REGISTRY.items() if isinstance(p, pc)),
            "?",
        )
        defaults = type(p)()
        print(f"[{p.name}]  type={algo_type}  mode={p.mode.value}")
        for f in fields(p):
            if f.name in ("name", "mode"):
                continue
            val = getattr(p, f.name)
            mark = " *" if val != getattr(defaults, f.name) else ""
            print(f"    {f.name} = {_fmt(val)}{mark}")
        print()
    print("(* = differs from schema default)")


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
