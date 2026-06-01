"""
One-time script to generate Polymarket CLOB API keys from your private key.
Run once, then save the output values to your .env file.

Usage:
    POLY_PRIVATE_KEY=0x... python scripts/setup_keys.py
"""

import os
import sys
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

CLOB_API = "https://clob.polymarket.com"

private_key = os.getenv("POLY_PRIVATE_KEY", "")
if not private_key:
    print("Error: set POLY_PRIVATE_KEY in your environment before running this script.")
    sys.exit(1)

print("Connecting to Polymarket CLOB...")
client = ClobClient(host=CLOB_API, key=private_key, chain_id=POLYGON)

print("Generating API key...")
creds = None
for nonce in range(5):
    try:
        creds = client.create_api_key(nonce=nonce)
        if creds:
            break
    except Exception as e:
        print(f"  nonce={nonce} failed: {e}")

if creds is None:
    print("Failed to generate API key. Check that your private key is correct.")
    sys.exit(1)

print("\nAdd these to your .env file:\n")
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
