"""
webhook.py

The Discord webhook transport — one fire-and-forget POST, nothing else.
Every sender goes through here, so none of them import each other.
"""

import logging

import requests as http

logger = logging.getLogger(__name__)

# Discord hard-caps message content at 2000 chars.
_DISCORD_MAX_LEN = 2000


def send(text: str, webhook_url: str = "") -> None:
    """Fire-and-forget Discord webhook message.

    Each algorithm carries its own `webhook_url`; there is no global
    fallback, so an algorithm without one simply doesn't notify.
    """
    if not webhook_url:
        return
    url = webhook_url
    try:
        http.post(url, json={"content": text[:_DISCORD_MAX_LEN]}, timeout=5)
    except Exception as e:
        logger.warning("Discord send failed: %s", e)
