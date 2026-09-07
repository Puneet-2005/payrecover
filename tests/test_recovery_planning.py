from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from payrecover.api.main import create_app
from payrecover.domain.analytics import (
    AmountBand,
    CohortAggregate,
    CohortDimension,
    DegradationDetection,
    DetectionOutcome,
    ErrorCodeCount,
    PaymentCohortV2,
    calculate_analytics_window,
    quantized_drop,
    quantized_rate,
)
from payrecover.domain.incidents import IncidentObservation
from payrecover.domain.models import FieldAvailability, RecoveryAction
from payrecover.domain.planning import PlanEvidence, Prerequisites, digest
from payrecover.services.diagnosis import diagnose
from payrecover.services.policy import decide_recovery, decide_recovery_plan

NOW = datetime(2026, 9, 7, 12, 20, tzinfo=UTC)


def facts(signal="unknown", **changes):
    return PlanEvidence(
        individual_signal=signal,
        **{
            "stored_success": False,
            "mixed_statuses": False,
            "inconsistent_dimensions_or_amount": False,
            "conflicting_reviewed_signals": False,
            **changes,
        },
    )


def observation(errors, failed=100, outcome=DetectionOutcome.DEGRADED):
    dim = CohortDimension(None, FieldAvailability.MISSING)
    cohort = PaymentCohortV2("synthetic", "card", dim, dim, AmountBand.BAND_0)
    errors = sorted(errors, key=lambda item: (-item[1], item[0]))
    evidence = CohortAggregate(
        cohort,
        1000,
        1000,
        0,
        quantized_rate(1000, 1000),
        0,
        failed,
        0,
        failed,
        quantized_rate(0, failed),
        failed * 100,
        quantized_drop(1000, 1000, 0, failed),
        tuple(ErrorCodeCount(code, count) for code, count in errors),
        errors[0][0] if errors else None,
    )
    return IncidentObservation(
        1,
        1,
        uuid4(),
        calculate_analytics_window(NOW),
        evidence,
        DegradationDetection("degradation-v2", outcome, None, None, None, None, None),
        NOW,
    )


@pytest.mark.parametrize(
    "signal,cause",
    [
        ("network_timeout", "probable_transient_network_failure"),
        ("issuer_unavailable", "probable_issuer_unavailability"),
        ("insufficient_funds", "probable_customer_funding_issue"),
        ("incorrect_pin", "probable_authentication_failure"),
    ],
)
def test_majority_is_over_all_failures(signal, cause):
    assert diagnose(observation([(signal, 51)]), NOW).outcome == cause
    result = diagnose(observation([(signal, 50)]), NOW)
    assert result.outcome == "unknown"
    assert result.evidence.unrepresented_failed_count == 50
    assert result.supporting_failed_count == 0
    assert "confidence" not in result.model_dump_json()


def test_truncated_top20_and_unmapped_codes_do_not_inflate_majority():
    errors = [("network_timeout", 40)] + [(f"UNMAPPED_{i}", 1) for i in range(19)]
    result = diagnose(observation(errors), NOW)
    assert result.outcome == "unknown"
    assert result.evidence.retained_failed_count == 59
    assert result.evidence.unrepresented_failed_count == 41
    assert diagnose(observation([("BAD_REQUEST_ERROR", 100)]), NOW).outcome == "unknown"


def test_insufficient_and_absent_evidence():
    assert diagnose(observation([], 0), NOW).outcome == "insufficient_evidence"
    assert (
        diagnose(
            observation([("network_timeout", 100)], outcome=DetectionOutcome.INSUFFICIENT), NOW
        ).outcome
        == "insufficient_evidence"
    )
    with pytest.raises(ValueError):
        diagnose(observation([("network_timeout", 101)]), NOW)


@pytest.mark.parametrize(
    "signal,action,delay,attempts",
    [
        ("network_timeout", RecoveryAction.RETRY_NOW, 30, 2),
        ("issuer_unavailable", RecoveryAction.WAIT_AND_RETRY, 480, 2),
        ("incorrect_pin", RecoveryAction.NO_ACTION, None, 0),
        ("insufficient_funds", RecoveryAction.NOTIFY_CUSTOMER, None, 0),
        ("unknown", RecoveryAction.ESCALATE, None, 0),
    ],
)
def test_terminal_and_backoff_rules(signal, action, delay, attempts):
    result = decide_recovery_plan(facts(signal))
    assert (result.action, result.retry_after_seconds, result.max_attempts) == (
        action,
        delay,
        attempts,
    )
    assert result.execution_authorized is False
    assert result.prerequisites.consent == "missing"
    assert result.prerequisites.provider_flow == "unverified"
    assert result.prerequisites.payment_status == "unreconciled"
    assert result.prerequisites.attempt_history == "unverified"


@pytest.mark.parametrize(
    "flag", ["mixed_statuses", "inconsistent_dimensions_or_amount", "conflicting_reviewed_signals"]
)
def test_integrity_blocks_transient_retry(flag):
    result = decide_recovery_plan(facts("network_timeout", **{flag: True}))
    assert result.action == RecoveryAction.ESCALATE
    assert result.prerequisites.integrity_blocker


def test_success_overrides_all_retry_signals():
    result = decide_recovery_plan(
        facts("network_timeout", stored_success=True, mixed_statuses=True)
    )
    assert result.action == RecoveryAction.NO_ACTION
    assert result.reason_code == "stored_success"
    assert result.prerequisites.integrity_blocker


@pytest.mark.parametrize("signal", ["incorrect_pin", "insufficient_funds"])
def test_legacy_zero_attempt_behavior_and_http_contract_remain(signal):
    decision = decide_recovery(signal, 0)
    assert decision.action == RecoveryAction.ESCALATE
    assert decision.reason == "retry stopping rule reached"
    response = TestClient(create_app()).get(
        f"/v1/recovery/decision/{signal}", params={"attempt": 0}
    )
    assert response.status_code == 200
    assert response.json() == decision.model_dump(mode="json")


def test_frozen_strict_evidence_and_utc():
    result = diagnose(observation([("network_timeout", 60)]), NOW)
    with pytest.raises(ValidationError):
        result.evidence.supporting_signal = "unknown"
    with pytest.raises(ValidationError):
        Prerequisites(consent="present", integrity_blocker=False)
    with pytest.raises(ValidationError):
        PlanEvidence(**{**facts().model_dump(), "customer_email": "never-store"})
    assert digest(result.evidence) == (
        "95f403690a74b1bd6d16bd90146b602ac26497553196b3aa25c458abf537bf31"
    )
    with pytest.raises(ValidationError):
        diagnose(observation([]), NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "args",
    [
        ["show", "--merchant-id", "synthetic", "--plan-id", "pay_PrivateMalformed"],
        ["diagnose", "--merchant-id", "synthetic", "--incident-id", "private-value"],
        ["plan", "--merchant-id", "synthetic", "--incident-id", "-1"],
    ],
)
def test_cli_parse_errors_do_not_echo_private_values(args, capsys):
    from payrecover.planning_cli import main

    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert capsys.readouterr().err == "Invalid planning command arguments; use --help\n"


def test_cli_database_errors_are_generic(capsys):
    from payrecover.planning_cli import main

    def unavailable():
        raise RuntimeError("postgresql://private-secret")

    assert (
        main(["show", "--merchant-id", "synthetic", "--plan-id", str(uuid4())], factory=unavailable)
        == 1
    )
    assert capsys.readouterr().err == "Planning command could not be completed\n"
