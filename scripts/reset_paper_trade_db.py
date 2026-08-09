#!/usr/bin/env python3
"""
Deletes positions.db and reinitialises every enabled algorithm's paper
balance from its own params.
"""

import os
import sys

# Allow running from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from algorithms import ENABLED
from bot import config
from bot.ledger import Ledger

db_path = config.DB_PATH

if os.path.exists(db_path):
    os.remove(db_path)
    print(f"Deleted {db_path}")

for algo in ENABLED:
    name = algo.params.name
    bal = algo.params.paper_starting_balance
    tracker = Ledger(algo=name)
    tracker.init_paper_balance(bal)
    print(f"Fresh paper account [{name}] created with ${bal:.2f}")
