import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import event as sql_event
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session
from test_phase3a2_incidents import MERCHANT, NOW, evidence, factory, run_scan

from alembic import command
from payrecover.domain.analytics import ErrorCodeCount
from payrecover.domain.planning import PlanningConflict, PlanningNotFound, PlanningUnavailable
from payrecover.infrastructure.database.models import PaymentEventRow
from payrecover.infrastructure.database.planning_models import DiagnosisRow, PlanRow
from payrecover.planning_cli import main
from payrecover.services.planning import diagnose_incident, list_candidates, plan_recovery

pytestmark = pytest.mark.integration


def add_payment(engine, *, payment_id="pay_PrivateSynthetic", **overrides):
    values = dict(
        id=uuid4(),
        schema_version=2,
        source="normalized_api",
        source_event_id=str(uuid4()),
        payload_sha256="a" * 64,
        merchant_id=MERCHANT,
        payment_id=payment_id,
        method="card",
        issuer=None,
        issuer_availability="missing",
        provider=None,
        provider_availability="redacted",
        amount_paise=1_000_000,
        status="failed",
        error_code="network_timeout",
        error_code_availability="provided",
        error_source=None,
        error_step=None,
        error_reason=None,
        latency_ms=None,
        latency_availability="missing",
        cohort_key="not-authoritative",
        occurred_at=NOW - timedelta(minutes=10),
    )
    values.update(overrides)
    with Session(engine) as session:
        session.add(PaymentEventRow(**values))
        session.commit()
    return values["id"]


def seed(engine):
    values = replace(
        evidence(),
        observation_error_code_distribution=(ErrorCodeCount("network_timeout", 500),),
        dominant_observation_error_code="network_timeout",
    )
    run_scan(engine, values=[values])
    return add_payment(engine)


def plan(engine, event, merchant=MERCHANT, incident=1, observation=1):
    with factory(engine)() as uow:
        return plan_recovery(uow, merchant, incident, observation, event, now=NOW)


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table: connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings().all()
            for table in ("incident_diagnoses", "recovery_plans", "audit_records")
        }


def test_opening_diagnosis_standalone_replay_and_plan(clean_database):
    event = seed(clean_database)
    with factory(clean_database)() as uow:
        first = diagnose_incident(uow, MERCHANT, 1, 1, now=NOW)
    assert first.diagnosis.outcome == "probable_transient_network_failure"
    before = snapshot(clean_database)
    with factory(clean_database)() as uow:
        replay = diagnose_incident(uow, MERCHANT, 1, 1, now=NOW + timedelta(days=1))
    assert replay.replayed and replay.diagnosis == first.diagnosis
    assert snapshot(clean_database) == before
    result = plan(clean_database, event)
    assert result.recommendation.diagnosis_id == first.diagnosis.id
    assert result.recommendation.execution_authorized is False
    assert result.recommendation.association_evaluated_at == NOW
    assert result.recommendation.retry_after_seconds == 30
    assert len(snapshot(clean_database)["audit_records"]) == 3


def test_replay_is_historical_even_after_success(clean_database):
    event = seed(clean_database)
    first = plan(clean_database, event)
    add_payment(
        clean_database, status="success", error_code=None, error_code_availability="not_applicable"
    )
    before = snapshot(clean_database)
    replay = plan(clean_database, event)
    assert replay.replayed and replay.recommendation == first.recommendation
    assert replay.recommendation.evidence.stored_success is False
    assert snapshot(clean_database) == before


@pytest.mark.parametrize(
    "change,reason,flag",
    [
        (
            {"status": "success", "error_code": None, "error_code_availability": "not_applicable"},
            "stored_success",
            "mixed_statuses",
        ),
        ({"amount_paise": 2_000_000}, "contradictory_history", "inconsistent_dimensions_or_amount"),
        ({"method": "upi"}, "contradictory_history", "inconsistent_dimensions_or_amount"),
        ({"error_code": "incorrect_pin"}, "contradictory_history", "conflicting_reviewed_signals"),
    ],
)
def test_contradictory_history_blocks_retry(clean_database, change, reason, flag):
    event = seed(clean_database)
    # Timestamp order cannot make an existing success disappear.
    add_payment(clean_database, occurred_at=NOW - timedelta(days=2), **change)
    value = plan(clean_database, event).recommendation
    assert value.reason_code == reason
    assert value.action in {"no_action", "escalate"}
    assert getattr(value.evidence, flag) is True
    assert value.prerequisites.integrity_blocker


@pytest.mark.parametrize(
    "signal,source,action",
    [
        ("incorrect_pin", "normalized_api", "no_action"),
        ("insufficient_funds", "normalized_api", "notify_customer"),
        ("network_timeout", "razorpay_webhook", "escalate"),
        ("BAD_REQUEST_ERROR", "razorpay_webhook", "escalate"),
    ],
)
def test_individual_signal_and_provider_namespace(clean_database, signal, source, action):
    seed(clean_database)
    event = add_payment(clean_database, payment_id="pay_Separate", error_code=signal, source=source)
    result = plan(clean_database, event).recommendation
    assert result.action == action
    assert not result.execution_authorized


def test_duplicate_event_or_evidence_association_conflicts(clean_database):
    original = seed(clean_database)
    plan(clean_database, original)
    other = add_payment(clean_database)
    before = snapshot(clean_database)
    with pytest.raises(PlanningConflict):
        plan(clean_database, other)
    assert snapshot(clean_database) == before


def test_concurrent_plans_and_diagnoses_commit_once(clean_database):
    event = seed(clean_database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: plan(clean_database, event), range(2)))
    assert sorted(r.replayed for r in results) == [False, True]
    assert results[0].recommendation == results[1].recommendation
    rows = snapshot(clean_database)
    assert len(rows["incident_diagnoses"]) == len(rows["recovery_plans"]) == 1
    assert len(rows["audit_records"]) == 3


def test_payment_policy_unique_conflict_rolls_back_then_reruns(clean_database):
    first = seed(clean_database)
    run_scan(clean_database, 1, [evidence()])
    second = add_payment(clean_database, occurred_at=NOW + timedelta(minutes=5))
    before = snapshot(clean_database)
    ready = Barrier(2)

    def race(subject):
        event_id, observation_id = subject
        with factory(clean_database)() as uow:
            # Test-only: distinct evidence lets both transactions insert a diagnosis
            # and its audit before competing on the real payment/policy constraint.
            uow.incidents.lock_merchant = lambda merchant: None
            insert = uow.planning.add_plan
            proposed = []

            def synchronized_insert(value, event):
                proposed.append(value)
                ready.wait(timeout=15)
                insert(value, event)

            uow.planning.add_plan = synchronized_insert
            try:
                result = plan_recovery(
                    uow, MERCHANT, 1, observation_id, event_id, now=NOW + timedelta(minutes=15)
                )
                return subject, result, proposed[0]
            except PlanningUnavailable as exc:
                assert isinstance(exc.__cause__, IntegrityError)
                assert exc.__cause__.orig.diag.constraint_name == "uq_plan_payment_policy"
                return subject, exc, proposed[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [(first, 1), (second, 2)]))
    winners = [r for r in results if not isinstance(r[1], PlanningUnavailable)]
    losers = [r for r in results if isinstance(r[1], PlanningUnavailable)]
    assert len(winners) == len(losers) == 1
    winner, result, _ = winners[0]
    loser, _, rejected = losers[0]
    assert not result.replayed
    after = snapshot(clean_database)
    assert len(after["incident_diagnoses"]) == len(after["recovery_plans"]) == 1
    assert after["recovery_plans"][0]["id"] == result.recommendation.id
    assert after["incident_diagnoses"][0]["id"] == result.recommendation.diagnosis_id
    assert len(after["audit_records"]) == len(before["audit_records"]) + 2
    assert all(
        row["diagnosis_id"] != rejected.diagnosis_id and row["recovery_plan_id"] != rejected.id
        for row in after["audit_records"]
    )
    replay = plan(clean_database, winner[0], observation=winner[1])
    assert replay.replayed and replay.recommendation == result.recommendation
    with pytest.raises(PlanningConflict):
        plan(clean_database, loser[0], observation=loser[1])
    assert snapshot(clean_database) == after


def test_history_rejects_foreign_or_nonexistent_event(clean_database):
    event_id = seed(clean_database)
    with factory(clean_database)() as uow:
        event = uow.planning.candidate(MERCHANT, 1, 1, event_id)
        with pytest.raises(PlanningNotFound):
            uow.planning.history("other", event)
        with pytest.raises(PlanningNotFound):
            uow.planning.history(MERCHANT, replace(event, id=uuid4()))
        assert uow.planning.history(MERCHANT, event).individual_signal == "network_timeout"


@pytest.mark.parametrize("forgery", [{"source": "razorpay_webhook"}, {"payment_id": "forged"}])
def test_history_rejects_forged_identity(clean_database, forgery):
    event_id = seed(clean_database)
    with factory(clean_database)() as uow:
        event = uow.planning.candidate(MERCHANT, 1, 1, event_id)
        with pytest.raises(PlanningConflict):
            uow.planning.history(MERCHANT, replace(event, **forgery))


def test_history_uses_persisted_signal_and_preserves_blockers(clean_database):
    seed(clean_database)
    event_id = add_payment(clean_database, error_code="incorrect_pin", amount_paise=2_000_000)
    with factory(clean_database)() as uow:
        event = uow.planning.candidate(MERCHANT, 1, 1, event_id)
        forged = replace(event, signal="network_timeout")
        history = uow.planning.history(MERCHANT, forged)
        assert history.individual_signal == "incorrect_pin"
        assert history.conflicting_reviewed_signals
        assert history.inconsistent_dimensions_or_amount
        assert not history.stored_success and not history.mixed_statuses
    add_payment(
        clean_database, status="success", error_code=None, error_code_availability="not_applicable"
    )
    with factory(clean_database)() as uow:
        history = uow.planning.history(MERCHANT, forged)
        assert history == uow.planning.history(MERCHANT, event)
        assert history.individual_signal == "incorrect_pin"
        assert history.stored_success and history.mixed_statuses
        assert history.conflicting_reviewed_signals and history.inconsistent_dimensions_or_amount


@pytest.mark.parametrize("operation", ["diagnose", "plan"])
def test_audit_failure_rolls_back_all_planning_records(clean_database, operation):
    event = seed(clean_database)
    before = snapshot(clean_database)
    with factory(clean_database)() as uow:
        append = uow.audit_records.append

        def fail(record):
            if record.event_type == (
                "incident.diagnosed" if operation == "diagnose" else "recovery_plan.created"
            ):
                raise RuntimeError("synthetic private failure")
            return append(record)

        uow.audit_records.append = fail
        with pytest.raises(PlanningUnavailable):
            if operation == "diagnose":
                diagnose_incident(uow, MERCHANT, 1, 1, now=NOW)
            else:
                plan_recovery(uow, MERCHANT, 1, 1, event, now=NOW)
    assert snapshot(clean_database) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"merchant_id": "other"},
        {"method": "upi"},
        {"issuer_availability": "redacted"},
        {"provider_availability": "missing"},
        {"amount_paise": 999_999},
        {"occurred_at": NOW - timedelta(minutes=5)},
        {"occurred_at": NOW - timedelta(minutes=20, microseconds=1)},
        {"status": "success", "error_code": None, "error_code_availability": "not_applicable"},
    ],
)
def test_invalid_association_rejected_by_direct_repository(clean_database, changes):
    seed(clean_database)
    event = add_payment(clean_database, **changes)
    with factory(clean_database)() as uow:
        with pytest.raises(PlanningNotFound):
            uow.planning.candidate(MERCHANT, 1, 1, event)
    assert not snapshot(clean_database)["recovery_plans"]


def test_candidates_boundaries_pagination_scope_and_safe_cli(clean_database, capsys):
    seed(clean_database)
    lower = add_payment(
        clean_database, payment_id="pay_Boundary", occurred_at=NOW - timedelta(minutes=20)
    )
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    sql_event.listen(clean_database, "before_cursor_execute", capture)
    try:
        with factory(clean_database)() as uow:
            first = list_candidates(uow, MERCHANT, 1, 1, 1, None, now=NOW)
            assert len(first.items) == 1 and first.next_cursor
            second = list_candidates(uow, MERCHANT, 1, 1, 1, first.next_cursor, now=NOW)
            assert len(second.items) == 1 and second.next_cursor is None
            assert len({first.items[0].payment_event_id, second.items[0].payment_event_id}) == 2
            assert lower in {first.items[0].payment_event_id, second.items[0].payment_event_id}
            for cursor in ("!", "e30", first.next_cursor):
                with pytest.raises(ValueError):
                    list_candidates(uow, "other", 1, 1, 1, cursor, now=NOW)
            with pytest.raises(PlanningNotFound):
                uow.planning.candidates("other", 1, 1, None, 1)
    finally:
        sql_event.remove(clean_database, "before_cursor_execute", capture)
    pages = [s for s in statements if s.startswith("SELECT payment_events.")]
    assert pages and all("LIMIT" in s for s in pages)
    args = ["candidates", "--merchant-id", MERCHANT, "--incident-id", "1", "--observation-id", "1"]
    assert main(args, clock=lambda: NOW, factory=factory(clean_database)) == 0
    output = capsys.readouterr().out
    assert "pay_PrivateSynthetic" not in output and "source_event_id" not in output
    assert len(json.loads(output)["items"]) == 2


@pytest.mark.parametrize("row_type", [DiagnosisRow, PlanRow])
@pytest.mark.parametrize("delete", [False, True])
def test_planning_history_orm_immutable(clean_database, row_type, delete):
    plan(clean_database, seed(clean_database))
    before = snapshot(clean_database)
    with Session(clean_database) as session:
        row = session.scalar(select(row_type))
        if delete:
            session.delete(row)
        else:
            row.reason_code = "changed"
        with pytest.raises(ValueError, match="append-only"):
            session.flush()
        session.rollback()
    assert snapshot(clean_database) == before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE recovery_plans SET execution_authorized=true",
        "UPDATE recovery_plans SET merchant_id='other'",
        "UPDATE incident_diagnoses SET incident_id=999",
        "UPDATE recovery_plans SET max_attempts=4",
        "UPDATE recovery_plans SET retry_after_seconds=-1",
        "UPDATE recovery_plans SET action='no_action'",
        "INSERT INTO recovery_plans SELECT gen_random_uuid(), merchant_id, incident_id, "
        "observation_id, diagnosis_id, payment_event_id, source, payment_id, policy_version, "
        "action, decision, reason_code, retry_after_seconds, max_attempts, execution_authorized, "
        "prerequisites, "
        "evidence, evidence_sha256, association_evaluated_at, created_at FROM recovery_plans",
    ],
)
def test_database_constraints(clean_database, statement):
    plan(clean_database, seed(clean_database))
    with clean_database.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(statement))
        connection.rollback()


def test_migration_cycle_and_refusal_preserve_history(clean_database, migrated_database_url):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    event = seed(clean_database)
    before = snapshot(clean_database)["audit_records"]
    command.downgrade(config, "20260905_0004")
    assert "recovery_plans" not in inspect(clean_database).get_table_names()
    command.upgrade(config, "head")
    assert snapshot(clean_database)["audit_records"] == before
    plan(clean_database, event)
    before = snapshot(clean_database)
    with pytest.raises(DatabaseError, match="Cannot downgrade while planning history exists"):
        command.downgrade(config, "20260905_0004")
    with clean_database.connect() as connection:
        schema = inspect(connection)
        assert {"incident_diagnoses", "recovery_plans"} <= set(schema.get_table_names())
        assert {"diagnosis_id", "recovery_plan_id"} <= {
            c["name"] for c in schema.get_columns("audit_records")
        }
        assert {"fk_audit_diagnosis_subject", "fk_audit_plan_subject"} <= {
            c["name"] for c in schema.get_foreign_keys("audit_records")
        }
        assert "ck_audit_records_incident_subject" in {
            c["name"] for c in schema.get_check_constraints("audit_records")
        }
        assert {"ix_audit_diagnosis_history", "ix_audit_plan_history"} <= {
            c["name"] for c in schema.get_indexes("audit_records")
        }
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260907_0006"
    assert snapshot(clean_database) == before


def test_concurrent_standalone_diagnosis_and_commit_failure(clean_database):
    event = seed(clean_database)

    def run():
        with factory(clean_database)() as uow:
            return diagnose_incident(uow, MERCHANT, 1, 1, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(value.replayed for value in results) == [False, True]
    assert results[0].diagnosis == results[1].diagnosis
    before = snapshot(clean_database)
    with factory(clean_database)() as uow:

        def fail_commit(session):
            raise RuntimeError("synthetic commit failure")

        sql_event.listen(uow.session, "before_commit", fail_commit)
        with pytest.raises(PlanningUnavailable):
            plan_recovery(uow, MERCHANT, 1, 1, event, now=NOW)
    assert snapshot(clean_database) == before


def test_other_merchant_and_source_history_do_not_change_individual_decision(clean_database):
    event = seed(clean_database)
    add_payment(
        clean_database,
        merchant_id="other",
        status="success",
        error_code=None,
        error_code_availability="not_applicable",
    )
    add_payment(
        clean_database,
        source="razorpay_webhook",
        status="success",
        error_code=None,
        error_code_availability="not_applicable",
    )
    result = plan(clean_database, event).recommendation
    assert result.action == "retry_now"
    assert not result.evidence.stored_success
    with factory(clean_database)() as uow:
        with pytest.raises(PlanningNotFound):
            uow.planning.get_plan("other", result.id)
        assert uow.planning.diagnosis("other", 1) is None


def test_schema_types_foreign_keys_and_metadata_match(clean_database):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from payrecover.infrastructure.database.base import Base

    with clean_database.connect() as connection:
        schema = inspect(connection)
        for table, column, expected in (
            ("incident_diagnoses", "incident_id", "BIGINT"),
            ("incident_diagnoses", "observation_id", "BIGINT"),
            ("recovery_plans", "payment_event_id", "UUID"),
            ("audit_records", "diagnosis_id", "UUID"),
            ("audit_records", "recovery_plan_id", "UUID"),
        ):
            columns = {c["name"]: str(c["type"]) for c in schema.get_columns(table)}
            assert columns[column] == expected
        version_column = schema.get_columns("alembic_version")[0]
        assert len("20260907_0005") <= version_column["type"].length
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_plan_cli_and_show_do_not_expose_provider_ids(clean_database, capsys):
    event = seed(clean_database)
    calls = []

    def clock():
        calls.append(1)
        return NOW

    args = [
        "plan",
        "--merchant-id",
        MERCHANT,
        "--incident-id",
        "1",
        "--observation-id",
        "1",
        "--payment-event-id",
        str(event),
    ]
    assert main(args, clock=clock, factory=factory(clean_database)) == 0
    output = capsys.readouterr().out
    assert "pay_PrivateSynthetic" not in output and "source_event_id" not in output
    value = json.loads(output)["recommendation"]
    assert value["execution_authorized"] is False and value["retry_after_seconds"] == 30
    assert calls == [1]
    assert (
        main(
            ["show", "--merchant-id", MERCHANT, "--plan-id", value["id"]],
            clock=clock,
            factory=factory(clean_database),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == value


def test_later_evidence_association_cannot_rebind_payment_policy(clean_database):
    event = seed(clean_database)
    plan(clean_database, event)
    run_scan(clean_database, 1, [evidence()])
    later = add_payment(clean_database, occurred_at=NOW + timedelta(minutes=5))
    before = snapshot(clean_database)
    with pytest.raises(PlanningConflict):
        plan(clean_database, later, observation=2)
    assert snapshot(clean_database) == before
