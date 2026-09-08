from datetime import timedelta
from uuid import uuid4

import pytest

from payrecover.domain.planning import PlanEvidence, RecoveryRecommendation
from payrecover.infrastructure.database.simulation import configured_url
from payrecover.services.policy import decide_recovery_plan
from payrecover.simulation.eligibility import eligibility
from payrecover.simulation.scenario import EPOCH, fixtures, model, traffic


def test_isolation_configuration(monkeypatch):
    monkeypatch.delenv("PAYRECOVER_SIMULATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/payrecover")
    with pytest.raises(ValueError):
        configured_url()
    for url in (
        "sqlite://",
        "postgresql+psycopg://localhost:5433/payrecover",
        "postgresql+psycopg://localhost:5432/payrecover_sim_demo",
    ):
        monkeypatch.setenv("PAYRECOVER_SIMULATION_DATABASE_URL", url)
        with pytest.raises(ValueError):
            configured_url()


def test_generator_and_hidden_model_are_reproducible():
    first = traffic(42, 1000, "sim_test")
    assert first == traffic(42, 1000, "sim_test")
    assert first != traffic(43, 1000, "sim_test")
    assert sum(not d.duplicate and d.key.startswith("event:") for d in first) == 1000
    assert any(d.duplicate for d in first)
    truth = fixtures(42, "p500")["hidden"]
    value = model(truth, EPOCH + timedelta(minutes=38))
    assert value == model(truth, EPOCH + timedelta(minutes=38))


def recommendation(signal="network_timeout", **changes):
    facts = PlanEvidence(
        individual_signal=signal,
        stored_success=False,
        mixed_statuses=False,
        inconsistent_dimensions_or_amount=False,
        conflicting_reviewed_signals=False,
    )
    facts = facts.model_copy(update=changes)
    value = RecoveryRecommendation(
        **decide_recovery_plan(facts).model_dump(),
        id=uuid4(),
        merchant_id="sim_test",
        incident_id=1,
        observation_id=1,
        diagnosis_id=uuid4(),
        payment_event_id=uuid4(),
        evidence=facts,
        evidence_sha256="a" * 64,
        association_evaluated_at=EPOCH,
        created_at=EPOCH,
    )
    return value, facts


@pytest.mark.parametrize(
    "visible,reason",
    [
        ({}, "missing_consent"),
        ({"consent": True}, "unsupported_flow"),
        ({"consent": True, "flow": True}, "unresolved_status"),
        ({"consent": True, "flow": True, "status": "unknown"}, "unresolved_status"),
    ],
)
def test_missing_prerequisites_fail_closed(visible, reason):
    plan, facts = recommendation()
    assert eligibility(plan, facts, visible, (), EPOCH + timedelta(minutes=10), None) == reason


@pytest.mark.parametrize("signal", ["incorrect_pin", "insufficient_funds", "unknown"])
def test_historical_transient_plan_cannot_override_current_terminal_signal(signal):
    plan, _ = recommendation()
    _, current = recommendation(signal)
    assert (
        eligibility(
            plan,
            current,
            {"consent": True, "flow": True, "status": "failed"},
            (),
            EPOCH + timedelta(minutes=10),
            None,
        )
        == "terminal_or_unknown"
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"stored_success": True}, "stored_success"),
        ({"mixed_statuses": True}, "integrity"),
        ({"inconsistent_dimensions_or_amount": True}, "integrity"),
        ({"conflicting_reviewed_signals": True}, "integrity"),
    ],
)
def test_current_history_blocks_historical_retry(changes, reason):
    plan, _ = recommendation()
    _, facts = recommendation(**changes)
    assert (
        eligibility(
            plan,
            facts,
            {"consent": True, "flow": True, "status": "failed"},
            (),
            EPOCH + timedelta(minutes=10),
            None,
        )
        == reason
    )


@pytest.mark.parametrize(
    "states,reason",
    [
        (("reserved",), "outstanding_attempt"),
        (("uncertain",), "outstanding_attempt"),
        (("succeeded",), "simulated_success"),
        (("failed", "failed"), "attempt_limit"),
    ],
)
def test_attempt_bounds_and_uncertainty(states, reason):
    plan, facts = recommendation()
    assert (
        eligibility(
            plan,
            facts,
            {"consent": True, "flow": True, "status": "failed"},
            states,
            EPOCH + timedelta(minutes=10),
            EPOCH,
        )
        == reason
    )


def test_exact_backoff_and_no_authorization():
    plan, facts = recommendation()
    visible = {"consent": True, "flow": True, "status": "failed"}
    assert eligibility(plan, facts, visible, (), EPOCH + timedelta(seconds=29), None) == "backoff"
    assert eligibility(plan, facts, visible, (), EPOCH + timedelta(seconds=30), None) is None
    assert (
        eligibility(plan, facts, visible, ("failed",), EPOCH + timedelta(seconds=59), EPOCH)
        == "backoff"
    )
    assert (
        eligibility(plan, facts, visible, ("failed",), EPOCH + timedelta(seconds=60), EPOCH) is None
    )
    assert plan.execution_authorized is False
