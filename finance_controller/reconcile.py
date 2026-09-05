"""
reconcile.py
------------
The reconciliation controller. This file NEVER opens ground_truth.csv -
everything here works only from internal_ledger.csv (what Razorpay's
records say should have happened) and bank_statement.csv (what actually
happened), exactly like a real finance team would.

Pipeline (matches the brief step by step):
    1. normalize()                 - clean references, parse dates
    2. detect_duplicate_*()        - flag duplicates before matching
    3. generate_candidates()       - exact-reference AND amount+date candidates
    4. score_candidate()           - transparent, evidence-based confidence
    5. find contended bank rows    - two ledger entries wanting one credit
    6. decide()                    - AUTO_RESOLVE / HUMAN_REVIEW / ABSTAIN / EXCEPTION
    7. classify_case_type()        - which of the four exception classes, if any
    8. write matches.csv + exceptions.csv, with full evidence preserved

Run:
    python reconcile.py
"""

import json

import pandas as pd

import config


# ---------------------------------------------------------------------------
# Step 1: normalize
# ---------------------------------------------------------------------------

def normalize(ledger_raw, bank_raw):
    ledger = ledger_raw.copy()
    bank = bank_raw.copy()

    ledger["bank_reference"] = ledger["bank_reference"].fillna("").astype(str).str.strip().str.upper()
    bank["bank_reference"] = bank["bank_reference"].fillna("").astype(str).str.strip().str.upper()

    ledger["created_at"] = pd.to_datetime(ledger["created_at"])
    ledger["settled_at"] = pd.to_datetime(ledger["settled_at"])
    bank["value_date"] = pd.to_datetime(bank["value_date"])

    # A settlement (type=payment) expects a CREDIT; a refund expects a DEBIT.
    ledger["direction"] = ledger["type"].map({"payment": "credit", "refund": "debit"})
    return ledger, bank


# ---------------------------------------------------------------------------
# Step 2: duplicate detection (done BEFORE candidate matching)
# ---------------------------------------------------------------------------

def detect_duplicate_bank_rows(bank):
    """bank_txn_ids that share (reference, amount, direction) with another row."""
    dup_ids = set()
    for _, group in bank.groupby(["bank_reference", "amount", "txn_type"]):
        if len(group) > 1:
            dup_ids.update(group["bank_txn_id"].tolist())
    return dup_ids


def detect_duplicate_ledger_rows(ledger):
    """entity_ids that share (payment_id, bank_reference, amount) with another row -
    e.g. a settlement.processed webhook that fired twice."""
    dup_ids = set()
    for _, group in ledger.groupby(["payment_id", "bank_reference", "amount"]):
        if len(group) > 1:
            dup_ids.update(group["entity_id"].tolist())
    return dup_ids


# ---------------------------------------------------------------------------
# Steps 3-4: candidate generation + evidence-based scoring
# ---------------------------------------------------------------------------

def score_candidate(ledger_row, bank_row):
    """Every number here is derived from actual evidence - never a flat
    "EXACT=100 / PROBABLE=75" style constant."""
    reference_match = 1.0 if (
        ledger_row["bank_reference"] and ledger_row["bank_reference"] == bank_row["bank_reference"]
    ) else 0.0

    amount_diff_rupees = abs(ledger_row["amount"] - bank_row["amount"]) / 100.0
    if amount_diff_rupees <= config.AMOUNT_EXACT_TOLERANCE_RUPEES:
        amount_score = 1.0
    elif amount_diff_rupees <= config.AMOUNT_VARIANCE_TOLERANCE_RUPEES:
        amount_score = 1.0 - 0.5 * (amount_diff_rupees / config.AMOUNT_VARIANCE_TOLERANCE_RUPEES)
    else:
        excess = amount_diff_rupees - config.AMOUNT_VARIANCE_TOLERANCE_RUPEES
        amount_score = max(0.0, 0.5 - 0.5 * (excess / (config.AMOUNT_VARIANCE_TOLERANCE_RUPEES * 4)))

    date_gap_days = abs((bank_row["value_date"] - ledger_row["settled_at"]).days)
    if date_gap_days <= config.SETTLEMENT_WINDOW_DAYS:
        date_score = 1.0
    elif date_gap_days <= config.DATE_PROBABLE_WINDOW_DAYS:
        span = config.DATE_PROBABLE_WINDOW_DAYS - config.SETTLEMENT_WINDOW_DAYS
        date_score = 1.0 - 0.5 * ((date_gap_days - config.SETTLEMENT_WINDOW_DAYS) / span)
    else:
        date_score = max(0.0, 0.5 - 0.05 * (date_gap_days - config.DATE_PROBABLE_WINDOW_DAYS))

    confidence = 100 * (
        config.WEIGHT_REFERENCE_MATCH * reference_match
        + config.WEIGHT_AMOUNT_AGREEMENT * amount_score
        + config.WEIGHT_DATE_PROXIMITY * date_score
    )

    return {
        "bank_txn_id": bank_row["bank_txn_id"],
        "bank_amount": bank_row["amount"],
        "bank_value_date": bank_row["value_date"],
        "reference_match": reference_match,
        "amount_diff_rupees": round(amount_diff_rupees, 2),
        "date_gap_days": int(date_gap_days),
        "confidence_score": round(confidence, 1),
    }


def generate_candidates(ledger_row, bank):
    """Union of exact-reference candidates and amount+date 'probable'
    candidates, in the correct money-flow direction. ALL plausible
    candidates are scored - we never stop at the first hit."""
    same_direction = bank[bank["txn_type"] == ledger_row["direction"]]

    ref = ledger_row["bank_reference"]
    if ref:
        exact_ref = same_direction[same_direction["bank_reference"] == ref]
    else:
        exact_ref = same_direction.iloc[0:0]

    # No reference to anchor on, so amount must be near-exact (tight
    # tolerance) rather than merely "within variance" - see config.py.
    amount_ok = (same_direction["amount"] - ledger_row["amount"]).abs() / 100.0 <= config.PROBABLE_MATCH_AMOUNT_TOLERANCE_RUPEES
    date_ok = (same_direction["value_date"] - ledger_row["settled_at"]).dt.days.abs() <= config.DATE_PROBABLE_WINDOW_DAYS
    probable = same_direction[amount_ok & date_ok]

    pool = pd.concat([exact_ref, probable]).drop_duplicates(subset="bank_txn_id")
    candidates = [score_candidate(ledger_row, b) for _, b in pool.iterrows()]
    candidates.sort(key=lambda c: c["confidence_score"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Steps 6-7: decision + exception classification
# ---------------------------------------------------------------------------

def decide(candidate_count, best, self_ambiguous, bank_contended):
    """Returns (decision, reason). Every branch names the exact evidence
    that drove it - no branch ever asserts an unproven cause."""
    if candidate_count == 0:
        return "EXCEPTION", "No matching bank record was found within the reconciliation window."

    if bank_contended:
        return "ABSTAIN", "Another ledger entry claims the same bank transaction with comparable confidence."

    if self_ambiguous:
        return "ABSTAIN", "Two or more bank transactions match this ledger entry with comparable confidence."

    ref_exact = best["reference_match"] == 1.0
    amount_exact = best["amount_diff_rupees"] <= config.AMOUNT_EXACT_TOLERANCE_RUPEES
    amount_within_variance = best["amount_diff_rupees"] <= config.AMOUNT_VARIANCE_TOLERANCE_RUPEES
    within_window = best["date_gap_days"] <= config.SETTLEMENT_WINDOW_DAYS

    if ref_exact and amount_exact and within_window and best["confidence_score"] >= config.AUTO_RESOLVE_THRESHOLD:
        return "AUTO_RESOLVE", "Exact reference, exact amount, credited within the expected settlement window."

    if ref_exact and not amount_within_variance:
        return "EXCEPTION", "Reference matches but the amount differs beyond the acceptable variance tolerance."

    if ref_exact and amount_within_variance and not amount_exact:
        return "HUMAN_REVIEW", "Reference matches; amount is within tolerance but not exact."

    if ref_exact and amount_exact and not within_window:
        return "HUMAN_REVIEW", "Reference and amount match, but the credit landed outside the expected settlement window."

    if best["confidence_score"] >= config.HUMAN_REVIEW_THRESHOLD:
        return "HUMAN_REVIEW", "No exact reference match; amount and date evidence give a probable, unconfirmed match."

    return "EXCEPTION", "The best available candidate has too little evidence to resolve automatically."


def classify_case_type(ledger_type, decision, best, bank_contended, self_ambiguous):
    """Maps the decision + evidence onto one of the four named exception
    classes (plus the bonus duplicate class). This is the SAME function
    for every row - one controller, not one product per exception type.

    This mirrors decide()'s branches on purpose: whatever evidence made
    decide() choose HUMAN_REVIEW/EXCEPTION is exactly what determines the
    case_type here, so the two functions can never quietly disagree."""
    if decision == "AUTO_RESOLVE":
        return "CLEAN_MATCH"
    if bank_contended:
        return "PAYMENT_STATE_AMBIGUITY"
    if self_ambiguous:
        return "DUPLICATE_BANK_ENTRY"
    if ledger_type == "refund":
        # Whether the debit is missing entirely or just mismatched, a
        # refund-leg problem is always this class, never a settlement one.
        return "REFUND_RECONCILIATION_EXCEPTION"
    if best is None:
        return "MISSING_OR_DELAYED_SETTLEMENT"

    ref_exact = best["reference_match"] == 1.0
    amount_exact = best["amount_diff_rupees"] <= config.AMOUNT_EXACT_TOLERANCE_RUPEES
    within_window = best["date_gap_days"] <= config.SETTLEMENT_WINDOW_DAYS

    if ref_exact and not amount_exact:
        # Covers both the small (HUMAN_REVIEW) and large (EXCEPTION) variance
        # cases from decide() - severity already lives in the decision column.
        return "SETTLEMENT_VARIANCE"
    if ref_exact and amount_exact and not within_window:
        return "MISSING_OR_DELAYED_SETTLEMENT"
    return "PAYMENT_STATE_AMBIGUITY"


RECOMMENDED_ACTION = {
    "CLEAN_MATCH": "No action needed.",
    "SETTLEMENT_VARIANCE": "Compare against the settlement's fee/tax break-down; escalate to Razorpay settlements support if the gap remains unexplained.",
    "MISSING_OR_DELAYED_SETTLEMENT": "Re-check the bank statement after the settlement window elapses; if still missing, raise a ticket with the settlement_id and bank_reference.",
    "REFUND_RECONCILIATION_EXCEPTION": "Confirm refund status via the Refunds dashboard/API and cross-check the acquirer reference with the bank; escalate if the refund is more than a few days old.",
    "PAYMENT_STATE_AMBIGUITY": "Manually check settlement webhook logs to confirm whether a duplicate settlement.processed event was received.",
    "DUPLICATE_BANK_ENTRY": "Confirm with the bank whether one of the two credits is a duplicate deposit before adjusting the ledger.",
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def to_rupees(paise):
    return round(float(paise) / 100.0, 2)


def reconcile(ledger_raw, bank_raw):
    ledger, bank = normalize(ledger_raw, bank_raw)

    duplicate_bank_ids = detect_duplicate_bank_rows(bank)
    duplicate_ledger_ids = detect_duplicate_ledger_rows(ledger)

    # Step 3-4: candidates + scores for every ledger row.
    candidates_by_entity = {row["entity_id"]: generate_candidates(row, bank) for _, row in ledger.iterrows()}

    # Step 5: find bank transactions that more than one ledger row wants.
    claims = {}
    for entity_id, cands in candidates_by_entity.items():
        if cands and cands[0]["confidence_score"] >= config.HUMAN_REVIEW_THRESHOLD:
            claims.setdefault(cands[0]["bank_txn_id"], []).append(entity_id)
    contended_entities = {eid for claimants in claims.values() if len(claimants) > 1 for eid in claimants}

    rows = []
    for _, ledger_row in ledger.iterrows():
        entity_id = ledger_row["entity_id"]
        cands = candidates_by_entity[entity_id]
        candidate_count = len(cands)
        best = cands[0] if cands else None
        second = cands[1] if candidate_count > 1 else None

        self_ambiguous = bool(
            best and second and (best["confidence_score"] - second["confidence_score"]) < config.AMBIGUITY_MARGIN
        )
        bank_contended = entity_id in contended_entities

        decision, reason = decide(candidate_count, best, self_ambiguous, bank_contended)
        case_type = classify_case_type(ledger_row["type"], decision, best, bank_contended, self_ambiguous)

        if entity_id in duplicate_ledger_ids and case_type == "PAYMENT_STATE_AMBIGUITY":
            reason += " Duplicate ledger entries were also detected for this payment_id/reference/amount combination."
        if best and best["bank_txn_id"] in duplicate_bank_ids and case_type == "DUPLICATE_BANK_ENTRY":
            reason += " The matching bank transaction itself appears more than once in the statement."

        evidence = {
            "direction": ledger_row["direction"],
            "duplicate_ledger_group": entity_id in duplicate_ledger_ids,
            "duplicate_bank_group": bool(best and best["bank_txn_id"] in duplicate_bank_ids),
            "candidates": [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in c.items()}
                for c in cands[:3]
            ],
        }

        rows.append({
            "entity_id": entity_id,
            "case_type": case_type,
            "order_id": ledger_row["order_id"],
            "payment_id": ledger_row["payment_id"],
            "method": ledger_row.get("method", ""),
            "settlement_id": entity_id if ledger_row["type"] == "payment" else "",
            "refund_id": entity_id if ledger_row["type"] == "refund" else "",
            "bank_txn_id": best["bank_txn_id"] if best else "",
            "expected_amount": to_rupees(ledger_row["amount"]),
            "actual_amount": to_rupees(best["bank_amount"]) if best else "",
            "variance": round(to_rupees(best["bank_amount"]) - to_rupees(ledger_row["amount"]), 2) if best else "",
            "transaction_date": ledger_row["created_at"].date().isoformat(),
            "settlement_date": ledger_row["settled_at"].date().isoformat(),
            "bank_value_date": best["bank_value_date"].date().isoformat() if best else "",
            "candidate_count": candidate_count,
            "confidence_score": best["confidence_score"] if best else 0.0,
            "evidence": json.dumps(evidence),
            "reason": reason,
            "recommended_action": RECOMMENDED_ACTION[case_type],
            "decision": decision,
            "status": "RESOLVED" if decision == "AUTO_RESOLVE" else "OPEN",
        })

    result_df = pd.DataFrame(rows)
    return result_df


def main():
    ledger_raw = pd.read_csv(config.INTERNAL_LEDGER_CSV)
    bank_raw = pd.read_csv(config.BANK_STATEMENT_CSV)

    result_df = reconcile(ledger_raw, bank_raw)

    matches_df = result_df[result_df["decision"] == "AUTO_RESOLVE"].copy()
    exceptions_df = result_df[result_df["decision"] != "AUTO_RESOLVE"].copy()

    # exceptions.csv gets a friendly, sequential case_id for the demo/UI.
    exceptions_df = exceptions_df.reset_index(drop=True)
    exceptions_df.insert(0, "case_id", [f"EXC-{i + 1:03d}" for i in range(len(exceptions_df))])
    matches_df = matches_df.reset_index(drop=True)
    matches_df.insert(0, "case_id", [f"MATCH-{i + 1:03d}" for i in range(len(matches_df))])

    matches_df.to_csv(config.MATCHES_CSV, index=False)
    exceptions_df.to_csv(config.EXCEPTIONS_CSV, index=False)

    print(f"Reconciled {len(result_df)} ledger entities.")
    print(f"  AUTO_RESOLVE : {len(matches_df)}  -> {config.MATCHES_CSV}")
    print(f"  Needs attention (HUMAN_REVIEW/ABSTAIN/EXCEPTION): {len(exceptions_df)} -> {config.EXCEPTIONS_CSV}")
    print("\nBreakdown of what needs attention, by decision:")
    print(exceptions_df["decision"].value_counts().to_string())
    print("\nBreakdown of what needs attention, by case_type:")
    print(exceptions_df["case_type"].value_counts().to_string())


if __name__ == "__main__":
    main()
