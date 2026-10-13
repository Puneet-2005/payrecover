"""Dashboard-only read connection and scoped, SQL-bounded projections."""

import os
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, and_, create_engine, distinct, func, select, text, tuple_
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from payrecover.domain.dashboard import DashboardNotFound, PageCursor, inr, position
from payrecover.domain.planning import IncidentDiagnosis, RecoveryRecommendation
from payrecover.infrastructure.database.incident_models import IncidentRow, ObservationRow
from payrecover.infrastructure.database.incidents import SqlAlchemyIncidentRepository
from payrecover.infrastructure.database.models import PaymentEventRow
from payrecover.infrastructure.database.planning import record
from payrecover.infrastructure.database.planning_models import DiagnosisRow, PlanRow
from payrecover.infrastructure.database.simulation_models import (
    SimulationAttempt,
    SimulationMarker,
    SimulationPayment,
    SimulationRun,
    SimulationStep,
)
from payrecover.services.incident_reads import incident_detail, list_incidents
from payrecover.simulation.classification import classify_payment


def dashboard_engine(url: str | None = None) -> Engine:
    value = url or os.environ.get("PAYRECOVER_SIMULATION_DATABASE_URL")
    if not value:
        raise ValueError("Dedicated simulation URL required")
    parsed = make_url(value)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.query
        or not re.fullmatch(r"payrecover_sim_[a-z0-9_]+", parsed.database or "")
        or (parsed.port or 5432) == 5432
        and (parsed.host or "localhost").lower() in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("Invalid simulation database target")
    # libpq startup options precede SQLAlchemy's dialect discovery and EVERY application query.
    # No SET after autobegin, and no dependence on the normal settings/engine connect hooks.
    return create_engine(
        parsed,
        pool_pre_ping=True,
        connect_args={"options": "-c default_transaction_read_only=on -c timezone=UTC"},
    )


@contextmanager
def read_session(engine: Engine):
    with Session(engine) as session:
        try:
            yield session
        finally:
            session.rollback()


def validate(session: Session) -> None:
    name = session.scalar(text("SELECT current_database()"))
    if not isinstance(name, str) or not re.fullmatch(r"payrecover_sim_[a-z0-9_]+", name):
        raise ValueError("Not a simulation database")
    marker = session.scalar(
        select(SimulationMarker.version).where(SimulationMarker.database_name == name)
    )
    if marker != "simulation-only-v1":
        raise ValueError("Simulation marker required")


class DashboardReads:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def check_limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid page limit")

    def run(self, identity: UUID) -> SimulationRun:
        row = self.session.scalar(
            select(SimulationRun).where(
                SimulationRun.id == identity, SimulationRun.merchant_id.startswith("sim_")
            )
        )
        if row is None:
            raise DashboardNotFound("Run not found")
        return row

    def runs(self, limit: int, cursor: str | None) -> dict[str, Any]:
        self.check_limit(limit)
        after = position(cursor, "runs")
        statement = select(SimulationRun).where(SimulationRun.merchant_id.startswith("sim_"))
        if after:
            statement = statement.where(SimulationRun.id > UUID(after))
        rows = list(self.session.scalars(statement.order_by(SimulationRun.id).limit(limit + 1)))
        return self.page([self.summary(r) for r in rows], limit, "runs", "id")

    @staticmethod
    def summary(row: SimulationRun) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "status": row.status,
            "manifest": row.manifest,
            "simulation_only": True,
        }

    def overview(self, identity: UUID) -> dict[str, Any]:
        row = self.run(identity)
        report = row.report if row.status == "complete" else None
        amounts = report.get("accounting", {}).get("amount_paise", {}) if report else {}
        return {
            **self.summary(row),
            "report": report,
            "report_supported": row.manifest.get("versions", {}).get("report")
            == "simulation-report-v1",
            "formatted_amounts": {k: inr(v) for k, v in amounts.items()},
            "report_label": "Historical completed report"
            if report
            else "Incomplete / possibly interrupted — no final metrics",
        }

    @staticmethod
    def page(rows: list[dict[str, Any]], limit: int, scope: str, key: str) -> dict[str, Any]:
        return {
            "items": rows[:limit],
            "next_cursor": PageCursor(scope=scope, after=str(rows[limit - 1][key])).encode()
            if len(rows) > limit
            else None,
        }

    def incidents(self, identity: UUID, limit: int, cursor: str | None) -> Any:
        self.check_limit(limit)
        run = self.run(identity)
        scope = f"{identity}:{run.merchant_id}:incidents"
        inner = position(cursor, scope) or None
        value = list_incidents(
            SqlAlchemyIncidentRepository(self.session), run.merchant_id, limit, inner
        )
        return {
            "items": value.items,
            "next_cursor": PageCursor(scope=scope, after=value.next_cursor).encode()
            if value.next_cursor
            else None,
        }

    def incident(self, identity: UUID, incident: int, limit: int, cursor: str | None) -> Any:
        self.check_limit(limit)
        run = self.run(identity)
        exists = self.session.scalar(
            select(IncidentRow.id)
            .join(SimulationRun, SimulationRun.merchant_id == IncidentRow.merchant_id)
            .where(
                SimulationRun.id == identity,
                IncidentRow.merchant_id == run.merchant_id,
                IncidentRow.id == incident,
            )
        )
        if exists is None:
            raise DashboardNotFound("Incident not found")
        scope = f"{identity}:{run.merchant_id}:incident:{incident}"
        value = incident_detail(
            SqlAlchemyIncidentRepository(self.session),
            run.merchant_id,
            incident,
            limit,
            position(cursor, scope) or None,
        )
        return {
            "incident": value.incident,
            "opening_observation": value.opening_observation,
            "observations": value.observations,
            "next_cursor": PageCursor(scope=scope, after=value.next_cursor).encode()
            if value.next_cursor
            else None,
        }

    def diagnosis(self, identity: UUID, observation: int) -> Any:
        run = self.run(identity)
        row = self.session.scalar(
            select(DiagnosisRow)
            .join(
                ObservationRow,
                and_(
                    ObservationRow.id == DiagnosisRow.observation_id,
                    ObservationRow.incident_id == DiagnosisRow.incident_id,
                    ObservationRow.merchant_id == DiagnosisRow.merchant_id,
                ),
            )
            .join(SimulationRun, SimulationRun.merchant_id == DiagnosisRow.merchant_id)
            .where(
                SimulationRun.id == identity,
                DiagnosisRow.merchant_id == run.merchant_id,
                DiagnosisRow.observation_id == observation,
            )
        )
        if row is None:
            raise DashboardNotFound("No persisted diagnosis")
        return record(IncidentDiagnosis, row)

    def payment_query(self, identity: UUID):
        run = self.run(identity)
        return (
            select(SimulationPayment)
            .join(
                SimulationRun,
                and_(
                    SimulationRun.id == SimulationPayment.run_id,
                    SimulationRun.merchant_id == SimulationPayment.merchant_id,
                ),
            )
            .where(SimulationRun.id == identity, SimulationPayment.merchant_id == run.merchant_id)
        )

    def payment(self, identity: UUID, payment: str) -> SimulationPayment:
        row = self.session.scalar(
            self.payment_query(identity).where(SimulationPayment.payment_id == payment)
        )
        if row is None:
            raise DashboardNotFound("Simulation payment not found")
        return row

    def payment_summary(self, identity: UUID, payment: str) -> dict[str, Any]:
        p = self.payment(identity, payment)
        history = self.session.execute(
            select(
                func.count(distinct(PaymentEventRow.amount_paise)),
                func.bool_or(PaymentEventRow.status == "success"),
            )
            .join(
                SimulationPayment,
                and_(
                    SimulationPayment.merchant_id == PaymentEventRow.merchant_id,
                    SimulationPayment.source == PaymentEventRow.source,
                    SimulationPayment.payment_id == PaymentEventRow.payment_id,
                ),
            )
            .where(
                SimulationPayment.run_id == identity,
                SimulationPayment.merchant_id == p.merchant_id,
                SimulationPayment.payment_id == payment,
            )
        ).one()
        attempts = self.attempts(identity, payment)
        category = classify_payment(
            history[0], bool(history[1]), any(a["state"] == "succeeded" for a in attempts)
        )
        amount = None if category == "excluded" else p.amount_paise
        return {
            "payment": payment,
            "category": category,
            "amount_paise": amount,
            "amount_inr": inr(amount),
            "has_plan": p.plan_id is not None,
            "classification_label": "Current evidence classification",
        }

    def payments(self, identity: UUID, limit: int, cursor: str | None) -> dict[str, Any]:
        self.check_limit(limit)
        run = self.run(identity)
        scope = f"{identity}:{run.merchant_id}:payments"
        after = position(cursor, scope)
        if len(after) > 100:
            raise ValueError("Invalid payment cursor")
        rows = list(
            self.session.scalars(
                self.payment_query(identity)
                .where(SimulationPayment.payment_id > after)
                .order_by(SimulationPayment.payment_id)
                .limit(limit + 1)
            )
        )
        values = [self.payment_summary(identity, p.payment_id) for p in rows]
        return self.page(values, limit, scope, "payment")

    def attempts(self, identity: UUID, payment: str) -> list[dict[str, Any]]:
        p = self.payment(identity, payment)
        rows = self.session.scalars(
            select(SimulationAttempt)
            .join(
                SimulationPayment,
                and_(
                    SimulationAttempt.run_id == SimulationPayment.run_id,
                    SimulationAttempt.payment_id == SimulationPayment.payment_id,
                ),
            )
            .where(
                SimulationPayment.run_id == identity,
                SimulationPayment.merchant_id == p.merchant_id,
                SimulationPayment.payment_id == payment,
            )
            .order_by(SimulationAttempt.ordinal)
            .limit(2)
        )
        # Never serialize SimulationAttempt.truth, even for an uncertain outcome.
        return [
            {"ordinal": a.ordinal, "reserved_at": a.reserved_at, "state": a.state} for a in rows
        ]

    def payment_detail(self, identity: UUID, payment: str) -> dict[str, Any]:
        p = self.payment(identity, payment)
        plan = self.session.scalar(
            select(PlanRow)
            .join(
                SimulationPayment,
                and_(
                    SimulationPayment.plan_id == PlanRow.id,
                    SimulationPayment.merchant_id == PlanRow.merchant_id,
                    SimulationPayment.source == PlanRow.source,
                    SimulationPayment.payment_id == PlanRow.payment_id,
                ),
            )
            .where(
                SimulationPayment.run_id == identity,
                SimulationPayment.merchant_id == p.merchant_id,
                SimulationPayment.payment_id == payment,
            )
        )
        return {
            **self.payment_summary(identity, payment),
            "recommendation": record(RecoveryRecommendation, plan) if plan else None,
            "advice_label": "Historical advice — never execution authorization",
            "attempts": self.attempts(identity, payment),
        }

    def steps(
        self, identity: UUID, limit: int, cursor: str | None, payment: str | None = None
    ) -> dict[str, Any]:
        self.check_limit(limit)
        run = self.run(identity)
        if payment is not None:
            self.payment(identity, payment)
        scope = f"{identity}:{run.merchant_id}:steps:{payment or 'evaluations'}"
        after = position(cursor, scope)
        statement = (
            select(SimulationStep)
            .join(SimulationRun, SimulationRun.id == SimulationStep.run_id)
            .where(SimulationRun.id == identity, SimulationRun.merchant_id == run.merchant_id)
        )
        if payment is None:
            statement = statement.where(SimulationStep.kind == "evaluation")
        else:
            statement = statement.where(
                SimulationStep.data["payment"].astext == payment,
                SimulationStep.kind.in_(
                    ("eligibility", "reserved", "outcome", "reconcile", "suppression")
                ),
            )
        if after:
            try:
                time, key = after.split("|", 1)
                stamp = datetime.fromisoformat(time)
                if stamp.tzinfo is None or not 1 <= len(key) <= 160:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Invalid history cursor") from exc
            statement = statement.where(
                tuple_(SimulationStep.logical_at, SimulationStep.key) > (stamp, key)
            )
        rows = list(
            self.session.scalars(
                statement.order_by(SimulationStep.logical_at, SimulationStep.key).limit(limit + 1)
            )
        )
        safe = (
            ("cohort", "stage", "label", "outcome", "delay_seconds")
            if payment is None
            else ("ordinal", "reason", "execution_authorized", "observed")
        )
        items = [
            {
                "key": r.key,
                "kind": r.kind,
                "logical_at": r.logical_at,
                "data": {k: r.data[k] for k in safe if k in r.data},
            }
            for r in rows[:limit]
        ]
        return {
            "items": items,
            "next_cursor": PageCursor(
                scope=scope, after=f"{rows[limit - 1].logical_at.isoformat()}|{rows[limit - 1].key}"
            ).encode()
            if len(rows) > limit
            else None,
        }
