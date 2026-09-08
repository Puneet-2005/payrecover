"""Metrics are rebuilt from committed records; no runtime counters or hidden uplift."""

from collections import Counter
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from payrecover.infrastructure.database.models import PaymentEventRow
from payrecover.infrastructure.database.simulation import SimulationStore


def report(store: SimulationStore, merchant: str) -> dict[str, Any]:
    steps = store.steps()
    deliveries = [v for k, v in steps.items() if k.startswith("delivery:")]
    evaluations = [v for k, v in steps.items() if k.startswith("evaluation:")]
    counts: Counter[str] = Counter()
    delays = []
    for e in evaluations:
        positive, detected = e["label"], e["outcome"] == "degraded"
        counts[
            "tp" if positive and detected else "fn" if positive else "fp" if detected else "tn"
        ] += 1
        counts["insufficient"] += e["outcome"] == "insufficient"
        if positive and detected:
            delays.append(e["delay_seconds"])
    categories: Counter[str] = Counter()
    amounts: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    attempts = uncertain = recovered_payments = excluded = 0
    with Session(store.engine) as session:
        for payment in store.payments():
            history = session.execute(
                select(
                    func.count(distinct(PaymentEventRow.amount_paise)),
                    func.bool_or(PaymentEventRow.status == "success"),
                ).where(
                    PaymentEventRow.merchant_id == merchant,
                    PaymentEventRow.source == payment.source,
                    PaymentEventRow.payment_id == payment.payment_id,
                )
            ).one()
            records = store.attempts(payment.payment_id)
            attempts += len(records)
            uncertain += sum(a.state in ("reserved", "uncertain") for a in records)
            if history[0] != 1:
                excluded += 1
                continue
            succeeded = any(a.state == "succeeded" for a in records)
            category = (
                "already_successful"
                if history[1]
                else "simulated_recovered"
                if succeeded
                else "unresolved"
            )
            categories[category] += 1
            amounts[category] += payment.amount_paise
            amounts["total"] += payment.amount_paise
            recovered_payments += category == "simulated_recovered"
    initial = [v for k, v in steps.items() if k.startswith("eligibility:") and v["ordinal"] == 1]
    for e in initial:
        blocks[e["reason"]] += 1
    assert amounts["total"] == sum(
        amounts[k] for k in ("already_successful", "simulated_recovered", "unresolved")
    )
    return {
        "status": "complete",
        "simulation_only": True,
        "execution_authorized": False,
        "traffic": {
            "deliveries": len(deliveries),
            "unique_events": sum(not d["duplicate"] for d in deliveries),
            "injected_duplicates": sum(d["duplicate"] for d in deliveries),
        },
        "detection": {
            **{k: counts[k] for k in ("tp", "fn", "fp", "tn", "insufficient")},
            "recall_numerator": counts["tp"],
            "recall_denominator": counts["tp"] + counts["fn"],
            "false_positive_denominator": counts["fp"] + counts["tn"],
            "detected_delay_seconds": delays,
        },
        "simulation": {
            "initial_decisions": dict(sorted(blocks.items())),
            "attempts": attempts,
            "uncertain_attempts": uncertain,
            "duplicate_dispatches_suppressed": sum(k.startswith("suppressed:") for k in steps),
            "recovered_payments": recovered_payments,
        },
        "accounting": {
            "population_count": len(store.payments()),
            "categories": {
                k: categories[k]
                for k in ("already_successful", "simulated_recovered", "unresolved")
            },
            "amount_paise": {
                k: amounts[k]
                for k in ("total", "already_successful", "simulated_recovered", "unresolved")
            },
            "excluded_conflicting_amount_count": excluded,
            "excluded_value": None,
            "exclusion_reason": "No authoritative amount for conflicting history",
        },
        "limitations": [
            "Synthetic model outcomes, not real money recovered",
            "No causal uplift comparison",
            "Insufficient labelled degradation counts as missed",
            "Stored success at horizon takes precedence, including after planning",
        ],
    }
