import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from payrecover.infrastructure.database.models import PaymentEventRow
from payrecover.infrastructure.database.planning import SqlAlchemyPlanningRepository, candidate
from payrecover.infrastructure.database.simulation_models import (
    SimulationAttempt,
    SimulationMarker,
    SimulationPayment,
    SimulationRun,
    SimulationStep,
)
from payrecover.simulation.eligibility import eligibility
from payrecover.simulation.scenario import fingerprint


def configured_url() -> str:
    from sqlalchemy.engine import make_url

    value = os.environ.get("PAYRECOVER_SIMULATION_DATABASE_URL")
    if not value:
        raise ValueError("PAYRECOVER_SIMULATION_DATABASE_URL is required")
    url = make_url(value)
    if (
        url.drivername != "postgresql+psycopg"
        or not re.fullmatch(r"payrecover_sim_[a-z0-9_]+", url.database or "")
        or url.port == 5432
        and url.host in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("Target is not a dedicated simulation database")
    return value


def validate_database(engine: Engine, *, marker: bool = True) -> None:
    with engine.connect() as connection:
        name = connection.scalar(text("SELECT current_database()"))
        if not isinstance(name, str) or not re.fullmatch(r"payrecover_sim_[a-z0-9_]+", name):
            raise ValueError("Not a simulation database")
        if marker:
            version = connection.scalar(
                select(SimulationMarker.version).where(SimulationMarker.database_name == name)
            )
            if version != "simulation-only-v1":
                raise ValueError("Simulation initialization marker missing or incompatible")


def initialize(engine: Engine) -> None:
    """Explicit CLI setup only, never called by ordinary execution."""
    validate_database(engine, marker=False)
    with engine.begin() as connection:
        if connection.scalar(text("SELECT EXISTS(SELECT 1 FROM payment_events)")):
            raise ValueError("Initialization requires an empty payment database")
        connection.execute(
            text("""
            INSERT INTO simulation_marker(database_name,version)
            VALUES(current_database(),'simulation-only-v1') ON CONFLICT DO NOTHING
        """)
        )
    validate_database(engine)


@contextmanager
def run_lock(engine: Engine, run: UUID) -> Iterator[None]:
    # A physical, non-pooled session owns the lock; business UOWs never use it.
    owner = create_engine(engine.url, poolclass=NullPool)
    key = int.from_bytes(run.bytes[:8], "big", signed=True)
    try:
        with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            if not acquired:
                raise ValueError("Simulation run is already active")
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        owner.dispose()


class SimulationStore:
    def __init__(self, engine: Engine, run: UUID):
        self.engine, self.run = engine, run

    def start(self, merchant: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
        with Session(self.engine) as session, session.begin():
            row = session.get(SimulationRun, self.run)
            if row:
                if row.manifest_sha256 != fingerprint(manifest) or row.manifest != manifest:
                    raise ValueError("Incompatible simulation resume")
                return row.report if row.status == "complete" else None
            session.add(
                SimulationRun(
                    id=self.run,
                    merchant_id=merchant,
                    manifest=manifest,
                    manifest_sha256=fingerprint(manifest),
                    status="running",
                )
            )
        return None

    def steps(self) -> dict[str, dict[str, Any]]:
        with Session(self.engine) as session:
            return {
                row.key: row.data
                for row in session.scalars(
                    select(SimulationStep).where(SimulationStep.run_id == self.run)
                )
            }

    def append(self, key: str, kind: str, now: datetime, data: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            self._append(session, key, kind, now, data)

    def _append(
        self, session: Session, key: str, kind: str, now: datetime, data: dict[str, Any]
    ) -> None:
        existing = session.get(SimulationStep, (self.run, key))
        if existing:
            if existing.data != data:
                raise ValueError("Conflicting simulation step")
            return
        session.add(SimulationStep(run_id=self.run, key=key, kind=kind, logical_at=now, data=data))

    def population(self, merchant: str, event_id: UUID, fixture: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            event = session.scalar(
                select(PaymentEventRow).where(
                    PaymentEventRow.id == event_id,
                    PaymentEventRow.merchant_id == merchant,
                    PaymentEventRow.status == "failed",
                )
            )
            if event is None:
                raise ValueError("Invalid simulation population event")
            if session.get(SimulationPayment, (self.run, event.payment_id)) is None:
                session.add(
                    SimulationPayment(
                        run_id=self.run,
                        payment_id=event.payment_id,
                        merchant_id=merchant,
                        source=event.source,
                        event_id=event.id,
                        amount_paise=event.amount_paise,
                        fixture=fixture,
                    )
                )

    def attach_plan(self, merchant: str, payment: str, plan_id: UUID) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(SimulationPayment, (self.run, payment), with_for_update=True)
            if row is None or row.merchant_id != merchant:
                raise ValueError("Invalid plan association")
            plan = SqlAlchemyPlanningRepository(session).get_plan(merchant, plan_id)
            if plan.payment_event_id != row.event_id:
                raise ValueError("Conflicting plan association")
            if row.plan_id not in (None, plan_id):
                raise ValueError("Immutable plan association")
            row.plan_id = plan_id

    def payments(self) -> list[SimulationPayment]:
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(SimulationPayment)
                    .where(SimulationPayment.run_id == self.run)
                    .order_by(SimulationPayment.payment_id)
                )
            )

    def attempts(self, payment: str) -> list[SimulationAttempt]:
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(SimulationAttempt)
                    .where(
                        SimulationAttempt.run_id == self.run,
                        SimulationAttempt.payment_id == payment,
                    )
                    .order_by(SimulationAttempt.ordinal)
                )
            )

    def reserve(self, merchant: str, payment: str, ordinal: int, now: datetime) -> bool:
        with Session(self.engine) as session, session.begin():
            row = session.get(SimulationPayment, (self.run, payment), with_for_update=True)
            if row is None or row.merchant_id != merchant:
                raise ValueError("Invalid simulation payment")
            existing = session.get(SimulationAttempt, (self.run, payment, ordinal))
            if existing:
                return True  # Stable dispatch identity, not another reservation.
            attempts = list(
                session.scalars(
                    select(SimulationAttempt)
                    .where(
                        SimulationAttempt.run_id == self.run,
                        SimulationAttempt.payment_id == payment,
                    )
                    .order_by(SimulationAttempt.ordinal)
                )
            )
            if ordinal != len(attempts) + 1:
                raise ValueError("Nonsequential attempt")
            blocker = "unplanned"
            if row.plan_id:
                repo = SqlAlchemyPlanningRepository(session)
                plan = repo.get_plan(merchant, row.plan_id)
                event = session.get(PaymentEventRow, row.event_id)
                if event is None:
                    raise ValueError("Missing event")
                history = repo.history(merchant, candidate(event))
                blocker = (
                    eligibility(
                        plan,
                        history,
                        row.fixture["visible"],
                        tuple(a.state for a in attempts),
                        now,
                        attempts[-1].reserved_at if attempts else None,
                    )
                    or "eligible"
                )
            self._append(
                session,
                f"eligibility:{payment}:{ordinal}",
                "eligibility",
                now,
                {
                    "payment": payment,
                    "ordinal": ordinal,
                    "reason": blocker,
                    "execution_authorized": False,
                },
            )
            if blocker != "eligible":
                return False
            session.add(
                SimulationAttempt(
                    run_id=self.run,
                    payment_id=payment,
                    ordinal=ordinal,
                    reserved_at=now,
                    state="reserved",
                )
            )
            self._append(
                session,
                f"reserved:{payment}:{ordinal}",
                "reserved",
                now,
                {"payment": payment, "ordinal": ordinal},
            )
            return True

    def outcome(self, payment: str, ordinal: int, value: dict[str, Any], now: datetime) -> None:
        with Session(self.engine) as session, session.begin():
            session.get(SimulationPayment, (self.run, payment), with_for_update=True)
            row = session.get(SimulationAttempt, (self.run, payment, ordinal))
            if row is None:
                raise ValueError("Attempt not reserved")
            if row.state != "reserved":
                if row.truth != value["truth"]:
                    raise ValueError("Conflicting simulated truth")
                return
            row.truth, row.state = value["truth"], value["observed"]
            self._append(
                session,
                f"outcome:{payment}:{ordinal}",
                "outcome",
                now,
                {"payment": payment, "ordinal": ordinal, **value},
            )

    def reconcile(self, payment: str, ordinal: int, now: datetime) -> None:
        with Session(self.engine) as session, session.begin():
            p = session.get(SimulationPayment, (self.run, payment), with_for_update=True)
            row = session.get(SimulationAttempt, (self.run, payment, ordinal))
            if row is None or p is None or row.state != "uncertain":
                return
            hidden = p.fixture["hidden"]
            elapsed = (now - row.reserved_at).total_seconds()
            if elapsed < hidden["reconcile_seconds"]:
                raise ValueError("Premature reconciliation")
            if hidden["reconcilable"]:
                assert row.truth is not None
                row.state = row.truth
            self._append(
                session,
                f"reconcile:{payment}:{ordinal}",
                "reconcile",
                now,
                {"payment": payment, "ordinal": ordinal, "observed": row.state},
            )

    def finish(self, report: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(SimulationRun, self.run, with_for_update=True)
            assert row is not None
            row.report, row.status = report, "complete"
