"""
generate_data.py
-----------------
Builds a synthetic but internally-consistent Razorpay-style money trail:

    Order -> Payment -> Settlement -> Bank credit
    Payment -> Refund -> Bank debit

Design principle
-----------------
settlements.csv / refunds.csv always represent what RAZORPAY'S OWN RECORDS
say happened (this becomes internal_ledger.csv). bank_statement.csv
represents what the BANK actually shows. Real reconciliation problems are
mismatches between these two sides, so that is exactly where this script
injects the four exception scenarios:

    1. SETTLEMENT_VARIANCE        -> bank amount != ledger amount
    2. MISSING/DELAYED SETTLEMENT -> bank credit absent or very late
    3. REFUND_EXCEPTION           -> refund debit missing / reference mismatch
    4. PAYMENT_STATE_AMBIGUITY    -> two ledger entries, one bank credit
       (+ bonus DUPLICATE_BANK_ENTRY -> one ledger entry, two bank credits)

A hidden ground_truth.csv records what was actually injected, per entity.
reconcile.py must NEVER read this file. It exists only so evaluate.py can
grade reconcile.py's decisions after the fact.

Run:
    python generate_data.py
"""

import os
import random
import string
from datetime import datetime, timedelta

import pandas as pd

import config

# A fixed anchor date keeps every run 100% reproducible (same seed -> same
# dates), which matters when you're comparing metrics across code changes.
BASE_DATE = datetime(2026, 8, 1)

ID_CHARS = string.ascii_letters + string.digits


def rand_id(prefix, rng, length=14):
    """Mimic Razorpay-style entity IDs, e.g. pay_9f8Ht2QwErX4aB."""
    suffix = "".join(rng.choices(ID_CHARS, k=length))
    return f"{prefix}_{suffix}"


def rand_utr(rng):
    """A bank UTR / acquirer reference: a 12-digit numeric string."""
    return "".join(rng.choices(string.digits, k=12))


def rand_txn_id(rng):
    return "TXN" + "".join(rng.choices(string.digits, k=10))


# ---------------------------------------------------------------------------
# Base entity builders (order, payment) - identical for every scenario
# ---------------------------------------------------------------------------

def build_order(index, rng):
    amount = rng.randint(10_000, 500_000)  # paise: ₹100 - ₹5,000
    created_at = BASE_DATE + timedelta(
        days=rng.randint(0, 45), minutes=rng.randint(0, 1439)
    )
    return {
        "order_id": rand_id("order", rng),
        "amount": amount,
        "currency": "INR",
        "receipt": f"rcpt_{index + 1:04d}",
        "notes": "{}",
        "created_at": created_at,
    }


def build_payment(order, rng):
    captured_at = order["created_at"] + timedelta(minutes=rng.randint(1, 30))
    return {
        "payment_id": rand_id("pay", rng),
        "order_id": order["order_id"],
        "amount": order["amount"],
        "currency": "INR",
        "status": "captured",
        "method": rng.choice(config.PAYMENT_METHODS),
        "created_at": order["created_at"],
        "captured_at": captured_at,
    }


def settlement_economics(amount, rng):
    """Simplified fee/tax split used only to build synthetic amounts."""
    fee = round(amount * config.MDR_RATE)
    tax = round(fee * config.GST_RATE)
    net_amount = amount - fee - tax
    return fee, tax, net_amount


def build_settlement(payment, rng, settled_offset_days=None):
    if settled_offset_days is None:
        settled_offset_days = config.SETTLEMENT_WINDOW_DAYS
    fee, tax, net_amount = settlement_economics(payment["amount"], rng)
    settled_at = payment["captured_at"] + timedelta(days=settled_offset_days)
    return {
        "settlement_id": rand_id("setl", rng),
        "payment_id": payment["payment_id"],
        "order_id": payment["order_id"],
        "amount": net_amount,
        "fee": fee,
        "tax": tax,
        "bank_reference": rand_utr(rng),
        "created_at": payment["captured_at"],
        "settled_at": settled_at,
    }


def build_refund(payment, settlement, rng, delay_days=3):
    refund_amount = payment["amount"]  # full refund, kept simple on purpose
    created_at = settlement["settled_at"] + timedelta(days=delay_days)
    return {
        "refund_id": rand_id("rfnd", rng),
        "payment_id": payment["payment_id"],
        "order_id": payment["order_id"],
        "amount": refund_amount,
        "status": "processed",
        "bank_reference": rand_utr(rng),
        "created_at": created_at,
        "receipt": None,
    }


def make_bank_row(bank_reference, txn_type, amount, value_date, narration, rng):
    return {
        "bank_txn_id": rand_txn_id(rng),
        "bank_reference": bank_reference,
        "txn_type": txn_type,
        "amount": amount,
        "value_date": value_date,
        "narration": narration,
    }


# ---------------------------------------------------------------------------
# One function per injected scenario.
# Each returns: settlements(list), refunds(list), bank_rows(list), ground_truth(list)
# ---------------------------------------------------------------------------

def scenario_clean_match(payment, rng):
    settlement = build_settlement(payment, rng)
    bank_row = make_bank_row(
        settlement["bank_reference"], "credit", settlement["amount"],
        settlement["settled_at"], f"NEFT SETTLEMENT {settlement['bank_reference']}", rng,
    )
    gt = {
        "entity_id": settlement["settlement_id"], "case_type": "CLEAN_MATCH",
        "true_bank_txn_id": bank_row["bank_txn_id"], "expected_decision": "AUTO_RESOLVE",
        "notes": "Exact reference, exact amount, on-time credit.",
    }
    return [settlement], [], [bank_row], [gt]


def scenario_settlement_variance(payment, rng):
    settlement = build_settlement(payment, rng)
    small = rng.random() < 0.5
    variance = rng.randint(500, 4000) if small else rng.randint(6000, 40000)
    sign = rng.choice([1, -1])
    bank_amount = settlement["amount"] + sign * variance
    bank_row = make_bank_row(
        settlement["bank_reference"], "credit", bank_amount,
        settlement["settled_at"], f"NEFT SETTLEMENT {settlement['bank_reference']}", rng,
    )
    expected = "HUMAN_REVIEW" if small else "EXCEPTION"
    gt = {
        "entity_id": settlement["settlement_id"], "case_type": "SETTLEMENT_VARIANCE",
        "true_bank_txn_id": bank_row["bank_txn_id"], "expected_decision": expected,
        "notes": f"Bank credit differs from ledger by {variance/100:.2f} rupees, unexplained by fee/tax.",
    }
    return [settlement], [], [bank_row], [gt]


def scenario_missing_settlement(payment, rng):
    settlement = build_settlement(payment, rng)
    # Deliberately: no bank row at all.
    gt = {
        "entity_id": settlement["settlement_id"], "case_type": "MISSING_SETTLEMENT",
        "true_bank_txn_id": "", "expected_decision": "EXCEPTION",
        "notes": "No bank credit exists for this settlement in the current statement window.",
    }
    return [settlement], [], [], [gt]


def scenario_delayed_settlement(payment, rng):
    settlement = build_settlement(payment, rng)
    late_days = rng.randint(5, 10)
    value_date = settlement["settled_at"] + timedelta(days=late_days)
    bank_row = make_bank_row(
        settlement["bank_reference"], "credit", settlement["amount"],
        value_date, f"NEFT SETTLEMENT {settlement['bank_reference']}", rng,
    )
    gt = {
        "entity_id": settlement["settlement_id"], "case_type": "DELAYED_SETTLEMENT",
        "true_bank_txn_id": bank_row["bank_txn_id"], "expected_decision": "HUMAN_REVIEW",
        "notes": f"Matching credit found {late_days} days after the {config.SETTLEMENT_WINDOW_DAYS}-day settlement window.",
    }
    return [settlement], [], [bank_row], [gt]


def scenario_refund_exception(payment, rng):
    # The original payment still settles cleanly - only the refund leg has a problem.
    settlement = build_settlement(payment, rng)
    settle_bank_row = make_bank_row(
        settlement["bank_reference"], "credit", settlement["amount"],
        settlement["settled_at"], f"NEFT SETTLEMENT {settlement['bank_reference']}", rng,
    )
    settle_gt = {
        "entity_id": settlement["settlement_id"], "case_type": "CLEAN_MATCH",
        "true_bank_txn_id": settle_bank_row["bank_txn_id"], "expected_decision": "AUTO_RESOLVE",
        "notes": "Original settlement leg is clean; only the refund leg is exceptional.",
    }

    refund = build_refund(payment, settlement, rng)
    bank_rows = []
    if rng.random() < 0.5:
        # Refund debit never shows up in the bank statement at all.
        note = "No bank debit found for this refund in the current statement window."
        expected = "EXCEPTION"
    else:
        # Debit exists (right amount, right timing) but under a different
        # reference - e.g. the acquirer used a different RRN/ARN format.
        wrong_reference = rand_utr(rng)
        bank_rows.append(make_bank_row(
            wrong_reference, "debit", refund["amount"],
            refund["created_at"], f"REFUND DEBIT {wrong_reference}", rng,
        ))
        note = "Bank debit found by amount/date only; its reference does not match the ledger's refund reference."
        expected = "HUMAN_REVIEW"

    refund_gt = {
        "entity_id": refund["refund_id"], "case_type": "REFUND_EXCEPTION",
        "true_bank_txn_id": bank_rows[0]["bank_txn_id"] if bank_rows else "",
        "expected_decision": expected, "notes": note,
    }
    return [settlement], [refund], [settle_bank_row] + bank_rows, [settle_gt, refund_gt]


def scenario_payment_state_ambiguity(payment, rng):
    # Simulates a duplicate settlement.processed webhook: two ledger rows
    # for the same payment, same UTR, same amount - but only one real credit.
    settlement_a = build_settlement(payment, rng)
    settlement_b = dict(settlement_a)
    settlement_b["settlement_id"] = rand_id("setl", rng)

    bank_row = make_bank_row(
        settlement_a["bank_reference"], "credit", settlement_a["amount"],
        settlement_a["settled_at"], f"NEFT SETTLEMENT {settlement_a['bank_reference']}", rng,
    )
    note = "Two ledger settlement entries share one bank reference; only one bank credit exists."
    gts = [
        {
            "entity_id": settlement_a["settlement_id"], "case_type": "PAYMENT_STATE_AMBIGUITY",
            "true_bank_txn_id": bank_row["bank_txn_id"], "expected_decision": "ABSTAIN", "notes": note,
        },
        {
            "entity_id": settlement_b["settlement_id"], "case_type": "PAYMENT_STATE_AMBIGUITY",
            "true_bank_txn_id": bank_row["bank_txn_id"], "expected_decision": "ABSTAIN", "notes": note,
        },
    ]
    return [settlement_a, settlement_b], [], [bank_row], gts


def scenario_duplicate_bank_entry(payment, rng):
    settlement = build_settlement(payment, rng)
    bank_row_1 = make_bank_row(
        settlement["bank_reference"], "credit", settlement["amount"],
        settlement["settled_at"], f"NEFT SETTLEMENT {settlement['bank_reference']}", rng,
    )
    bank_row_2 = make_bank_row(
        settlement["bank_reference"], "credit", settlement["amount"],
        settlement["settled_at"] + timedelta(days=1),
        f"NEFT SETTLEMENT {settlement['bank_reference']} DUP", rng,
    )
    gt = {
        "entity_id": settlement["settlement_id"], "case_type": "DUPLICATE_BANK_ENTRY",
        "true_bank_txn_id": "", "expected_decision": "ABSTAIN",
        "notes": "Two bank credits equally match one settlement entry; cannot tell which (if either) is a duplicate deposit.",
    }
    return [settlement], [], [bank_row_1, bank_row_2], [gt]


SCENARIOS = {
    "CLEAN_MATCH": scenario_clean_match,
    "SETTLEMENT_VARIANCE": scenario_settlement_variance,
    "MISSING_SETTLEMENT": scenario_missing_settlement,
    "DELAYED_SETTLEMENT": scenario_delayed_settlement,
    "REFUND_EXCEPTION": scenario_refund_exception,
    "PAYMENT_STATE_AMBIGUITY": scenario_payment_state_ambiguity,
    "DUPLICATE_BANK_ENTRY": scenario_duplicate_bank_entry,
}


# ---------------------------------------------------------------------------
# Derive internal_ledger.csv from settlements + refunds (this is exactly
# what a Razorpay-style settlement reconciliation report looks like).
# ---------------------------------------------------------------------------

def build_internal_ledger(settlements, refunds, method_by_payment_id):
    rows = []
    for s in settlements:
        rows.append({
            "entity_id": s["settlement_id"], "type": "payment",
            "order_id": s["order_id"], "payment_id": s["payment_id"], "refund_id": "",
            "method": method_by_payment_id.get(s["payment_id"], ""),
            "amount": s["amount"], "debit": 0, "credit": s["amount"],
            "fee": s["fee"], "tax": s["tax"], "bank_reference": s["bank_reference"],
            "created_at": s["created_at"], "settled_at": s["settled_at"],
        })
    for r in refunds:
        rows.append({
            "entity_id": r["refund_id"], "type": "refund",
            "order_id": r["order_id"], "payment_id": r["payment_id"], "refund_id": r["refund_id"],
            "method": method_by_payment_id.get(r["payment_id"], ""),
            "amount": r["amount"], "debit": r["amount"], "credit": 0,
            "fee": 0, "tax": 0, "bank_reference": r["bank_reference"],
            "created_at": r["created_at"], "settled_at": r["created_at"],
        })
    return pd.DataFrame(rows)


def main():
    rng = random.Random(config.RANDOM_SEED)
    os.makedirs(config.DATA_DIR, exist_ok=True)

    case_types = list(config.CASE_MIX.keys())
    weights = list(config.CASE_MIX.values())

    orders, payments, settlements, refunds, bank_rows, ground_truth = [], [], [], [], [], []

    for i in range(config.NUM_ORDERS):
        order = build_order(i, rng)
        payment = build_payment(order, rng)
        case_type = rng.choices(case_types, weights=weights, k=1)[0]

        s_list, r_list, b_list, gt_list = SCENARIOS[case_type](payment, rng)

        orders.append(order)
        payments.append(payment)
        settlements.extend(s_list)
        refunds.extend(r_list)
        bank_rows.extend(b_list)
        ground_truth.extend(gt_list)

    orders_df = pd.DataFrame(orders)
    payments_df = pd.DataFrame(payments)
    settlements_df = pd.DataFrame(settlements)
    refunds_df = pd.DataFrame(refunds)
    bank_df = pd.DataFrame(bank_rows)
    ledger_df = build_internal_ledger(settlements, refunds, {p["payment_id"]: p["method"] for p in payments})
    gt_df = pd.DataFrame(ground_truth)

    orders_df.to_csv(config.ORDERS_CSV, index=False)
    payments_df.to_csv(config.PAYMENTS_CSV, index=False)
    settlements_df.to_csv(config.SETTLEMENTS_CSV, index=False)
    refunds_df.to_csv(config.REFUNDS_CSV, index=False)
    ledger_df.to_csv(config.INTERNAL_LEDGER_CSV, index=False)
    bank_df.to_csv(config.BANK_STATEMENT_CSV, index=False)
    gt_df.to_csv(config.GROUND_TRUTH_CSV, index=False)

    print(f"Generated {config.NUM_ORDERS} orders -> {len(ledger_df)} ledger entities, "
          f"{len(bank_df)} bank statement rows.")
    print("\nInjected case mix (ground truth - reconcile.py never sees this):")
    print(gt_df["case_type"].value_counts().to_string())
    print(f"\nFiles written to: {config.DATA_DIR}/")


if __name__ == "__main__":
    main()
