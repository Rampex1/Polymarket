"""
Diagnostic — does the CLOB recognise this EOA/API-key as authorized to
trade for POLY_FUNDER_ADDRESS, under each signature_type?

Usage on the VM:
    source .venv/bin/activate
    python scripts/probe_clob_auth.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config  # noqa: E402
from py_clob_client.client import ClobClient  # noqa: E402
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams  # noqa: E402


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


for sig in (1, 2):
    print(f"\n=== signature_type={sig} ===")
    try:
        ba = make(sig).get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig)
        )
        print("balance_allowance:", ba)
    except Exception as e:
        print("error:", repr(e))
