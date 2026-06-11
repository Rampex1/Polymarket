"""
Probe Polymarket CLOB authorisation under py_clob_client v2 (which
supports the V2 CTF Exchange + new POLY_1271 smart-contract sig type).

Tries sig types 2 (GNOSIS_SAFE) and 3 (POLY_1271). The one that
returns non-zero allowances is the one the account uses.

Usage:
    pip install py_clob_client_v2
    python scripts/probe_v2.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config  # noqa: E402
from py_clob_client_v2.client import ClobClient  # noqa: E402
from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams  # noqa: E402


def make(sig: int) -> ClobClient:
    return ClobClient(
        host=config.CLOB_API,
        key=config.POLY_PRIVATE_KEY,
        chain_id=137,
        creds=ApiCreds(
            api_key=config.POLY_API_KEY,
            api_secret=config.POLY_API_SECRET,
            api_passphrase=config.POLY_API_PASSPHRASE,
        ),
        signature_type=sig,
        funder=config.POLY_FUNDER_ADDRESS,
    )


for sig in (2, 3):
    label = {2: "GNOSIS_SAFE", 3: "POLY_1271"}[sig]
    print(f"\n=== v2 signature_type={sig} ({label}) ===")
    try:
        ba = make(sig).get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)
        )
        print("balance_allowance:", ba)
    except Exception as e:
        print("error:", repr(e))
