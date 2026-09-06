from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from payrecover.cli import main, parse_as_of
from payrecover.domain.analytics import (
    AmountBand,
    CohortDimension,
    DegradationDetection,
    DegradationSeverity,
    DetectionOutcome,
    PaymentCohortV2,
    calculate_analytics_window,
)
from payrecover.domain.incidents import Incident, IncidentOpenedAudit, ScanSummary
from payrecover.domain.models import FieldAvailability
from payrecover.services.incident_reads import Cursor, InvalidCursor, decode_cursor
from payrecover.services.incidents import absent_evidence, advance_incident

NOW = datetime(2026, 9, 5, 12, 20, tzinfo=UTC)
COHORT = PaymentCohortV2(
    "merchant",
    "card",
    CohortDimension(None, FieldAvailability.MISSING),
    CohortDimension(None, FieldAvailability.REDACTED),
    AmountBand.BAND_2,
)


def scan_summary(offset=0):
    return ScanSummary(
        uuid4(),
        "merchant",
        "degradation-v2",
        calculate_analytics_window(NOW + timedelta(minutes=15 * offset)),
        0,
        0,
        0,
        0,
        NOW + timedelta(minutes=15 * offset),
    )


def incident():
    scan = scan_summary()
    return Incident(
        1,
        COHORT,
        "degradation-v2",
        scan.id,
        NOW,
        scan.window.observation_start,
        DegradationSeverity.MEDIUM,
        DegradationSeverity.MEDIUM,
        DetectionOutcome.DEGRADED,
        scan.window.observation_start,
        0,
        "open",
        None,
    )


def detection(outcome, severity=None):
    return DegradationDetection("degradation-v2", outcome, severity, None, None, None, None)


@pytest.mark.parametrize(
    "outcome", [DetectionOutcome.WATCH, DetectionOutcome.INSUFFICIENT, DetectionOutcome.DEGRADED]
)
def test_nonhealthy_observation_resets_streak(outcome):
    healthy = advance_incident(incident(), detection(DetectionOutcome.HEALTHY), scan_summary(1))
    value = advance_incident(healthy, detection(outcome), scan_summary(2))
    assert value.healthy_streak == 0
    assert value.status == "open"


def test_adjacent_health_resolves_but_gap_restarts_streak():
    first = advance_incident(incident(), detection(DetectionOutcome.HEALTHY), scan_summary(1))
    adjacent = advance_incident(first, detection(DetectionOutcome.HEALTHY), scan_summary(2))
    assert adjacent.status == "resolved"
    assert adjacent.healthy_streak == 2
    with pytest.raises(ValueError, match="Resolved"):
        advance_incident(adjacent, detection(DetectionOutcome.DEGRADED), scan_summary(3))
    gap = advance_incident(first, detection(DetectionOutcome.HEALTHY), scan_summary(3))
    assert gap.status == "open"
    assert gap.healthy_streak == 1


def test_peak_severity_increases_and_opening_is_immutable():
    critical = advance_incident(
        incident(),
        detection(DetectionOutcome.DEGRADED, DegradationSeverity.CRITICAL),
        scan_summary(1),
    )
    medium = advance_incident(
        critical, detection(DetectionOutcome.DEGRADED, DegradationSeverity.MEDIUM), scan_summary(2)
    )
    assert medium.peak_severity == DegradationSeverity.CRITICAL
    assert medium.opening_severity == DegradationSeverity.MEDIUM
    assert medium.opened_at == NOW


def test_absent_cohort_has_zero_unavailable_evidence():
    value = absent_evidence(COHORT)
    assert value.baseline_total == value.observation_total == 0
    assert value.baseline_success_rate is None
    assert value.observation_success_rate is None
    assert value.observation_error_code_distribution == ()


def test_cli_future_time_reads_clock_once_without_opening_uow(capsys):
    calls = []

    def clock():
        calls.append(1)
        return NOW

    def factory():
        pytest.fail("future scan must not connect")

    assert (
        main(
            ["--merchant-id", "merchant", "--as-of", "2026-09-06T00:00:00Z"],
            clock=clock,
            factory=factory,
        )
        == 2
    )
    assert calls == [1]
    assert "future" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value", ["2026-09-05", "2026-09-05T12:20:00", "yesterday", "2026-99-99T12:00:00Z"]
)
def test_cli_rejects_invalid_or_naive_rfc3339(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        parse_as_of(value)


def test_cli_normalizes_rfc3339_offset():
    assert parse_as_of("2026-09-05T17:50:00+05:30") == NOW


def test_cli_rejects_overflowing_offset_minutes():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        parse_as_of("2026-09-05T17:50:00+05:99")


@pytest.mark.parametrize("value", ["!", "e30", "a" * 1025, "====", "eyJhZnRlciI6TmFOfQ"])
def test_cursor_strictly_rejects_malformed_input(value):
    with pytest.raises(InvalidCursor):
        decode_cursor(value, "merchant", 0)


def test_cursor_scope_and_boundaries():
    encoded = Cursor(merchant="merchant", subject=1, after=2, upper=9).encode()
    assert decode_cursor(encoded, "merchant", 1) == (2, 9)
    with pytest.raises(InvalidCursor):
        decode_cursor(encoded, "other", 1)
    with pytest.raises(InvalidCursor):
        decode_cursor(encoded, "merchant", 2)
    with pytest.raises(InvalidCursor):
        decode_cursor(
            Cursor(merchant="merchant", subject=1, after=10, upper=9).encode(), "merchant", 1
        )


def test_incident_audit_schema_rejects_arbitrary_evidence():
    details = dict(
        scan_id=uuid4(),
        observation_id=1,
        cohort_sha256=COHORT.cohort_hash,
        detector_version="degradation-v2",
        observation_start=NOW,
        observation_end=NOW + timedelta(minutes=15),
        opening_severity="medium",
    )
    assert IncidentOpenedAudit.model_validate(details)
    with pytest.raises(ValidationError):
        IncidentOpenedAudit.model_validate(
            {**details, "customer_email": "synthetic@example.invalid"}
        )
    with pytest.raises(ValidationError):
        IncidentOpenedAudit.model_validate({**details, "cohort_sha256": "A" * 64})


def test_incident_chronology_rejects_nonadvancing_window():
    with pytest.raises(ValueError, match="chronologically"):
        advance_incident(replace(incident()), detection(DetectionOutcome.HEALTHY), scan_summary())
