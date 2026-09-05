# AI Finance Controller — Multi-Source Payment & Settlement Reconciliation

**Razorpay AI Buildathon — Track 04**

> An AI Finance Controller that reconciles Razorpay-style payment, settlement
> and refund records, automatically resolves high-confidence financial
> matches, abstains on ambiguous cases, quantifies unresolved cash exposure,
> and gives finance teams an evidence-backed explanation of what still needs
> attention.

The point of this project is **not** the match rate. Any matcher can hit
99% by guessing on the hard cases. The point is that every decision is
backed by evidence, every uncertain case is flagged instead of guessed, and
every unresolved rupee is counted and explained.

---

## The money trail this project reconciles

```
Order ──> Payment ──> Settlement ──> Bank credit
                 └──> Refund ──> Bank debit
```

- **Internal ledger** (`internal_ledger.csv`) = what Razorpay's own records
  say happened (built from `settlements.csv` + `refunds.csv`).
- **Bank statement** (`bank_statement.csv`) = what the bank actually shows.

Real reconciliation problems are mismatches *between* these two sides, so
that's exactly where the synthetic data generator injects its exceptions —
never into the ledger's own internal logic.

---

## Files

| File | Role |
|---|---|
| `config.py` | Every path, threshold, and weight in one place. No magic numbers elsewhere. |
| `generate_data.py` | Builds the synthetic, internally-consistent money trail + a hidden `ground_truth.csv`. |
| `reconcile.py` | The controller: normalizes, generates candidates, scores evidence, decides, classifies. Never reads ground truth. |
| `evaluate.py` | Grades `reconcile.py`'s output against ground truth. The **only** file allowed to read `ground_truth.csv`. |
| `agent_explain.py` | Lightweight grounded Q&A/explainer over `matches.csv` + `exceptions.csv`. |
| `requirements.txt` | `pandas` + `anthropic`. That's it. |
| `.env.example` | Copy to `.env` for your `ANTHROPIC_API_KEY` (optional). |

Running the pipeline creates a `data/` folder with every CSV listed above.

---

## Quick start

```bash
pip install -r requirements.txt

python generate_data.py   # -> data/*.csv (100 orders, one coherent money trail)
python reconcile.py       # -> data/matches.csv, data/exceptions.csv
python evaluate.py        # -> scorecard printed + data/metrics.json
python agent_explain.py "Why is EXC-004 unresolved?"
```

`generate_data.py` prints the injected case mix (for your own sanity-check —
`reconcile.py` never sees this). `reconcile.py` prints a breakdown of what
it auto-resolved vs. what needs attention. `evaluate.py` prints the
scorecard described below.

### Using the AI agent

```bash
cp .env.example .env        # optional — fill in ANTHROPIC_API_KEY
python agent_explain.py                              # interactive mode
python agent_explain.py "give me a summary of exceptions"
python agent_explain.py "explain EXC-008"
python agent_explain.py "Which EMI transactions require review?"
python agent_explain.py "Why did Razorpay charge exactly Rs 350?"
```

**No API key configured?** The agent still works. It falls back to a
deterministic, template-based responder that reads the exact same
structured evidence an LLM would get — so the grounding and abstention
behaviour is testable and demo-safe even with no network/API access.

---

## The four exception scenarios (+ one bonus)

All five are handled by the **same** controller (`reconcile.py`) — they are
exception *classes*, not separate products.

| `case_type` | What it represents | How it's injected |
|---|---|---|
| `SETTLEMENT_VARIANCE` | Bank credit differs from the ledger's expected amount, unexplained by fee/tax. | Same reference, amount nudged by an unexplained rupee amount. |
| `MISSING_OR_DELAYED_SETTLEMENT` | Settlement exists in the ledger but the bank credit is absent or very late. | No bank row at all, **or** a bank row 5–10 days past the settlement window. |
| `REFUND_RECONCILIATION_EXCEPTION` | Refund exists but the bank-side debit is missing or under a different reference. | No debit row, **or** a debit with a scrambled acquirer reference. |
| `PAYMENT_STATE_AMBIGUITY` | Two ledger entries plausibly claim one bank credit (e.g. a duplicated `settlement.processed` webhook). | Two settlement rows, same UTR/amount, one real bank credit. |
| `DUPLICATE_BANK_ENTRY` *(bonus)* | One ledger entry, two equally-plausible bank credits. | One settlement row, two identical bank credits. |

---

## How the matcher decides (transparency by design)

**1. Normalize** references (trim/upper-case) and parse dates.

**2. Detect duplicates first** — both duplicate ledger entries (same
`payment_id` + reference + amount) and duplicate bank rows (same reference +
amount + direction) are flagged *before* any matching happens.

**3. Generate every plausible candidate**, not just the first hit:
   - **Exact-reference candidates**: bank rows whose reference matches the
     ledger row's, in the correct direction (credit for a settlement, debit
     for a refund) — regardless of amount, so a variance is still surfaced.
   - **Probable candidates**: bank rows with a *near-exact* amount
     (± `PROBABLE_MATCH_AMOUNT_TOLERANCE_RUPEES`, default ₹2) and a nearby
     date, used only when there's no reference to anchor on. This tolerance
     is intentionally much tighter than the settlement-variance tolerance —
     without a reference, a loose amount match would let unrelated bank
     rows "coincidentally" qualify.

**4. Score every candidate from evidence**, never a flat constant:

```
confidence = 100 × ( 0.50 × reference_match
                    + 0.35 × amount_agreement
                    + 0.15 × date_proximity )
```

`amount_agreement` decays smoothly from 1.0 (exact) down through the
variance-tolerance band to 0. `date_proximity` is 1.0 inside the settlement
window, decays through the "probable" window, and keeps decaying slowly
beyond it (so a very late credit still shows up, just with lower
confidence).

**5. Look for contention.** If a single bank transaction is the top pick of
*more than one* ledger row, none of them can safely claim it →
`PAYMENT_STATE_AMBIGUITY`. If a single ledger row has two candidates whose
scores are within `AMBIGUITY_MARGIN` (default 8 points) of each other, it's
`DUPLICATE_BANK_ENTRY`. **Similarly plausible candidates always abstain
rather than guess.**

**6. Decide**, via named, explicit rules — not just a threshold on one
opaque number:

| Decision | Condition |
|---|---|
| `AUTO_RESOLVE` | Exact reference **and** exact amount **and** within the settlement window **and** confidence ≥ 90. |
| `HUMAN_REVIEW` | Reference matches but amount is within tolerance (not exact), **or** reference+amount match but timing is late, **or** a strong reference-less probable match. |
| `ABSTAIN` | Contended bank row or self-ambiguous candidates (see step 5). |
| `EXCEPTION` | No candidate at all, **or** reference matches but the amount variance is beyond tolerance, **or** the best candidate is too weak to act on. |

Every row's `evidence` column keeps the top-3 candidates and their scores,
so any decision can be re-derived by a human without re-running the code.

---

## Ground truth & the evaluation scorecard

`generate_data.py` privately records, per ledger entity, which scenario it
injected and what a correct reconciler should conclude. `reconcile.py`
**never reads this file** — only `evaluate.py` does, purely to grade the
result after the fact. Running `python evaluate.py` prints:

- **`auto_match_precision`** — of everything auto-resolved, how much was
  genuinely a clean match.
- **`false_reconciliation_rate`** — of everything auto-resolved, how much
  should *not* have been (the metric that punishes over-matching).
- **`exception_detection_recall`** — of every real injected problem, how
  much did the controller correctly *not* rubber-stamp.
- **`resolution_rate`** — share of all ledger entities resolved without a
  human.
- **`unresolved_count` / `unresolved_rupee_value`** — open `EXCEPTION` +
  `ABSTAIN` cases and the cash they represent.
- **`human_review_count` / `human_review_value`** — cases routed to a human
  and the cash they represent.
- **Resolution rate by payment method** — e.g. "9/24 EMI cases auto-resolved" —
  so a question like *"which EMI transactions need review?"* has a real,
  data-backed answer (`agent_explain.py` answers this directly; see below).

Payment method (UPI/Card/EMI/Netbanking/Wallet) is carried through as a
plain attribute on every ledger entity, exactly as it exists in Razorpay's
own settlement data — it does not create a second reconciliation path.

---

## The AI agent's grounding contract

`agent_explain.py` builds a small JSON "context" block for every question by
looking it up in `matches.csv` / `exceptions.csv` — by case ID (`EXC-004`),
by a Razorpay-style entity ID (`pay_...`, `setl_...`, `rfnd_...`), or by a
rupee amount mentioned in the question. **That JSON block, and nothing
else, is what the model is allowed to use.** The system prompt explicitly
forbids outside knowledge and requires an abstention sentence when the
evidence doesn't cover the question — e.g. asking about an amount that
doesn't appear in any case returns:

> "The available reconciliation data does not contain a case matching
> 'Rs 350.0'."

rather than a guess.

---

## Demo script

1. **Clean exact match** — `python agent_explain.py "explain MATCH-001"`
2. **Variance match** — `python agent_explain.py "explain EXC-008"` (`SETTLEMENT_VARIANCE`)
3. **Duplicate/ambiguous case** — find a `DUPLICATE_BANK_ENTRY` or
   `PAYMENT_STATE_AMBIGUITY` row in `exceptions.csv` and explain it.
4. **A settlement/refund exception** — any `MISSING_OR_DELAYED_SETTLEMENT`
   or `REFUND_RECONCILIATION_EXCEPTION` row.
5. **AI investigation** — `python agent_explain.py "give me a summary of exceptions"`
6. **A question the AI correctly refuses** —
   `python agent_explain.py "Why did Razorpay charge exactly Rs 350?"`

---

## Razorpay documentation this data model is based on

Synthetic fields mirror Razorpay's publicly documented entities and states
only — no claims are made about Razorpay's internal/proprietary logic:

- **Payments**: `payment_id`, `order_id`, `amount`, `currency`, `status`,
  `method`, `created_at`/`captured_at`; methods UPI/Card/Netbanking/
  Wallet/EMI.
- **Settlement reconciliation**: `entity_id`, `type` (payment/refund),
  `debit`/`credit`, `amount`, `fee`, `tax`, `settlement_id`, `payment_id`,
  a bank reference (UTR), `order_id`, `created_at`/`settled_at`.
- **Settlement webhook**: the important documented nuance that
  `settlement.processed` means the transfer was *initiated*, not that the
  bank credit necessarily landed immediately — this is exactly the
  `MISSING_OR_DELAYED_SETTLEMENT` scenario.
- **Refunds**: `refund_id`, `payment_id`, `amount`, `status`, an acquirer
  reference (RRN/ARN/UTR), `created_at`.
- **Orders**: `order_id`, `amount` (paise), `currency`, `receipt`.

EMI appears only as a payment method attribute, not a loan-servicing
feature — out of scope on purpose.

## Scope & deliberate simplifications

- Every payment used in the demo is `captured` — payment-state transitions
  (`created`/`authorized`/`failed`) aren't modeled, since the focus is
  post-capture reconciliation.
- `dispute_id`/adjustment-type ledger entities are out of scope for this MVP.
- Fee/tax (`MDR_RATE`, `GST_RATE` in `config.py`) is a simplified
  approximation used only to build synthetic settlement amounts, not a
  claim about Razorpay's real pricing.
- Refunds in the synthetic data are always full refunds, to keep the
  dataset easy to read end to end.

## What broke during development (and how it was caught)

Two real bugs were found and fixed while building this, both by comparing
`reconcile.py`'s output against `ground_truth.csv` via `evaluate.py`
rather than by inspection:

1. **Loose "probable" matching tolerance caused false ambiguity.** The
   no-reference amount+date fallback originally reused the ±₹50 settlement-
   variance tolerance. With ~100 ledger rows and ~90 bank rows in a 45-day
   window, that was loose enough for unrelated rows to coincidentally
   satisfy it, which fed spurious "claims" into the contention check and
   mislabeled several clean matches and missing settlements as
   `PAYMENT_STATE_AMBIGUITY`. Fix: a much tighter, separate
   `PROBABLE_MATCH_AMOUNT_TOLERANCE_RUPEES` (±₹2) for reference-less
   candidates only — the ±₹50 tolerance still applies once a reference has
   already anchored the pair.
2. **A missing branch in the exception classifier.** `classify_case_type()`
   had no explicit rule for "reference matches, amount within tolerance but
   not exact," so those genuine `SETTLEMENT_VARIANCE` cases fell through to
   the generic ambiguity fallback. Fix: added the missing branch so it
   mirrors `decide()`'s own branches exactly, so the two functions can't
   silently disagree again.

After both fixes, `evaluate.py` reports 100% `auto_match_precision` and
100% `exception_detection_recall` against the hidden ground truth on the
default seed (and this was re-checked against a second random seed to
confirm it wasn't a fluke of one dataset).

## Configuration

Every threshold used above lives in `config.py`: `NUM_ORDERS`, `CASE_MIX`,
`SETTLEMENT_WINDOW_DAYS`, the amount tolerances, `AUTO_RESOLVE_THRESHOLD`,
`HUMAN_REVIEW_THRESHOLD`, `AMBIGUITY_MARGIN`, and the confidence-score
weights. Nothing in `reconcile.py` or `generate_data.py` hardcodes a number
that isn't named and documented there.
