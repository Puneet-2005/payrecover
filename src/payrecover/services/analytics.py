from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from payrecover.domain.analytics import (
    MINIMUM_BASELINE_SAMPLE,
    MINIMUM_OBSERVATION_SAMPLE,
    RATE_QUANTUM,
    CohortAggregate,
    CohortAnalysis,
    DegradationDetection,
    DegradationSeverity,
    DetectionOutcome,
    PaymentAnalyticsResult,
    calculate_analytics_window,
)
from payrecover.domain.repositories import PaymentAnalyticsRepository

DETECTOR_VERSION = "degradation-v2"
DEGRADATION_DROP_THRESHOLD = Decimal("0.10")
DEGRADATION_Z_THRESHOLD = Decimal("3.0")
HEALTHY_DROP_THRESHOLD = Decimal("0.05")
HIGH_DROP_THRESHOLD = Decimal("0.20")
CRITICAL_DROP_THRESHOLD = Decimal("0.35")
SCORE_QUANTUM = Decimal("0.000000000001")


def detect_degradation_v2(aggregate: CohortAggregate) -> DegradationDetection:
    """Classify an aggregate with a deterministic, stabilized two-sample z-score.

    Given baseline successes ``Bs`` of ``Bn`` and observation successes ``Os`` of
    ``On``, the smoothed pooled proportion is::

        p = (Bs + Os + 1) / (Bn + On + 2)

    The score uses ``sqrt(p * (1-p) * (1/Bn + 1/On))`` as its standard error.
    Its implementation calculates the algebraically equivalent smoothed-success
    and smoothed-failure product directly, avoiding cancellation in ``1-p``.
    Adding one pseudo-success and one pseudo-failure keeps variance positive for
    all-success and all-failure samples. Decisions use a Decimal precision of at
    least 50 that expands with the inputs; returned values are rounded half-even
    to twelve decimal places.
    """
    if (
        aggregate.baseline_total < MINIMUM_BASELINE_SAMPLE
        or aggregate.observation_total < MINIMUM_OBSERVATION_SAMPLE
    ):
        return DegradationDetection(
            detector_version=DETECTOR_VERSION,
            outcome=DetectionOutcome.INSUFFICIENT,
            severity=None,
            baseline_success_rate=aggregate.baseline_success_rate,
            observation_success_rate=aggregate.observation_success_rate,
            absolute_success_rate_drop=aggregate.absolute_success_rate_drop,
            z_score=None,
        )

    input_digits = max(
        len(str(aggregate.baseline_total)),
        len(str(aggregate.observation_total)),
    )
    with localcontext() as context:
        context.prec = max(50, input_digits * 2 + 32)
        context.rounding = ROUND_HALF_EVEN
        baseline_rate = Decimal(aggregate.baseline_success_count) / Decimal(
            aggregate.baseline_total
        )
        observation_rate = Decimal(aggregate.observation_success_count) / Decimal(
            aggregate.observation_total
        )
        absolute_drop = baseline_rate - observation_rate
        smoothed_successes = (
            aggregate.baseline_success_count
            + aggregate.observation_success_count
            + 1
        )
        smoothed_failures = (
            aggregate.baseline_total
            - aggregate.baseline_success_count
            + aggregate.observation_total
            - aggregate.observation_success_count
            + 1
        )
        denominator = aggregate.baseline_total + aggregate.observation_total + 2
        smoothed_variance = (
            Decimal(smoothed_successes)
            * Decimal(smoothed_failures)
            / Decimal(denominator * denominator)
            * (
                Decimal(1) / Decimal(aggregate.baseline_total)
                + Decimal(1) / Decimal(aggregate.observation_total)
            )
        )
        standard_error = context.sqrt(smoothed_variance)
        raw_z_score = absolute_drop / standard_error

        degraded = (
            absolute_drop >= DEGRADATION_DROP_THRESHOLD
            and raw_z_score >= DEGRADATION_Z_THRESHOLD
        )
        if degraded:
            outcome = DetectionOutcome.DEGRADED
            severity = (
                DegradationSeverity.CRITICAL
                if absolute_drop >= CRITICAL_DROP_THRESHOLD
                else DegradationSeverity.HIGH
                if absolute_drop >= HIGH_DROP_THRESHOLD
                else DegradationSeverity.MEDIUM
            )
        elif absolute_drop <= HEALTHY_DROP_THRESHOLD:
            outcome = DetectionOutcome.HEALTHY
            severity = None
        else:
            outcome = DetectionOutcome.WATCH
            severity = None

        return DegradationDetection(
            detector_version=DETECTOR_VERSION,
            outcome=outcome,
            severity=severity,
            baseline_success_rate=baseline_rate.quantize(
                RATE_QUANTUM, rounding=ROUND_HALF_EVEN
            ),
            observation_success_rate=observation_rate.quantize(
                RATE_QUANTUM, rounding=ROUND_HALF_EVEN
            ),
            absolute_success_rate_drop=absolute_drop.quantize(
                RATE_QUANTUM, rounding=ROUND_HALF_EVEN
            ),
            z_score=raw_z_score.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_EVEN),
        )


def analyze_payment_cohorts(
    merchant_id: str,
    as_of: datetime,
    repository: PaymentAnalyticsRepository,
) -> PaymentAnalyticsResult:
    """Aggregate and classify one merchant without persisting or authorizing actions."""
    if not merchant_id.strip():
        raise ValueError("merchant_id must be nonblank")
    window = calculate_analytics_window(as_of)
    aggregates = repository.aggregate_for_merchant(merchant_id, window)
    return PaymentAnalyticsResult(
        merchant_id=merchant_id,
        window=window,
        cohorts=tuple(
            CohortAnalysis(aggregate=value, detection=detect_degradation_v2(value))
            for value in aggregates
        ),
    )
