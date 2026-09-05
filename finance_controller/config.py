"""
config.py
---------
Single place for every path, threshold, and weight used across the project.

Nothing in here is "AI" — it's just the dials a finance-ops person could
reasonably want to tune (settlement window, tolerance for a variance,
how confident the model must be before it auto-resolves something).
Keeping them here means reconcile.py and generate_data.py never have a
bare number like `90` or `5` floating in the middle of a formula.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

ORDERS_CSV = os.path.join(DATA_DIR, "orders.csv")
PAYMENTS_CSV = os.path.join(DATA_DIR, "payments.csv")
SETTLEMENTS_CSV = os.path.join(DATA_DIR, "settlements.csv")
REFUNDS_CSV = os.path.join(DATA_DIR, "refunds.csv")

INTERNAL_LEDGER_CSV = os.path.join(DATA_DIR, "internal_ledger.csv")
BANK_STATEMENT_CSV = os.path.join(DATA_DIR, "bank_statement.csv")

# The matcher (reconcile.py) must NEVER open this file. It exists purely
# so evaluate.py can grade the matcher's decisions after the fact.
GROUND_TRUTH_CSV = os.path.join(DATA_DIR, "ground_truth.csv")

MATCHES_CSV = os.path.join(DATA_DIR, "matches.csv")
EXCEPTIONS_CSV = os.path.join(DATA_DIR, "exceptions.csv")
METRICS_JSON = os.path.join(DATA_DIR, "metrics.json")

# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------
RANDOM_SEED = 42
NUM_ORDERS = 100

# Approximate mix of injected ground-truth scenarios. Must sum to 1.0.
# CLEAN_MATCH is the "happy path" - everything else is a deliberately
# injected exception used to test whether the matcher abstains correctly.
CASE_MIX = {
    "CLEAN_MATCH": 0.50,
    "SETTLEMENT_VARIANCE": 0.12,
    "MISSING_SETTLEMENT": 0.10,
    "DELAYED_SETTLEMENT": 0.08,
    "REFUND_EXCEPTION": 0.10,
    "PAYMENT_STATE_AMBIGUITY": 0.06,
    "DUPLICATE_BANK_ENTRY": 0.04,
}

PAYMENT_METHODS = ["UPI", "CARD", "NETBANKING", "WALLET", "EMI"]

# Simplified fee model used ONLY to build synthetic settlement amounts.
# This is not a claim about Razorpay's real pricing - just enough to make
# "settlement amount = payment amount - fee - tax" hold for clean cases.
MDR_RATE = 0.02   # merchant discount rate charged on the payment amount
GST_RATE = 0.18   # GST charged on top of the fee

# A settlement is expected to reach the bank within this many days of
# settlement being marked "processed" by the gateway.
SETTLEMENT_WINDOW_DAYS = 2

# ---------------------------------------------------------------------------
# Reconciliation thresholds (used by reconcile.py, never by generate_data.py)
# ---------------------------------------------------------------------------
# Amounts below this many rupees apart are treated as "exactly equal"
# (guards against paisa-level float noise).
AMOUNT_EXACT_TOLERANCE_RUPEES = 0.01

# A variance up to this many rupees is still recognisably "the same
# transaction" but must be flagged rather than silently auto-resolved.
AMOUNT_VARIANCE_TOLERANCE_RUPEES = 50.0

# A "probable" (no reference match) candidate is only considered if the
# ledger and bank dates are within this many days of each other...
DATE_PROBABLE_WINDOW_DAYS = 5

# ...AND the amount is essentially exact. Without a reference to anchor on,
# a wide amount tolerance would let unrelated bank rows "coincidentally"
# match just because they happen to fall in the same date window - this
# is intentionally much tighter than AMOUNT_VARIANCE_TOLERANCE_RUPEES,
# which only applies once a reference match already anchors the pair.
PROBABLE_MATCH_AMOUNT_TOLERANCE_RUPEES = 2.0

# Confidence score (0-100) needed to auto-resolve without a human looking.
# (In practice AUTO_RESOLVE also requires exact reference + exact amount +
# on-time credit - see decide() in reconcile.py. This threshold is a
# belt-and-suspenders numeric floor on top of those explicit checks.)
AUTO_RESOLVE_THRESHOLD = 90
# Below this, there isn't even enough evidence for a routed human review -
# it goes to EXCEPTION instead. Deliberately below the "exact amount, no
# reference, good date" score (~50) so a plausible-but-unconfirmed match
# still reaches a human instead of being silently written off.
HUMAN_REVIEW_THRESHOLD = 45

# If the best and second-best candidate for the same ledger entry score
# within this many points of each other, we cannot safely tell them apart
# -> ABSTAIN instead of guessing.
AMBIGUITY_MARGIN = 8

# Evidence weights for confidence_score. Must sum to 1.0.
WEIGHT_REFERENCE_MATCH = 0.50
WEIGHT_AMOUNT_AGREEMENT = 0.35
WEIGHT_DATE_PROXIMITY = 0.15
