#!/usr/bin/env python3
"""
Deletes positions.db and reinitialises it with a fresh $20 paper balance.
"""

import os
import sys

# Allow running from any directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, db
from bot.positions import PositionTracker

db_path = config.DB_PATH

if os.path.exists(db_path):
    os.remove(db_path)
    print(f"Deleted {db_path}")

tracker = PositionTracker()
tracker.init_paper_balance(config.PAPER_STARTING_BALANCE)
print(f"Fresh paper account created with ${config.PAPER_STARTING_BALANCE:.2f}")
