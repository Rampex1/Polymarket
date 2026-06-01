# Scripts

| Script | Description |
|---|---|
| `setup_keys.py` | One-time generator for Polymarket CLOB API credentials. Run with `POLY_PRIVATE_KEY=0x... python scripts/setup_keys.py` and paste the output into `.env`. |
| `reset.py` | Deletes `positions.db` and reinitialises it with a fresh paper balance. Use before switching from paper to live trading, or to start over. |
| `summary.py` | Prints a formatted terminal summary of your current balance, open positions, and P&L. |
| `ssh_vm.sh` | SSH into the Oracle Cloud VM where the bot runs 24/7. Run with `bash scripts/ssh_vm.sh`. |
