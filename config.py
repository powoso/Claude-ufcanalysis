"""Configuration for the UFC Betting Analyzer."""

import os

# --- API Keys ---
# Get a free key at https://the-odds-api.com/
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

# --- Database ---
DB_PATH = os.path.join(os.path.dirname(__file__), "ufc_analysis.db")

# --- Scraping ---
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 2.0  # seconds between requests
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --- Cache ---
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
CACHE_TTL_HOURS = 6  # re-fetch after this many hours

# --- Model ---
ELO_K_FACTOR = 32
ELO_INITIAL = 1500
RECENT_FIGHTS_WEIGHT = 5  # last N fights weighted more heavily
RECENCY_DECAY = 0.85  # weight multiplier per fight going back

# --- Betting ---
KELLY_FRACTION = 0.25  # fractional Kelly (25%)
EDGE_THRESHOLD = 0.05  # minimum 5% implied probability edge
BANKROLL = 1000  # default bankroll in units

# --- Odds API ---
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORT = "mma_mixed_martial_arts"
ODDS_REGIONS = "us"
ODDS_MARKETS = "h2h,totals"

# --- URLs ---
UFCSTATS_BASE = "http://www.ufcstats.com/statistics/events/completed"
UFCSTATS_UPCOMING = "http://www.ufcstats.com/statistics/events/upcoming"
UFCSTATS_FIGHT_DETAILS = "http://www.ufcstats.com/fight-details/"
UFCSTATS_FIGHTER = "http://www.ufcstats.com/fighter-details/"
UFCSTATS_EVENT = "http://www.ufcstats.com/event-details/"
SHERDOG_BASE = "https://www.sherdog.com"
