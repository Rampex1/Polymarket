"""
discord

Everything that talks to Discord:
  webhook.py      the fire-and-forget POST every sender shares
  messages.py     pure formatting and markdown escaping
  threads.py      (market_id, algo, paper) -> thread id, and the whole
                  thread lifecycle: route into one, or open the first
  alerts.py       one message per trade event
  heartbeat.py    the only scheduled message — proof the process is alive
  discord_bot.py  the slash commands — a text builder per command plus the
                  gateway wiring; the only file importing discord.py
"""
