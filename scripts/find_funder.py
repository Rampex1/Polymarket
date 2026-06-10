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

# USDC.e on Polygon — the collateral Polymarket uses.
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Try eth_account / web3 if installed; fall back to a tiny JSON-RPC call.
try:
    from web3 import Web3
    _w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    def usdc_balance(addr: str) -> float:
        # ERC20 balanceOf(address) selector
        data = "0x70a08231" + "0" * 24 + addr.lower().replace("0x", "")
        raw = _w3.eth.call({"to": USDC_E, "data": data})
        return int(raw.hex() or "0x0", 16) / 1_000_000
except Exception:
    import requests
    def usdc_balance(addr: str) -> float:
        data = "0x70a08231" + "0" * 24 + addr.lower().replace("0x", "")
        r = requests.post(
            "https://polygon-rpc.com",
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                  "params": [{"to": USDC_E, "data": data}, "latest"]},
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
    try:
        bal = usdc_balance(addr)
        print(f"  USDC.e balance on Polygon: ${bal:,.2f}")
    except Exception as e:
        print(f"  USDC balance lookup failed: {e!r}")

    try:
        trades = fetcher.fetch_recent_trades(addr, limit=5)
        print(f"  Recent Polymarket trades:  {len(trades)}")
        for t in trades[:3]:
            print(f"    - {t.action} {t.outcome!r} ${t.size_usdc:.2f} @ {t.price:.3f} | {t.question[:50]}")
    except Exception as e:
        print(f"  Activity lookup failed: {e!r}")

print()
print("Reading the result:")
print("  * If FUNDER shows your USDC balance AND your trades → FUNDER is correct.")
print("  * If FUNDER shows $0 and 0 trades but EOA shows them → swap them.")
print("  * If neither shows anything → the real proxy is a third address;")
print("    grab it from polymarket.com → Deposit (Settings → Wallet).")
