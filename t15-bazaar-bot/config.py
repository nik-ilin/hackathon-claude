"""Configuration: all tunable knobs in one place, loaded from env vars with sane defaults."""
import os

# ── Connection ──────────────────────────────────────────────────────────────
BAZAAR_URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
BAZAAR_KEY = os.environ["BAZAAR_KEY"]  # must be set
BROKER_KEY = os.environ.get("BROKER_KEY", "")  # set after opening venue

# ── Dealer Haggling ─────────────────────────────────────────────────────────
DEALER_START_RATIO = 0.55       # first offer as fraction of expected price (buying)
DEALER_SELL_START_RATIO = 1.45  # first ask as fraction of expected price (selling)
DEALER_STEP_RATIO = 0.08        # each concession as fraction of remaining gap
DEALER_ACCEPT_MARGIN = 0.05     # accept if within this fraction of our target
DEALER_MAX_ROUNDS = 20          # give up after this many rounds
DEALER_MAX_SCORING_DEALS = 3    # only the best 3 negotiated deals per dealer score (RULES.md)

# ── Duels ───────────────────────────────────────────────────────────────────
DUEL_BUYER_START = 0.55         # first offer as fraction of your_limit (buyer)
DUEL_SELLER_START = 1.50        # first ask as multiple of your_limit (seller)
DUEL_STEP_RATIO = 0.15          # move this fraction of remaining gap each round
DUEL_ACCEPT_THRESHOLD = 0.02   # accept if within 2% of limit (don't leave money on table)

# ── P2P Trading ─────────────────────────────────────────────────────────────
TRADE_DUPLICATE_DISCOUNT = 0.90  # sell duplicates at 90% of book value
TRADE_BUY_PREMIUM = 1.20         # willing to pay up to 120% of book for wanted cards
TRADE_LISTING_DURATION = 40      # ticks before listing expires
MAX_OPEN_OFFERS = 25             # keep some slots free for tactical offers

# ── Venue / Broker ──────────────────────────────────────────────────────────
VENUE_FEE_BPS = 0             # 1.5% fee — undercut El Rastro's 5%
VENUE_FEE_PER_CARD = 0          # no per-card fee
VENUE_MECHANISM = "board"       # must be board so our broker controls matching
VENUE_BOND = 270                # 250 refundable + 20 opening fee

# ── Smart Broker ────────────────────────────────────────────────────────────
BROKER_POLL_INTERVAL = 0.5      # seconds between book reads
BROKER_PATIENCE_WINDOW = 8      # ticks to observe before estimating limits
BROKER_IMPATIENT_THRESHOLD = 2  # if a trader moved price ≥2 times, they're impatient
BROKER_LIMIT_ESTIMATE_FACTOR = 0.7  # estimate limit is 70% of the way from quote to midpoint

# ── General ─────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
REFRESH_INTERVAL_TICKS = 1      # refresh game state every N ticks
LEADERBOARD_INTERVAL = 60       # seconds between leaderboard checks
