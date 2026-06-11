"""
Diagnostic — verify whether POLY_FUNDER_ADDRESS is your real Polymarket
proxy wallet by checking on-chain activity & USDC balance.

If the configured funder shows zero activity, it isn't where your
trades and USDC actually live — the address in your polymarket.com
profile URL is sometimes the display EOA, not the proxy.

Usage:
    source .venv/bin/activate
    python scripts/find_funder.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account  # noqa: E402

from bot import config, fetcher  # noqa: E402

# Possible collateral tokens on Polygon. Polymarket historically used
# USDC.e (bridged) but has been migrating to native USDC.
TOKENS = {
    "USDC.e (bridged)": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC (native)":    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
}

try:
    from web3 import Web3
    _w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    def erc20_balance(token: str, addr: str) -> float:
        data = "0x70a08231" + "0" * 24 + addr.lower().replace("0x", "")
        raw = _w3.eth.call({"to": token, "data": data})
        return int(raw.hex() or "0x0", 16) / 1_000_000
except Exception:
    import requests
    def erc20_balance(token: str, addr: str) -> float:
        data = "0x70a08231" + "0" * 24 + addr.lower().replace("0x", "")
        r = requests.post(
            "https://polygon-rpc.com",
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                  "params": [{"to": token, "data": data}, "latest"]},
            timeout=10,
        )
        hex_val = r.json().get("result") or "0x0"
        return int(hex_val, 16) / 1_000_000


eoa = Account.from_key(config.POLY_PRIVATE_KEY).address
funder = config.POLY_FUNDER_ADDRESS

print("Candidates to probe:")
print(f"  EOA (from POLY_PRIVATE_KEY): {eoa}")
print(f"  FUNDER (in .env):            {funder}")

for label, addr in (("FUNDER", funder), ("EOA", eoa)):
    print(f"\n=== {label}  {addr} ===")
    for tname, taddr in TOKENS.items():
        try:
            bal = erc20_balance(taddr, addr)
            print(f"  {tname:20s} balance: ${bal:,.4f}")
        except Exception as e:
            print(f"  {tname:20s} balance lookup failed: {e!r}")

    try:
        trades = fetcher.fetch_recent_trades(addr, limit=5)
        print(f"  Recent Polymarket trades:  {len(trades)}")
        for t in trades[:3]:
            print(f"    - {t.action} {t.outcome!r} ${t.size_usdc:.2f} @ {t.price:.3f} | {t.question[:50]}")
    except Exception as e:
        print(f"  Activity lookup failed: {e!r}")

    # Open positions value via Data API (what Polymarket UI calls "Portfolio").
    try:
        positions = fetcher.fetch_user_positions(addr)
        open_value = sum(float(p.get("value") or p.get("currentValue") or 0) for p in positions)
        print(f"  Open-position USD value:    ${open_value:,.2f} ({len(positions)} markets)")
    except Exception as e:
        print(f"  Positions lookup failed: {e!r}")

print()
print("Reading the result:")
print("  * If FUNDER has trades + USDC.e or USDC balance → setup is right and you can trade.")
print("  * If FUNDER has trades but $0 of both USDC tokens, only open-position value → all")
print("    your collateral is locked in positions. Deposit cash before the bot can BUY.")
print("  * If FUNDER has the trades but balance is in USDC (native), Polymarket migrated;")
print("    the bot's collateral asset needs updating.")
