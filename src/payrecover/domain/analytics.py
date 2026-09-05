from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from hashlib import sha256
from typing import Final

from payrecover.domain.models import FieldAvailability

COHORT_VERSION = 2
RATE_QUANTUM = Decimal("0.000000000001")
OBSERVATION_DURATION = timedelta(minutes=15)
BASELINE_DURATION = timedelta(days=7)
COMPLETION_DELAY = timedelta(minutes=5)
MINIMUM_OBSERVATION_SAMPLE = 30
MINIMUM_BASELINE_SAMPLE = 100
AMOUNT_BAND_UPPER_BOUNDS_PAISE: Final[tuple[int, ...]] = (
    50_000,
    100_000,
    200_000,
    500_000,
    1_000_000,
)


class AmountBand(StrEnum):
    BAND_0 = "band_0"
    BAND_1 = "band_1"
    BAND_2 = "band_2"
    BAND_3 = "band_3"
    BAND_4 = "band_4"
    BAND_5 = "band_5"


class DetectionOutcome(StrEnum):
    INSUFFICIENT = "insufficient"
    HEALTHY = "healthy"
    WATCH = "watch"
    DEGRADED = "degraded"


class DegradationSeverity(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def amount_band_for_paise(amount_paise: int) -> AmountBand:
    """Return the cohort-v2 amount band for a positive integer-paise amount."""
    if amount_paise <= 0:
        raise ValueError("amount_paise must be positive")
    for band_index, upper_bound in enumerate(AMOUNT_BAND_UPPER_BOUNDS_PAISE):
        if amount_paise < upper_bound:
            return AmountBand(f"band_{band_index}")
    return AmountBand.BAND_5


@dataclass(frozen=True, slots=True)
class CohortDimension:
    value: str | None
    availability: FieldAvailability

    def __post_init__(self) -> None:
        if not isinstance(self.availability, FieldAvailability):
            raise TypeError("availability must be a FieldAvailability")
        if self.availability == FieldAvailability.PROVIDED:
            if self.value is None or not self.value.strip():
                raise ValueError("provided dimensions require a nonblank value")
        elif self.value is not None:
            raise ValueError("non-provided dimensions must not contain a value")

    def canonical_value(self) -> dict[str, str | None]:
        return {"availability": self.availability.value, "value": self.value}


@dataclass(frozen=True, slots=True)
class PaymentCohortV2:
    merchant_id: str
    method: str
    issuer: CohortDimension
    provider: CohortDimension
    amount_band: AmountBand
    cohort_version: int = COHORT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.amount_band, AmountBand):
            raise TypeError("amount_band must be an AmountBand")
        if self.cohort_version != COHORT_VERSION:
            raise ValueError(f"cohort_version must be {COHORT_VERSION}")
        if not self.merchant_id.strip():
            raise ValueError("merchant_id must be nonblank")
        if not self.method.strip():
            raise ValueError("method must be nonblank")

    @property
    def canonical_json(self) -> str:
        value: dict[str, object] = {
            "amount_band": self.amount_band.value,
            "cohort_version": self.cohort_version,
            "issuer": self.issuer.canonical_value(),
            "merchant_id": self.merchant_id,
            "method": self.method,
            "provider": self.provider.canonical_value(),
        }
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @property
    def cohort_hash(self) -> str:
        return sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AnalyticsWindow:
    baseline_start: datetime
    baseline_end: datetime
    observation_start: datetime
    observation_end: datetime

    def __post_init__(self) -> None:
        values = (
            self.baseline_start,
            self.baseline_end,
            self.observation_start,
            self.observation_end,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("analytics window timestamps must be timezone-aware")
        if any(value.utcoffset() != timedelta(0) for value in values):
            raise ValueError("analytics window timestamps must be UTC")
        if not (
            self.baseline_start < self.baseline_end
            and self.baseline_end == self.observation_start
            and self.observation_start < self.observation_end
        ):
            raise ValueError("analytics windows must be ordered and non-overlapping")


def calculate_analytics_window(as_of: datetime) -> AnalyticsWindow:
    """Calculate deterministic completed windows after normalizing ``as_of`` to UTC.

    The function never reads the system clock. Its intervals are half-open:
    ``[baseline_start, baseline_end)`` and
    ``[observation_start, observation_end)``.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")

    effective = as_of.astimezone(UTC) - COMPLETION_DELAY
    midnight = effective.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = (effective - midnight).seconds
    duration_seconds = int(OBSERVATION_DURATION.total_seconds())
    completed_seconds = (seconds_since_midnight // duration_seconds) * duration_seconds
    observation_end = midnight + timedelta(seconds=completed_seconds)
    observation_start = observation_end - OBSERVATION_DURATION
    baseline_end = observation_start
    baseline_start = baseline_end - BASELINE_DURATION
    return AnalyticsWindow(
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        observation_start=observation_start,
        observation_end=observation_end,
    )


def quantized_rate(success_count: int, total_count: int) -> Decimal | None:
    if success_count < 0 or total_count < 0 or success_count > total_count:
        raise ValueError("success and total counts are inconsistent")
    if total_count == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return (Decimal(success_count) / Decimal(total_count)).quantize(RATE_QUANTUM)


def quantized_drop(
    baseline_success_count: int,
    baseline_total: int,
    observation_success_count: int,
    observation_total: int,
) -> Decimal | None:
    if baseline_total == 0 or observation_total == 0:
        return None
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        baseline = Decimal(baseline_success_count) / Decimal(baseline_total)
        observation = Decimal(observation_success_count) / Decimal(observation_total)
        return (baseline - observation).quantize(RATE_QUANTUM)


@dataclass(frozen=True, slots=True)
class ErrorCodeCount:
    error_code: str
    failed_count: int

    def __post_init__(self) -> None:
        if not self.error_code.strip():
            raise ValueError("error_code must be nonblank")
        if len(self.error_code) > 100:
            raise ValueError("error_code must contain at most 100 characters")
        if self.failed_count <= 0:
            raise ValueError("failed_count must be positive")


@dataclass(frozen=True, slots=True)
class CohortAggregate:
    cohort: PaymentCohortV2
    baseline_total: int
    baseline_success_count: int
    baseline_failed_count: int
    baseline_success_rate: Decimal | None
    baseline_failed_amount_paise: int
    observation_total: int
    observation_success_count: int
    observation_failed_count: int
    observation_success_rate: Decimal | None
    observation_failed_amount_paise: int
    absolute_success_rate_drop: Decimal | None
    observation_error_code_distribution: tuple[ErrorCodeCount, ...]
    dominant_observation_error_code: str | None

    @property
    def cohort_hash(self) -> str:
        return self.cohort.cohort_hash

    def __post_init__(self) -> None:
        counts = (
            self.baseline_total,
            self.baseline_success_count,
            self.baseline_failed_count,
            self.observation_total,
            self.observation_success_count,
            self.observation_failed_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("aggregate counts must be nonnegative")
        if self.baseline_success_count + self.baseline_failed_count != self.baseline_total:
            raise ValueError("baseline counts are inconsistent")
        if (
            self.observation_success_count + self.observation_failed_count
            != self.observation_total
        ):
            raise ValueError("observation counts are inconsistent")
        if self.baseline_failed_amount_paise < 0 or self.observation_failed_amount_paise < 0:
            raise ValueError("failed amounts must be nonnegative")
        expected_baseline_rate = quantized_rate(
            self.baseline_success_count, self.baseline_total
        )
        expected_observation_rate = quantized_rate(
            self.observation_success_count, self.observation_total
        )
        expected_drop = quantized_drop(
            self.baseline_success_count,
            self.baseline_total,
            self.observation_success_count,
            self.observation_total,
        )
        if self.baseline_success_rate != expected_baseline_rate:
            raise ValueError("baseline success rate does not match its counts")
        if self.observation_success_rate != expected_observation_rate:
            raise ValueError("observation success rate does not match its counts")
        if self.absolute_success_rate_drop != expected_drop:
            raise ValueError("absolute success-rate drop does not match its counts")
        if len(self.observation_error_code_distribution) > 20:
            raise ValueError("error-code distribution must contain at most 20 entries")
        if len(
            {item.error_code for item in self.observation_error_code_distribution}
        ) != len(self.observation_error_code_distribution):
            raise ValueError("error-code distribution contains duplicate codes")
        if (
            sum(item.failed_count for item in self.observation_error_code_distribution)
            > self.observation_failed_count
        ):
            raise ValueError("error-code counts exceed the observation failure count")
        expected_order = tuple(
            sorted(
                self.observation_error_code_distribution,
                key=lambda item: (-item.failed_count, item.error_code.encode("utf-8")),
            )
        )
        if self.observation_error_code_distribution != expected_order:
            raise ValueError("error-code distribution is not deterministically ordered")
        expected_dominant = expected_order[0].error_code if expected_order else None
        if self.dominant_observation_error_code != expected_dominant:
            raise ValueError("dominant error code must be the first distribution entry")


@dataclass(frozen=True, slots=True)
class DegradationDetection:
    detector_version: str
    outcome: DetectionOutcome
    severity: DegradationSeverity | None
    baseline_success_rate: Decimal | None
    observation_success_rate: Decimal | None
    absolute_success_rate_drop: Decimal | None
    z_score: Decimal | None


@dataclass(frozen=True, slots=True)
class CohortAnalysis:
    aggregate: CohortAggregate
    detection: DegradationDetection


@dataclass(frozen=True, slots=True)
class PaymentAnalyticsResult:
    merchant_id: str
    window: AnalyticsWindow
    cohorts: tuple[CohortAnalysis, ...]
