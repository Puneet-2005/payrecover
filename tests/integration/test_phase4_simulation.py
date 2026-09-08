import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

from alembic import command
from payrecover.infrastructure.database.session import build_engine
from payrecover.infrastructure.database.simulation import (
    SimulationStore,
    initialize,
    run_lock,
    validate_database,
)
from payrecover.simulation.runner import run
from payrecover.simulation.scenario import EPOCH

pytestmark = pytest.mark.integration


@pytest.fixture
def simulation_database(postgres_url):
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    name = "payrecover_sim_" + uuid4().hex
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = build_engine(admin.url.set(database=name).render_as_string(hide_password=False))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    command.upgrade(config, "head")
    initialize(engine)
    try:
        yield engine, config
    finally:
        engine.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def counts(engine, key):
    run_id = uuid5(NAMESPACE_URL, f"payrecover-simulation:{key}")
    merchant = f"sim_{run_id.hex}"
    with engine.connect() as connection:
        values = {
            table: connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE merchant_id=:merchant"),
                {"merchant": merchant},
            )
            for table in ("payment_events", "incident_diagnoses", "recovery_plans")
        }
        values["audits"] = connection.scalar(
            text("""
            SELECT count(*) FROM audit_records a
            WHERE EXISTS(SELECT 1 FROM payment_events p
                         WHERE p.id=a.payment_event_id AND p.merchant_id=:merchant)
               OR EXISTS(SELECT 1 FROM incidents i
                         WHERE i.id=a.incident_id AND i.merchant_id=:merchant)
        """),
            {"merchant": merchant},
        )
        return values


@pytest.mark.parametrize(
    "boundary",
    [
        "after_ingestion_commit",
        "after_planning_commit",
        "after_reservation_commit",
        "after_model_outcome",
        "after_outcome_commit",
    ],
)
def test_crash_resume_reproduces_original_outcomes(simulation_database, boundary):
    engine, _ = simulation_database
    original = run(engine, "reference", size=1000)
    injected = []

    def fail(point):
        if point == boundary and not injected:
            injected.append(point)
            raise RuntimeError("deterministic interruption")

    with pytest.raises(RuntimeError, match="deterministic interruption"):
        run(engine, "interrupted", size=1000, fail=fail)
    assert injected == [boundary]
    resumed = run(engine, "interrupted", size=1000)
    assert resumed == original
    reference_steps = SimulationStore(
        engine, uuid5(NAMESPACE_URL, "payrecover-simulation:reference")
    ).steps()
    interrupted_steps = SimulationStore(
        engine, uuid5(NAMESPACE_URL, "payrecover-simulation:interrupted")
    ).steps()
    assert interrupted_steps == reference_steps
    assert counts(engine, "interrupted") == counts(engine, "reference")
    before = counts(engine, "interrupted")
    assert run(engine, "interrupted", size=1000) == resumed
    assert counts(engine, "interrupted") == before
    with pytest.raises(ValueError, match="Incompatible"):
        run(engine, "interrupted", seed=43, size=1000)


def test_run_lock_survives_other_commits_and_releases(simulation_database):
    engine, _ = simulation_database
    identity = uuid4()
    with run_lock(engine, identity):
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ValueError, match="already active"):
                pool.submit(lambda: _take_lock(engine, identity)).result()
    _take_lock(engine, identity)
    with pytest.raises(RuntimeError):
        with run_lock(engine, identity):
            raise RuntimeError
    _take_lock(engine, identity)


def _take_lock(engine, identity):
    with run_lock(engine, identity):
        pass


def test_metrics_uncertainty_isolation_and_migration(simulation_database):
    engine, config = simulation_database
    command.downgrade(config, "20260907_0005")
    assert "simulation_runs" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    with pytest.raises(ValueError, match="marker"):
        validate_database(engine)
    with pytest.raises(ValueError, match="marker"):
        run(engine, "must-not-initialize", size=1000)
    initialize(engine)
    result = run(engine, "metrics", size=1000)
    assert result["execution_authorized"] is False
    assert result["detection"]["recall_denominator"] == 2
    assert result["detection"]["insufficient"] > 0
    assert result["detection"]["fn"] >= 1
    assert result["detection"]["detected_delay_seconds"] == [1320]
    assert result["simulation"]["attempts"] > 0
    assert (
        result["simulation"]["duplicate_dispatches_suppressed"] == result["simulation"]["attempts"]
    )
    assert result["accounting"]["excluded_conflicting_amount_count"] > 0
    amount = result["accounting"]["amount_paise"]
    assert amount["total"] == sum(
        amount[k] for k in ("already_successful", "simulated_recovered", "unresolved")
    )
    identity = uuid5(NAMESPACE_URL, "payrecover-simulation:metrics")
    store = SimulationStore(engine, identity)
    for payment in store.payments():
        attempts = store.attempts(payment.payment_id)
        assert len(attempts) <= 2
        if attempts and attempts[0].state == "uncertain":
            assert len(attempts) == 1
        if payment.plan_id:
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT execution_authorized FROM recovery_plans WHERE id=:id"),
                        {"id": payment.plan_id},
                    )
                    is False
                )
    before = store.steps()
    uncertain = [
        v for k, v in before.items() if k.startswith("outcome:") and v["observed"] == "uncertain"
    ]
    assert uncertain
    for value in uncertain:
        reconciliation = before[f"reconcile:{value['payment']}:{value['ordinal']}"]
        assert reconciliation["observed"] in ("uncertain", value["truth"])
    payment = store.payments()[0]
    with pytest.raises(ValueError, match="Invalid simulation payment"):
        store.reserve(
            "sim_wrong",
            payment.payment_id,
            1,
            EPOCH,
        )
    tables = (
        "simulation_marker",
        "simulation_runs",
        "simulation_payments",
        "simulation_attempts",
        "simulation_steps",
    )
    with engine.connect() as connection:
        schema = inspect(connection)
        schema_before = {
            table: (
                schema.get_columns(table),
                schema.get_foreign_keys(table),
                schema.get_check_constraints(table),
                schema.get_indexes(table),
            )
            for table in tables
        }
        rows_before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
            for table in tables
        }
    with pytest.raises(DatabaseError, match="simulation history"):
        command.downgrade(config, "20260907_0005")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260907_0006"
        assert {
            "simulation_runs",
            "simulation_payments",
            "simulation_attempts",
            "simulation_steps",
        } <= set(inspect(connection).get_table_names())
        schema = inspect(connection)
        for table in tables:
            after_schema = (
                schema.get_columns(table),
                schema.get_foreign_keys(table),
                schema.get_check_constraints(table),
                schema.get_indexes(table),
            )
            # SQLAlchemy type instances compare by identity; compare their stable descriptions.
            assert repr(after_schema) == repr(schema_before[table])
            assert (
                connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
                == rows_before[table]
            )
    assert store.steps() == before
    assert json.loads(json.dumps(run(engine, "metrics", size=1000))) == result


def test_concurrent_reservation_creates_one_attempt_and_history(simulation_database):
    engine, _ = simulation_database
    identity = uuid5(NAMESPACE_URL, "payrecover-simulation:reservation-race")
    merchant = f"sim_{identity.hex}"
    selected = []

    def interrupt(boundary):
        if boundary == "after_planning_commit":
            with engine.connect() as connection:
                row = connection.execute(
                    text("""
                    SELECT p.payment_id,p.id,p.created_at FROM recovery_plans p
                    JOIN simulation_payments s ON s.event_id=p.payment_event_id AND s.run_id=:run
                    WHERE p.merchant_id=:merchant AND p.reason_code='transient_network'
                    AND s.fixture->'visible'->>'consent'='true'
                    AND s.fixture->'visible'->>'flow'='true'
                    AND s.fixture->'visible'->>'status'='failed'
                    ORDER BY p.payment_id LIMIT 1
                """),
                    {"run": identity, "merchant": merchant},
                ).first()
            if row:
                selected.append(row)
                raise RuntimeError("stop before simulation")

    with pytest.raises(RuntimeError, match="stop before simulation"):
        run(engine, "reservation-race", size=1000, fail=interrupt)
    store = SimulationStore(engine, identity)
    row = selected[0]
    store.attach_plan(merchant, row.payment_id, row.id)
    at = row.created_at + timedelta(minutes=10)
    # Choose a naturally eligible fixture; never change fixtures or bypass the gate.
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(lambda _: store.reserve(merchant, row.payment_id, 1, at), range(2))
        )
    assert outcomes == [True, True]
    assert len(store.attempts(row.payment_id)) == 1
    steps = store.steps()
    assert f"eligibility:{row.payment_id}:1" in steps
    assert f"reserved:{row.payment_id}:1" in steps
