"""
discord

Everything that talks to Discord:
  webhook.py      the fire-and-forget POST every sender shares
  messages.py     pure formatting and markdown escaping
  alerts.py       one message per trade event
  heartbeat.py    the only scheduled message — proof the process is alive
  threads.py      (market_id, algo, paper) -> thread id, so updates nest
  discord_bot.py  the slash commands, each with its own text builder
"""
