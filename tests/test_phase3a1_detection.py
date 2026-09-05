from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest

from payrecover.domain.analytics import (
    AmountBand,
    CohortAggregate,
    CohortDimension,
    DegradationSeverity,
    DetectionOutcome,
    PaymentCohortV2,
    quantized_drop,
    quantized_rate,
)
from payrecover.domain.models import FieldAvailability
from payrecover.services.analytics import (
    DETECTOR_VERSION,
    analyze_payment_cohorts,
    detect_degradation_v2,
)


def aggregate(
    baseline_success: int,
    baseline_total: int,
    observation_success: int,
    observation_total: int,
) -> CohortAggregate:
    return CohortAggregate(
        cohort=PaymentCohortV2(
            merchant_id="acc_TestMerchant",
            method="upi",
            issuer=CohortDimension(None, FieldAvailability.MISSING),
            provider=CohortDimension(None, FieldAvailability.REDACTED),
            amount_band=AmountBand.BAND_2,
        ),
        baseline_total=baseline_total,
        baseline_success_count=baseline_success,
        baseline_failed_count=baseline_total - baseline_success,
        baseline_success_rate=quantized_rate(baseline_success, baseline_total),
        baseline_failed_amount_paise=(baseline_total - baseline_success) * 100_000,
        observation_total=observation_total,
        observation_success_count=observation_success,
        observation_failed_count=observation_total - observation_success,
        observation_success_rate=quantized_rate(observation_success, observation_total),
        observation_failed_amount_paise=(observation_total - observation_success) * 100_000,
        absolute_success_rate_drop=quantized_drop(
            baseline_success,
            baseline_total,
            observation_success,
            observation_total,
        ),
        observation_error_code_distribution=(),
        dominant_observation_error_code=None,
    )


@pytest.mark.parametrize(
    "value",
    [
        aggregate(99, 99, 0, 30),
        aggregate(100, 100, 0, 29),
        aggregate(0, 0, 0, 30),
        aggregate(100, 100, 0, 0),
    ],
)
def test_minimum_sample_guards_return_explicit_insufficient(value: CohortAggregate):
    result = detect_degradation_v2(value)
    assert result.detector_version == DETECTOR_VERSION
    assert result.outcome == DetectionOutcome.INSUFFICIENT
    assert result.severity is None
    assert result.z_score is None


def test_ten_point_drop_below_statistical_threshold_is_watch():
    result = detect_degradation_v2(aggregate(60, 100, 15, 30))
    assert result.absolute_success_rate_drop == Decimal("0.100000000000")
    assert result.z_score is not None and result.z_score < Decimal("3")
    assert result.outcome == DetectionOutcome.WATCH
    assert result.severity is None


def test_score_above_threshold_but_drop_below_ten_points_is_watch():
    result = detect_degradation_v2(aggregate(9_000, 10_000, 8_200, 10_000))
    assert result.z_score is not None and result.z_score > Decimal("3")
    assert result.absolute_success_rate_drop == Decimal("0.080000000000")
    assert result.outcome == DetectionOutcome.WATCH
    assert result.severity is None


@pytest.mark.parametrize(
    ("observation_success", "severity"),
    [
        (750, DegradationSeverity.MEDIUM),
        (700, DegradationSeverity.HIGH),
        (550, DegradationSeverity.CRITICAL),
    ],
)
def test_degradation_severity_boundaries(
    observation_success: int, severity: DegradationSeverity
):
    result = detect_degradation_v2(aggregate(900, 1_000, observation_success, 1_000))
    assert result.outcome == DetectionOutcome.DEGRADED
    assert result.severity == severity


def test_healthy_watch_and_negative_drop_classification():
    healthy = detect_degradation_v2(aggregate(900, 1_000, 860, 1_000))
    watch = detect_degradation_v2(aggregate(900, 1_000, 820, 1_000))
    improved = detect_degradation_v2(aggregate(800, 1_000, 900, 1_000))
    assert (healthy.outcome, healthy.severity) == (DetectionOutcome.HEALTHY, None)
    assert (watch.outcome, watch.severity) == (DetectionOutcome.WATCH, None)
    assert improved.outcome == DetectionOutcome.HEALTHY
    assert improved.absolute_success_rate_drop == Decimal("-0.100000000000")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (aggregate(100, 100, 30, 30), DetectionOutcome.HEALTHY),
        (aggregate(0, 100, 0, 30), DetectionOutcome.HEALTHY),
        (aggregate(100, 100, 0, 30), DetectionOutcome.DEGRADED),
        (aggregate(0, 100, 30, 30), DetectionOutcome.HEALTHY),
    ],
)
def test_perfect_and_zero_success_windows_are_finite(
    value: CohortAggregate, expected: DetectionOutcome
):
    result = detect_degradation_v2(value)
    assert result.outcome == expected
    assert result.z_score is not None
    assert result.z_score.is_finite()
    assert result.absolute_success_rate_drop is not None
    assert result.absolute_success_rate_drop.is_finite()


def test_very_large_counts_remain_decimal_and_finite():
    result = detect_degradation_v2(
        aggregate(
            9_000_000_000_000_000,
            10_000_000_000_000_000,
            8_000_000_000_000_000,
            10_000_000_000_000_000,
        )
    )
    assert result.outcome == DetectionOutcome.DEGRADED
    assert isinstance(result.z_score, Decimal)
    assert result.z_score is not None and result.z_score.is_finite()
    assert result.absolute_success_rate_drop == Decimal("0.100000000000")


def test_extreme_counts_do_not_collapse_smoothed_variance():
    baseline_total = 10**100
    result = detect_degradation_v2(
        aggregate(
            baseline_total,
            baseline_total,
            0,
            30,
        )
    )
    assert result.z_score is not None
    assert result.z_score.is_finite()
    assert not result.z_score.is_nan()
    assert not result.z_score.is_infinite()
    assert result.z_score > 0
    assert result.outcome == DetectionOutcome.DEGRADED
    assert result.severity == DegradationSeverity.CRITICAL


def test_z_score_matches_independent_high_precision_reference():
    result = detect_degradation_v2(aggregate(917, 1_003, 811, 997))

    # Reference value calculated separately at 100-digit precision from the exact
    # integer-ratio formula in the approved detector-v2 specification.
    expected = Decimal(
        "6.569036451348214920657726164706415138899677674587483566060502598639"
    )
    tolerance = Decimal("0.0000000000005")
    assert result.z_score is not None
    assert abs(result.z_score - expected) <= tolerance


def test_decimal_results_are_repeatable():
    value = aggregate(917, 1_003, 811, 997)
    assert detect_degradation_v2(value) == detect_degradation_v2(value)


def test_rate_quantization_uses_twelve_places_and_round_half_even():
    assert quantized_rate(1, 6) == Decimal("0.166666666667")
    assert quantized_rate(1, 2_000_000_000_000) == Decimal("0.000000000000")
    assert quantized_rate(3, 2_000_000_000_000) == Decimal("0.000000000002")


def test_rate_quantization_does_not_depend_on_the_process_decimal_context():
    with localcontext() as context:
        context.prec = 6
        assert quantized_rate(1, 6) == Decimal("0.166666666667")


def test_analytics_service_uses_the_caller_supplied_time():
    value = aggregate(90, 100, 21, 30)

    class RecordingRepository:
        def __init__(self) -> None:
            self.window = None

        def aggregate_for_merchant(self, merchant_id, window):
            assert merchant_id == "acc_TestMerchant"
            self.window = window
            return (value,)

    repository = RecordingRepository()
    result = analyze_payment_cohorts(
        "acc_TestMerchant",
        datetime(2026, 9, 4, 12, 23, tzinfo=UTC),
        repository,
    )
    assert repository.window == result.window
    assert result.window.observation_end == datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    assert result.cohorts[0].aggregate == value
