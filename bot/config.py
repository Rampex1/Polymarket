import os
from dotenv import load_dotenv

load_dotenv()

# Target user to copy
TARGET_USERNAME = "surfandturf"
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS", "")

# Polling
POLL_INTERVAL_SECONDS = 20

# Trade filters
MIN_TRADE_SIZE_USDC = 0.0

# Polymarket API base URLs
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# ── Execution ────────────────────────────────────────────────────────────────

# Set to True to log orders without sending them to the exchange
PAPER_TRADE: bool = os.getenv("PAPER_TRADE", "true").lower() != "false"

# Starting virtual balance for paper trading
PAPER_STARTING_BALANCE: float = float(os.getenv("PAPER_STARTING_BALANCE", "20.0"))

# Proportion of surfandturf's size to copy
SCALE_FACTOR: float = float(os.getenv("SCALE_FACTOR", "0.01"))

# Hard cap per trade as a fraction of paper balance
MAX_TRADE_PCT: float = float(os.getenv("MAX_TRADE_PCT", "0.20"))

# Order type: "market" (FOK) or "limit" (GTC at signal price)
ORDER_TYPE: str = os.getenv("ORDER_TYPE", "market")

# Per-order floor — skip if scaled size is below this
MIN_ORDER_SIZE_USDC: float = float(os.getenv("MIN_ORDER_SIZE_USDC", "0.0"))

# Max allowed price movement since signal (fraction, e.g. 0.05 = 5%)
MAX_SLIPPAGE: float = float(os.getenv("MAX_SLIPPAGE", "0.05"))

# Your Polymarket credentials (EOA private key + CLOB API keys)
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
# Your proxy wallet address (shown in Polymarket profile URL)
POLY_FUNDER_ADDRESS: str = os.getenv("POLY_FUNDER_ADDRESS", "")

# ── Risk limits ──────────────────────────────────────────────────────────────

# Max USDC cost allowed in a single market position
MAX_POSITION_SIZE_USDC: float = float(os.getenv("MAX_POSITION_SIZE_USDC", "500.0"))

# Max USDC exposure across all open positions combined
MAX_TOTAL_EXPOSURE_USDC: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "2000.0"))

# Stop all new BUYs if realized losses today exceed this
DAILY_LOSS_LIMIT_USDC: float = float(os.getenv("DAILY_LOSS_LIMIT_USDC", "200.0"))

# ── Notifications ────────────────────────────────────────────────────────────

# Get a bot token from @BotFather on Telegram, then /start your bot to get chat ID
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Storage ──────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "positions.db")
