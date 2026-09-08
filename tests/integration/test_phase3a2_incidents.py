from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session, load_only

from alembic import command
from payrecover.api.dependencies import get_unit_of_work_factory
from payrecover.api.main import create_app
from payrecover.cli import main
from payrecover.domain.analytics import (
    AmountBand,
    CohortAggregate,
    CohortDimension,
    DegradationSeverity,
    ErrorCodeCount,
    PaymentCohortV2,
    quantized_drop,
    quantized_rate,
)
from payrecover.domain.models import FieldAvailability
from payrecover.infrastructure.database.incident_models import (
    IncidentErrorRow,
    IncidentRow,
    ObservationRow,
    ScanRunRow,
)
from payrecover.infrastructure.database.incidents import SqlAlchemyIncidentRepository
from payrecover.infrastructure.database.models import AuditRecordRow, PaymentEventRow
from payrecover.infrastructure.database.session import build_session_factory
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork
from payrecover.services.incident_reads import incident_detail
from payrecover.services.incidents import ScanUnavailable, StaleScan, scan_merchant

pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 5, 12, 20, tzinfo=UTC)
MERCHANT = "acc_IncidentSynthetic"


def evidence(success=500, total=1000, merchant=MERCHANT, method="card", baseline=1000):
    return CohortAggregate(
        PaymentCohortV2(
            merchant,
            method,
            CohortDimension(None, FieldAvailability.MISSING),
            CohortDimension(None, FieldAvailability.REDACTED),
            AmountBand.BAND_5,
        ),
        baseline,
        baseline,
        0,
        quantized_rate(baseline, baseline),
        0,
        total,
        success,
        total - success,
        quantized_rate(success, total),
        (total - success) * 9_000_000_000_000_000_000,
        quantized_drop(baseline, baseline, success, total),
        (),
        None,
    )


def factory(engine):
    sessions = build_session_factory(engine=engine)
    return lambda: SqlAlchemyUnitOfWork(sessions)


def run_scan(engine, offset=0, values=None, merchant=MERCHANT):
    when = NOW + timedelta(minutes=15 * offset)
    with factory(engine)() as uow:
        if values is not None:

            class Analytics:
                def aggregate_for_merchant(self, merchant_id, window):
                    return tuple(values)

            uow.payment_analytics = Analytics()
        return scan_merchant(merchant, when, uow, now=when)


def counts(engine):
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(row))
            for row in (ScanRunRow, IncidentRow, ObservationRow, IncidentErrorRow, AuditRecordRow)
        )


def stored(engine):
    with factory(engine)() as uow:
        items, _ = uow.incidents.list_incidents(MERCHANT, 0, None, 100)
        return items


def test_open_update_resolve_and_new_incident_preserve_opening_evidence(clean_database):
    first = run_scan(clean_database, values=[evidence(850)])
    assert first.summary.opened == 1
    run_scan(clean_database, 1, [evidence(500)])
    run_scan(clean_database, 2, [evidence(1000)])
    resolved = run_scan(clean_database, 3, [evidence(1000)])
    assert resolved.summary.resolved == 1
    original = stored(clean_database)[0]
    assert original.status == "resolved"
    assert original.opening_severity == "medium"
    assert original.peak_severity == "critical"
    assert original.latest_outcome == "healthy"
    assert original.healthy_streak == 2
    with factory(clean_database)() as uow:
        detail = incident_detail(uow.incidents, MERCHANT, original.id, 100, None)
        assert detail.opening_observation.evidence.observation_success_count == 850
        assert [item.detection.severity for item in detail.observations] == [
            "medium",
            "critical",
            None,
            None,
        ]
        assert detail.observations[1].evidence.observation_failed_amount_paise == (
            4_500_000_000_000_000_000_000
        )
    later = run_scan(clean_database, 4, [evidence(500)])
    assert later.summary.opened == 1
    assert stored(clean_database)[0] == original
    assert len(stored(clean_database)) == 2
    with Session(clean_database) as session:
        audits = list(session.scalars(select(AuditRecordRow)))
        assert sum(a.event_type == "incident.resolved" for a in audits) == 1
        assert all(a.payment_event_id is None and a.incident_id is not None for a in audits)
        assert all(
            set(a.details)
            <= {
                "scan_id",
                "observation_id",
                "cohort_sha256",
                "detector_version",
                "observation_start",
                "observation_end",
                "opening_severity",
                "outcome",
                "healthy_streak",
                "reason",
            }
            for a in audits
        )


@pytest.mark.parametrize("interruption", ["watch", "insufficient", "absent", "gap"])
def test_healthy_streak_resets_in_database(clean_database, interruption):
    run_scan(clean_database, values=[evidence()])
    run_scan(clean_database, 1, [evidence(1000)])
    if interruption != "gap":
        values = {"watch": [evidence(920)], "insufficient": [evidence(10, 10)], "absent": []}
        run_scan(clean_database, 2, values[interruption])
        assert stored(clean_database)[0].healthy_streak == 0
        if interruption == "absent":
            with Session(clean_database) as session:
                observation = session.scalar(
                    select(ObservationRow).order_by(ObservationRow.id.desc())
                )
                assert observation.baseline_total == observation.observation_total == 0
                assert (
                    observation.baseline_success_rate
                    is observation.observation_success_rate
                    is None
                )
                assert observation.outcome == "insufficient"
    run_scan(clean_database, 3, [evidence(1000)])
    assert stored(clean_database)[0].status == "open"
    assert stored(clean_database)[0].healthy_streak == 1
    run_scan(clean_database, 4, [evidence(1000)])
    assert stored(clean_database)[0].status == "resolved"


def test_exact_replay_and_late_events_freeze_summary(clean_database):
    first = run_scan(clean_database, values=[evidence()])
    before = counts(clean_database)
    replay = run_scan(clean_database, values=[evidence(1000)])
    assert replay.replayed
    assert replay.summary == first.summary
    assert counts(clean_database) == before
    run_scan(clean_database, 2, [evidence(1000)])
    assert run_scan(clean_database, values=[]).summary == first.summary
    with pytest.raises(StaleScan):
        run_scan(clean_database, 1, [evidence()])


def test_audit_failure_rolls_back_claim_incident_observation_and_errors(clean_database):
    class Analytics:
        def aggregate_for_merchant(self, merchant_id, window):
            return (
                replace(
                    evidence(),
                    observation_error_code_distribution=(ErrorCodeCount("E", 1),),
                    dominant_observation_error_code="E",
                ),
            )

    class FailingAudits:
        def append(self, record):
            raise RuntimeError("synthetic audit unavailable")

    with factory(clean_database)() as uow:
        uow.payment_analytics = Analytics()
        uow.audit_records = FailingAudits()
        with pytest.raises(ScanUnavailable):
            scan_merchant(MERCHANT, NOW, uow, now=NOW)
    assert counts(clean_database) == (0, 0, 0, 0, 0)
    assert run_scan(clean_database, values=[evidence()]).summary.opened == 1


def test_concurrent_identical_scans_commit_one_result(clean_database):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_scan(clean_database, values=[evidence()]), range(2)))
    assert sorted(r.replayed for r in results) == [False, True]
    assert results[0].summary == results[1].summary
    assert counts(clean_database) == (1, 1, 1, 0, 1)


def test_merchant_lock_serializes_chronology_but_other_merchant_proceeds(clean_database):
    locked, release, waiting = Event(), Event(), Event()
    sessions = factory(clean_database)

    def first():
        with sessions() as uow:
            uow.incidents.lock_merchant(MERCHANT)
            locked.set()
            assert release.wait(15)
            return scan_merchant(MERCHANT, NOW, uow, now=NOW)

    def later():
        waiting.set()
        return run_scan(clean_database, 1)

    with ThreadPoolExecutor(max_workers=3) as pool:
        one = pool.submit(first)
        assert locked.wait(10)
        two = pool.submit(later)
        assert waiting.wait(10)
        try:
            independent = pool.submit(run_scan, clean_database, 0, [], "acc_Other")
            assert independent.result(timeout=10).summary.merchant_id == "acc_Other"
            assert not two.done()
        finally:
            release.set()
        assert one.result(timeout=10).summary.window.observation_start < (
            two.result(timeout=10).summary.window.observation_start
        )
    assert counts(clean_database)[0] == 3


def test_cli_success_replay_and_sanitized_database_error(clean_database, capsys):
    calls = []

    def clock():
        calls.append(1)
        return NOW

    for replay in (False, True):
        assert main(["--merchant-id", MERCHANT], clock=clock, factory=factory(clean_database)) == 0
        import json

        assert json.loads(capsys.readouterr().out)["replayed"] is replay
    assert calls == [1, 1]

    def broken():
        raise RuntimeError("postgresql://sensitive-do-not-print")

    assert main(["--merchant-id", MERCHANT], clock=clock, factory=broken) == 1
    assert "sensitive" not in capsys.readouterr().err


def test_api_scoping_and_complete_stable_cursor_pages(clean_database):
    run_scan(
        clean_database,
        values=[evidence(method="card"), evidence(method="upi"), evidence(method="wallet")],
    )
    run_scan(clean_database, 1, [evidence(method="card")])
    run_scan(clean_database, 2, [evidence(method="card")])
    run_scan(clean_database, values=[evidence(merchant="acc_Other")], merchant="acc_Other")
    app = create_app()
    app.dependency_overrides[get_unit_of_work_factory] = lambda: factory(clean_database)
    client = TestClient(app)
    first = client.get("/v1/incidents", params={"merchant_id": MERCHANT, "limit": 1})
    assert first.status_code == 200
    page = first.json()
    items = page["items"]
    # New incident after page one must not change this traversal's upper bound.
    run_scan(clean_database, 3, [evidence(method="emi")])
    while page["next_cursor"]:
        page = client.get(
            "/v1/incidents",
            params={"merchant_id": MERCHANT, "limit": 1, "cursor": page["next_cursor"]},
        ).json()
        items += page["items"]
    assert len(items) == len({item["id"] for item in items}) == 3
    target = items[0]["id"]
    assert client.get(f"/v1/incidents/{target}").status_code == 422
    hidden = client.get(f"/v1/incidents/{target}", params={"merchant_id": "acc_Other"})
    missing = client.get("/v1/incidents/999999", params={"merchant_id": MERCHANT})
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json() == {"detail": "Incident not found"}
    page = client.get(
        f"/v1/incidents/{target}", params={"merchant_id": MERCHANT, "limit": 1}
    ).json()
    observations = page["observations"]
    opening = page["opening_observation"]
    while page["next_cursor"]:
        page = client.get(
            f"/v1/incidents/{target}",
            params={"merchant_id": MERCHANT, "limit": 1, "cursor": page["next_cursor"]},
        ).json()
        observations += page["observations"]
        assert page["opening_observation"] == opening
    assert len(observations) == len({o["id"] for o in observations}) == 4
    assert isinstance(opening["evidence"]["observation_success_rate"], str)
    for path in ("/v1/incidents", f"/v1/incidents/{target}"):
        assert (
            client.get(path, params={"merchant_id": MERCHANT, "cursor": "bad!"}).status_code == 422
        )
        assert client.get(path, params={"merchant_id": MERCHANT, "limit": 101}).status_code == 422
    app.dependency_overrides[get_unit_of_work_factory] = lambda: (
        lambda: (_ for _ in ()).throw(RuntimeError("sensitive connection string"))
    )
    failure = client.get("/v1/incidents", params={"merchant_id": MERCHANT})
    assert failure.status_code == 503
    assert failure.json() == {"detail": "Incident data is unavailable"}


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("incidents", "cohort_hash", "A" * 64),
        ("incidents", "issuer_availability", "provided"),
        ("incidents", "healthy_streak", 2),
        ("incident_observations", "severity", None),
        ("incident_observations", "baseline_success_count", -1),
        ("incident_observations", "z_score", Decimal("NaN")),
        ("incident_observations", "observation_failed_amount_paise", Decimal("Infinity")),
        ("incident_observations", "merchant_id", "acc_Other"),
    ],
)
def test_database_incident_invariants(clean_database, table, column, value):
    run_scan(clean_database, values=[evidence()])
    with clean_database.connect() as connection:
        # PostgreSQL rejects infinity at the NUMERIC typmod before CHECK evaluation.
        expected = DataError if value == Decimal("Infinity") else IntegrityError
        with pytest.raises(expected) as exc_info:
            connection.execute(text(f"UPDATE {table} SET {column} = :value"), {"value": value})
        assert exc_info.value.orig.sqlstate in {"22003", "23514", "23503"}
        connection.rollback()


@pytest.mark.parametrize("table", ["incidents", "incident_scan_runs", "incident_observations"])
def test_restrictive_history_foreign_keys(clean_database, table):
    run_scan(clean_database, values=[evidence()])
    if table == "incident_observations":
        with clean_database.begin() as connection:
            connection.execute(text("INSERT INTO incident_error_counts VALUES (1, 1, 'ERROR', 1)"))
    with clean_database.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(f"DELETE FROM {table}"))
        connection.rollback()


def test_real_postgresql_aggregation_and_late_arrival_snapshot(clean_database):
    from test_phase3a1_analytics import add_event

    with Session(clean_database) as session:
        for i in range(100):
            add_event(
                session,
                f"base_{i}",
                NOW - timedelta(days=1),
                merchant_id=MERCHANT,
                amount_paise=9_000_000_000_000_000_000,
            )
        for i in range(30):
            add_event(
                session,
                f"obs_{i}",
                NOW - timedelta(minutes=10),
                merchant_id=MERCHANT,
                amount_paise=9_000_000_000_000_000_000,
                status="failed",
                error_code=f"ERROR_{i:02d}" if i < 22 else None,
            )
        session.commit()
    first = run_scan(clean_database)
    assert first.summary.opened == 1
    with factory(clean_database)() as uow:
        value = stored(clean_database)[0]
        detail = incident_detail(uow.incidents, MERCHANT, value.id, 100, None)
        aggregate = detail.opening_observation.evidence
        assert aggregate.observation_failed_amount_paise == 270_000_000_000_000_000_000
        assert [
            (e.error_code, e.failed_count) for e in aggregate.observation_error_code_distribution
        ] == [(f"ERROR_{i:02d}", 1) for i in range(20)]
    before = counts(clean_database)
    with Session(clean_database) as session:
        add_event(
            session,
            "late",
            NOW - timedelta(minutes=10),
            merchant_id=MERCHANT,
            amount_paise=9_000_000_000_000_000_000,
        )
        session.commit()
    assert run_scan(clean_database).summary == first.summary
    assert counts(clean_database) == before
    run_scan(clean_database, 1)
    with Session(clean_database) as session:
        latest = session.scalar(select(ObservationRow).order_by(ObservationRow.id.desc()))
        assert latest.baseline_total == 131


def test_upgrade_with_existing_phase3a1_data_and_reupgrade(clean_database, migrated_database_url):
    from test_phase3a1_analytics import add_event

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    with Session(clean_database) as session:
        add_event(session, "retained", NOW, merchant_id=MERCHANT)
        session.flush()
        payment_id = session.scalar(select(PaymentEventRow.id))
        session.add(
            AuditRecordRow(
                correlation_id=uuid4(),
                payment_event_id=payment_id,
                event_type="payment_event.ingested",
                actor_type="test",
                details={"schema_version": 2, "source": "analytics_test"},
            )
        )
        session.commit()
    with clean_database.connect() as connection:
        payment_before = connection.execute(text("SELECT * FROM payment_events")).mappings().one()
        audit_before = connection.execute(text("SELECT * FROM audit_records")).mappings().one()
    try:
        command.downgrade(config, "20260904_0003")
        with clean_database.begin() as connection:
            # Existing contract permits subjectless audit rows; migration must preserve them.
            connection.execute(
                text(
                    "INSERT INTO audit_records "
                    "(correlation_id,event_type,actor_type) VALUES (:id,'legacy','test')"
                ),
                {"id": uuid4()},
            )
        command.upgrade(config, "head")
        assert "incident_scan_runs" in inspect(clean_database).get_table_names()
        with clean_database.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM audit_records")) == 2
            assert connection.execute(text("SELECT * FROM payment_events")).mappings().one() == (
                payment_before
            )
            assert (
                connection.execute(
                    text("SELECT * FROM audit_records WHERE payment_event_id IS NOT NULL")
                )
                .mappings()
                .one()
                == audit_before
            )
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "20260907_0006"
            )
        command.downgrade(config, "20260904_0003")
        assert "incidents" not in inspect(clean_database).get_table_names()
        command.upgrade(config, "head")
        with clean_database.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM audit_records")) == 2
    finally:
        command.upgrade(config, "head")


def test_failed_resolution_audit_rolls_back_existing_incident(clean_database):
    run_scan(clean_database, values=[evidence()])
    run_scan(clean_database, 1, [evidence(1000)])
    original, before = stored(clean_database), counts(clean_database)
    with factory(clean_database)() as uow:
        real_append = uow.audit_records.append

        def failing_append(record):
            if record.event_type == "incident.resolved":
                raise RuntimeError("synthetic audit failure")
            return real_append(record)

        uow.audit_records.append = failing_append

        class Healthy:
            def aggregate_for_merchant(self, merchant, window):
                return (evidence(1000),)

        uow.payment_analytics = Healthy()
        when = NOW + timedelta(minutes=30)
        with pytest.raises(ScanUnavailable):
            scan_merchant(MERCHANT, when, uow, now=when)
    assert counts(clean_database) == before
    assert stored(clean_database) == original
    assert run_scan(clean_database, 2, [evidence(1000)]).summary.resolved == 1


def test_later_concurrent_window_commits_first_and_older_is_stale(clean_database):
    locked, release = Event(), Event()

    def newer():
        with factory(clean_database)() as uow:
            uow.incidents.lock_merchant(MERCHANT)
            locked.set()
            assert release.wait(15)
            when = NOW + timedelta(minutes=15)
            return scan_merchant(MERCHANT, when, uow, now=when)

    with ThreadPoolExecutor(max_workers=2) as pool:
        later = pool.submit(newer)
        assert locked.wait(10)
        older = pool.submit(run_scan, clean_database)
        release.set()
        assert not later.result(timeout=10).replayed
        with pytest.raises(StaleScan):
            older.result(timeout=10)
    assert counts(clean_database)[0] == 1


@pytest.mark.parametrize(
    "row_type,field,value",
    [
        (IncidentRow, "opening_severity", "high"),
        (ObservationRow, "outcome", "watch"),
        (ScanRunRow, "evaluated", 999),
    ],
)
def test_opening_and_committed_evidence_immutable_through_orm(
    clean_database, row_type, field, value
):
    run_scan(clean_database, values=[evidence()])
    with Session(clean_database) as session:
        row = session.scalar(select(row_type))
        setattr(row, field, value)
        with pytest.raises(ValueError, match="immutable|append-only"):
            session.flush()


def test_database_unique_scan_open_incident_and_bounded_errors(clean_database):
    run_scan(clean_database, values=[evidence()])
    with clean_database.connect() as connection:
        for statement, constraint in (
            (
                "INSERT INTO incident_scan_runs SELECT gen_random_uuid(), merchant_id, "
                "detector_version, baseline_start, baseline_end, "
                "observation_start, observation_end, "
                "evaluated, opened, updated, resolved, recorded_at FROM incident_scan_runs",
                "uq_scan_merchant_detector_window",
            ),
            (
                "INSERT INTO incidents OVERRIDING USER VALUE SELECT * FROM incidents",
                "uq_incident_open_cohort",
            ),
            (
                "INSERT INTO incident_error_counts VALUES (1, 21, 'ERROR', 1)",
                "ck_incident_error_counts_rank_bound",
            ),
            (
                "INSERT INTO audit_records (correlation_id,event_type,actor_type) "
                "VALUES (gen_random_uuid(),'incident.opened','test')",
                "ck_audit_records_incident_subject",
            ),
        ):
            with pytest.raises(IntegrityError) as exc:
                connection.execute(text(statement))
            assert exc.value.orig.diag.constraint_name == constraint
            connection.rollback()


def test_downgrade_refuses_to_orphan_incident_audits(clean_database, migrated_database_url):
    from sqlalchemy.exc import DatabaseError

    value = replace(
        evidence(),
        observation_error_code_distribution=(ErrorCodeCount("SYNTHETIC_ERROR", 3),),
        dominant_observation_error_code="SYNTHETIC_ERROR",
    )
    run_scan(clean_database, values=[value])
    tables = {"incident_scan_runs", "incidents", "incident_observations", "incident_error_counts"}
    with clean_database.connect() as connection:
        before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
            for table in tables | {"audit_records"}
        }
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    with pytest.raises(DatabaseError, match="Cannot downgrade while incident audits exist"):
        command.downgrade(config, "20260904_0003")
    # A new connection/Inspector must observe committed schema and data after the refusal.
    with clean_database.connect() as connection:
        schema = inspect(connection)
        assert tables <= set(schema.get_table_names())
        assert "incident_id" in {column["name"] for column in schema.get_columns("audit_records")}
        fk = next(
            key for key in schema.get_foreign_keys("audit_records")
            if key["name"] == "fk_audit_records_incident_id_incidents"
        )
        assert fk["constrained_columns"] == ["incident_id"]
        assert fk["referred_table"] == "incidents"
        assert fk["referred_columns"] == ["id"]
        assert fk["options"]["ondelete"] == "RESTRICT"
        assert "ck_audit_records_incident_subject" in {
            check["name"] for check in schema.get_check_constraints("audit_records")
        }
        index = next(
            index for index in schema.get_indexes("audit_records")
            if index["name"] == "ix_audit_incident_history"
        )
        assert index["column_names"] == ["incident_id", "id"]
        assert "incident_id IS NOT NULL" in index["dialect_options"]["postgresql_where"]
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table}")).mappings().all() == rows
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260907_0006"
    assert counts(clean_database) == (1, 1, 1, 1, 1)


def test_observation_repository_enforces_merchant_pair_and_retains_evidence(clean_database):
    other = "acc_Other"
    errors = (ErrorCodeCount("SYNTHETIC_ERROR", 3),)
    value = replace(
        evidence(merchant=other),
        observation_error_code_distribution=errors,
        dominant_observation_error_code="SYNTHETIC_ERROR",
    )
    run_scan(clean_database, values=[evidence()])
    run_scan(clean_database, values=[value], merchant=other)
    run_scan(clean_database, 1, [value], merchant=other)
    with factory(clean_database)() as uow:
        incident = uow.incidents.list_incidents(other, 0, None, 1)[0][0]
        first, upper = uow.incidents.observations(other, incident.id, 0, None, 1)
        second, _ = uow.incidents.observations(other, incident.id, first[0].id, upper, 1)
        assert len(first) == len(second) == 1
        assert first[0].id < second[0].id == upper
        for observation in first + second:
            assert observation.incident_id == incident.id
            assert observation.evidence.cohort.merchant_id == other
            assert observation.evidence.observation_error_code_distribution == errors
            assert observation.evidence.dominant_observation_error_code == "SYNTHETIC_ERROR"
        # Unknown upper tests the aggregate query; a valid foreign upper tests page retrieval.
        for supplied_upper in (None, upper):
            hidden, hidden_upper = uow.incidents.observations(
                MERCHANT, incident.id, 0, supplied_upper, 100
            )
            assert hidden == ()  # No observations or nested error evidence.
            assert hidden_upper == (0 if supplied_upper is None else upper)


@pytest.mark.parametrize("proposed", ["high", "medium"])
@pytest.mark.parametrize("boundary", ["repository", "orm"])
@pytest.mark.parametrize("attribute_state", ["loaded", "expired", "unloaded"])
def test_peak_decrease_rejected_and_rolled_back(
    clean_database, proposed, boundary, attribute_state
):
    run_scan(clean_database, values=[evidence(850)])
    run_scan(clean_database, 1, [evidence(500)])
    incident = stored(clean_database)[0]
    assert incident.opening_severity == "medium"
    assert incident.peak_severity == "critical"
    with Session(clean_database) as session:
        query = select(IncidentRow).where(IncidentRow.id == incident.id)
        if attribute_state == "unloaded":
            query = query.options(load_only(IncidentRow.id))
        row = session.scalar(query)
        if attribute_state == "expired":
            session.expire(row)
        if attribute_state != "loaded":
            assert "peak_severity" in inspect(row).unloaded
        with pytest.raises(ValueError, match="peak severity cannot decrease"):
            if boundary == "repository":
                SqlAlchemyIncidentRepository(session).update_incident(
                    replace(incident, peak_severity=DegradationSeverity(proposed))
                )
            else:
                row.peak_severity = proposed
                if attribute_state != "loaded":
                    assert not inspect(row).attrs.peak_severity.history.deleted
                session.flush()
        session.rollback()
    assert stored(clean_database)[0] == incident  # Fresh session, all persisted state unchanged.


@pytest.mark.parametrize("boundary", ["repository", "orm"])
def test_peak_increases_and_unchanged_updates_are_allowed(clean_database, boundary):
    run_scan(clean_database, values=[evidence(850)])
    incident = stored(clean_database)[0]
    for peak in ("high", "critical", "critical"):
        with Session(clean_database) as session:
            if boundary == "repository":
                SqlAlchemyIncidentRepository(session).update_incident(
                    replace(incident, peak_severity=DegradationSeverity(peak))
                )
            else:
                row = session.get(IncidentRow, incident.id)
                row.peak_severity = peak
                row.last_window_start += timedelta(minutes=15)
            session.commit()
        persisted = stored(clean_database)[0]
        assert persisted.peak_severity == peak
        assert persisted.opening_severity == "medium"


@pytest.mark.parametrize("boundary", ["repository", "orm"])
def test_peak_guard_waits_for_concurrent_writer_and_rejects_stale_state(clean_database, boundary):
    run_scan(clean_database, values=[evidence(850)])
    incident = stored(clean_database)[0]
    loaded, proceed = Event(), Event()
    backend = []

    def stale_writer():
        with Session(clean_database) as session:
            row = session.get(IncidentRow, incident.id)
            assert row.peak_severity == "medium"
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            # Bound a broken implementation's wait, even if the assertion thread fails.
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            loaded.set()
            assert proceed.wait(10)
            try:
                with pytest.raises(ValueError, match="peak severity cannot decrease"):
                    if boundary == "repository":
                        SqlAlchemyIncidentRepository(session).update_incident(
                            replace(incident, peak_severity=DegradationSeverity.HIGH)
                        )
                    else:
                        row.peak_severity = "high"
                        session.flush()
            finally:
                session.rollback()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(stale_writer)
        try:
            assert loaded.wait(10)
            with Session(clean_database) as holder:
                holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
                SqlAlchemyIncidentRepository(holder).update_incident(
                    replace(incident, peak_severity=DegradationSeverity.CRITICAL)
                )
                proceed.set()
                deadline = monotonic() + 5
                with clean_database.connect() as observer:
                    while True:
                        blockers = observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": backend[0]}
                        )
                        if holder_pid in blockers:
                            break
                        assert monotonic() < deadline, "Peak check did not wait for the row lock"
                        sleep(0.01)
                holder.commit()
            pending.result(timeout=10)
        finally:
            proceed.set()
    persisted = stored(clean_database)[0]
    assert persisted.peak_severity == "critical"
    assert persisted.opening_severity == "medium"
