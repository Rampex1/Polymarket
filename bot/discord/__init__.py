"""
discord

Everything that talks to Discord:
  webhook.py      the fire-and-forget POST both senders share
  messages.py     pure formatting and markdown escaping
  alerts.py       one message per trade event
  summaries.py    profile-level digests — /summary, heartbeat, weekly
  threads.py      (market_id, algo, paper) -> thread id, so updates nest
  discord_bot.py  the slash-command bot, running as its own process
"""
