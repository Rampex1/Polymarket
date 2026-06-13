"""
Discord thread registry — maps (market_id, algo, paper) → Discord thread_id.

When a position first opens, the BUY notification is sent with ?wait=true to
capture the message's channel_id and id, then the Discord REST API creates a
public thread on that message. All subsequent notifications for the same
position route to ?thread_id=<id> so they land in the thread instead of
flooding the main channel.

Requires DISCORD_BOT_TOKEN. Without it, open_thread() falls back to a plain
webhook send and no thread is created — the rest of the bot is unaffected.

All public functions swallow and log their own exceptions so a Discord API
failure never interrupts trading.
"""

import logging
import time

import requests as http

from . import config, db

logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
_session = http.Session()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get(market_id: str, algo_name: str, paper: bool) -> str | None:
    """Return the stored Discord thread_id, or None if not yet created."""
    try:
        row = db.get().execute(
            """
            SELECT thread_id FROM discord_threads
            WHERE market_id = ? AND algo = ? AND paper = ?
            """,
            (market_id, algo_name, 1 if paper else 0),
        ).fetchone()
        return row["thread_id"] if row else None
    except Exception:
        logger.exception("Failed to look up thread for %s/%s", market_id, algo_name)
        return None


def _save(market_id: str, algo_name: str, paper: bool, thread_id: str) -> None:
    try:
        db.get().execute(
            """
            INSERT INTO discord_threads (market_id, algo, paper, thread_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (market_id, algo, paper) DO UPDATE SET
                thread_id  = excluded.thread_id,
                created_at = excluded.created_at
            """,
            (market_id, algo_name, 1 if paper else 0, thread_id, int(time.time())),
        )
        db.get().commit()
    except Exception:
        logger.exception("Failed to save thread_id for %s/%s", market_id, algo_name)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route(webhook_url: str, market_id: str, algo_name: str, paper: bool) -> str:
    """Return webhook_url?thread_id=<id> if a thread exists, else the bare URL."""
    if not webhook_url:
        return webhook_url
    thread_id = get(market_id, algo_name, paper)
    if thread_id:
        return f"{webhook_url}?thread_id={thread_id}"
    return webhook_url


# ---------------------------------------------------------------------------
# Thread creation
# ---------------------------------------------------------------------------

def open_thread(
    text: str,
    webhook_url: str,
    market_id: str,
    algo_name: str,
    paper: bool,
    thread_name: str,
) -> None:
    """Send `text` to the main channel (wait=true) and create a Discord thread.

    Caller guarantees DISCORD_BOT_TOKEN is set. On success the thread_id is
    stored so `route()` picks it up for all subsequent sends. Any failure is
    logged and swallowed — a Discord hiccup must never interrupt trading.
    """
    try:
        resp = _session.post(
            f"{webhook_url}?wait=true",
            json={"content": text[:2000]},
            timeout=5,
        )
        if not resp.ok:
            logger.warning(
                "Webhook send (wait=true) failed %s: %s",
                resp.status_code, resp.text[:120],
            )
            return

        msg = resp.json()
        channel_id = msg.get("channel_id", "")
        message_id = msg.get("id", "")
        if not (channel_id and message_id):
            return

        name = (thread_name or f"Position {market_id[:8]}")[:100]
        t_resp = _session.post(
            f"{_DISCORD_API}/channels/{channel_id}/messages/{message_id}/threads",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            json={"name": name, "auto_archive_duration": 10080},
            timeout=5,
        )
        if not t_resp.ok:
            logger.warning(
                "Thread creation failed %s: %s",
                t_resp.status_code, t_resp.text[:120],
            )
            return

        thread_id = t_resp.json().get("id", "")
        if thread_id:
            _save(market_id, algo_name, paper, thread_id)
            logger.debug(
                "[%s] Opened Discord thread %s for market %s",
                algo_name, thread_id, market_id[:12],
            )
    except Exception:
        logger.exception("open_thread raised for %s/%s — continuing", market_id, algo_name)
