"""
evaluate.py
-----------
Grades reconcile.py's decisions against the hidden ground truth.

This is the ONLY file in the project allowed to open ground_truth.csv.
reconcile.py never sees it - this script exists purely to answer the
question "how good is the matcher, honestly?" after the fact.

Run:
    python evaluate.py
"""

import json

import pandas as pd

import config


def load_outputs():
    ground_truth = pd.read_csv(config.GROUND_TRUTH_CSV)
    matches = pd.read_csv(config.MATCHES_CSV)
    exceptions = pd.read_csv(config.EXCEPTIONS_CSV)

    matches_tagged = matches.assign(decision="AUTO_RESOLVE")
    combined = pd.concat([
        matches_tagged[["entity_id", "decision", "expected_amount"]],
        exceptions[["entity_id", "decision", "expected_amount"]],
    ], ignore_index=True)

    return ground_truth.merge(combined, on="entity_id", how="left"), matches, exceptions


def method_breakdown(matches, exceptions):
    """Resolution rate by payment method - not needed for correctness, but
    directly answers "which EMI transactions need attention" style
    questions without touching ground truth."""
    matches_tagged = matches.assign(decision="AUTO_RESOLVE")
    combined = pd.concat([matches_tagged, exceptions], ignore_index=True)
    if "method" not in combined.columns or combined.empty:
        return {}
    combined["method"] = combined["method"].fillna("")
    rows = {}
    for method, group in combined.groupby("method"):
        if not method:
            continue
        total = len(group)
        auto = int((group["decision"] == "AUTO_RESOLVE").sum())
        rows[method] = {
            "total": total,
            "auto_resolved": auto,
            "resolution_rate": round(auto / total, 4) if total else None,
            "needs_attention": total - auto,
        }
    return rows


def compute_metrics(df):
    total = len(df)
    is_truly_clean = df["case_type"] == "CLEAN_MATCH"
    auto_resolved = df["decision"] == "AUTO_RESOLVE"

    auto_resolve_count = int(auto_resolved.sum())
    correct_auto = int((auto_resolved & is_truly_clean).sum())
    wrong_auto = int((auto_resolved & ~is_truly_clean).sum())

    auto_match_precision = correct_auto / auto_resolve_count if auto_resolve_count else None
    false_reconciliation_rate = wrong_auto / auto_resolve_count if auto_resolve_count else None

    true_exceptions = ~is_truly_clean
    caught = true_exceptions & ~auto_resolved
    exception_detection_recall = caught.sum() / true_exceptions.sum() if true_exceptions.sum() else None

    resolution_rate = auto_resolve_count / total if total else None

    unresolved_mask = df["decision"].isin(["EXCEPTION", "ABSTAIN"])
    human_review_mask = df["decision"] == "HUMAN_REVIEW"

    metrics = {
        "total_ledger_entities": total,
        "auto_match_precision": _round(auto_match_precision),
        "false_reconciliation_rate": _round(false_reconciliation_rate),
        "exception_detection_recall": _round(exception_detection_recall),
        "resolution_rate": _round(resolution_rate),
        "unresolved_count": int(unresolved_mask.sum()),
        "unresolved_rupee_value": round(df.loc[unresolved_mask, "expected_amount"].sum(), 2),
        "human_review_count": int(human_review_mask.sum()),
        "human_review_value": round(df.loc[human_review_mask, "expected_amount"].sum(), 2),
    }
    return metrics


def _round(x, ndigits=4):
    return None if x is None else round(x, ndigits)


def print_report(metrics, df):
    print("=" * 60)
    print("AI FINANCE CONTROLLER - RECONCILIATION SCORECARD")
    print("=" * 60)
    print(f"Total ledger entities reconciled : {metrics['total_ledger_entities']}")
    print()
    print("-- Correctness (graded against hidden ground truth) --")
    print(f"  auto_match_precision       : {fmt_pct(metrics['auto_match_precision'])}  "
          "(of everything auto-resolved, how much was genuinely clean)")
    print(f"  false_reconciliation_rate  : {fmt_pct(metrics['false_reconciliation_rate'])}  "
          "(of everything auto-resolved, how much should NOT have been)")
    print(f"  exception_detection_recall : {fmt_pct(metrics['exception_detection_recall'])}  "
          "(of every real injected problem, how much did we NOT rubber-stamp)")
    print()
    print("-- Coverage --")
    print(f"  resolution_rate            : {fmt_pct(metrics['resolution_rate'])}  "
          "(share auto-resolved without a human)")
    print()
    print("-- Exposure sitting on the books right now --")
    print(f"  unresolved_count           : {metrics['unresolved_count']}")
    print(f"  unresolved_rupee_value     : Rs {metrics['unresolved_rupee_value']:,.2f}")
    print(f"  human_review_count         : {metrics['human_review_count']}")
    print(f"  human_review_value         : Rs {metrics['human_review_value']:,.2f}")
    print("=" * 60)


def fmt_pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def print_method_breakdown(breakdown):
    if not breakdown:
        return
    print("\n-- Resolution rate by payment method --")
    for method, stats in sorted(breakdown.items()):
        print(f"  {method:<12} {stats['auto_resolved']}/{stats['total']} auto-resolved "
              f"({fmt_pct(stats['resolution_rate'])}), {stats['needs_attention']} need attention")


def main():
    df, matches, exceptions = load_outputs()
    metrics = compute_metrics(df)
    breakdown = method_breakdown(matches, exceptions)
    metrics["by_payment_method"] = breakdown

    print_report(metrics, df)
    print_method_breakdown(breakdown)

    with open(config.METRICS_JSON, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"\nSaved machine-readable metrics to {config.METRICS_JSON}")


if __name__ == "__main__":
    main()
