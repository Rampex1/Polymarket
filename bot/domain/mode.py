"""
mode.py

Paper or live, chosen independently for each algorithm — there is no
global switch, so one process can run both side by side.
"""

from enum import Enum


class Mode(str, Enum):
    """Execution mode selected independently for each algorithm."""

    PAPER = "paper"
    LIVE = "live"
