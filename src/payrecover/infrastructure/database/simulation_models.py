"""Simulation-only storage; no provider authorization capability."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from payrecover.infrastructure.database.base import Base


class SimulationMarker(Base):
    __tablename__ = "simulation_marker"
    database_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    version: Mapped[str] = mapped_column(String(32))


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    __table_args__ = (
        UniqueConstraint("id", "merchant_id", name="uq_sim_run_merchant"),
        CheckConstraint("merchant_id LIKE 'sim_%'", name="merchant"),
        CheckConstraint("status IN ('running','complete')", name="status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(100), unique=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class SimulationPayment(Base):
    __tablename__ = "simulation_payments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["simulation_runs.id", "simulation_runs.merchant_id"],
            name="fk_sim_payment_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_id", "merchant_id", "source", "payment_id"],
            [
                "payment_events.id",
                "payment_events.merchant_id",
                "payment_events.source",
                "payment_events.payment_id",
            ],
            name="fk_sim_payment_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["plan_id", "merchant_id", "source", "payment_id"],
            [
                "recovery_plans.id",
                "recovery_plans.merchant_id",
                "recovery_plans.source",
                "recovery_plans.payment_id",
            ],
            name="fk_sim_payment_plan",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount_paise > 0", name="amount"),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[UUID]
    plan_id: Mapped[UUID | None]
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    fixture: Mapped[dict[str, Any]] = mapped_column(JSONB)


class SimulationAttempt(Base):
    __tablename__ = "simulation_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "payment_id"],
            ["simulation_payments.run_id", "simulation_payments.payment_id"],
            name="fk_sim_attempt_payment",
            ondelete="RESTRICT",
        ),
        CheckConstraint("ordinal BETWEEN 1 AND 2", name="ordinal"),
        CheckConstraint("state IN ('reserved','uncertain','failed','succeeded')", name="state"),
        Index(
            "uq_sim_outstanding",
            "run_id",
            "payment_id",
            unique=True,
            postgresql_where=text("state IN ('reserved','uncertain')"),
        ),
        Index(
            "uq_sim_success",
            "run_id",
            "payment_id",
            unique=True,
            postgresql_where=text("state = 'succeeded'"),
        ),
    )
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16))
    truth: Mapped[str | None] = mapped_column(String(16))


class SimulationStep(Base):
    __tablename__ = "simulation_steps"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("simulation_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    logical_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@event.listens_for(Session, "before_flush")
def immutable_steps(session: Session, context: object, instances: object) -> None:
    if any(isinstance(row, SimulationStep) for row in session.dirty.union(session.deleted)):
        raise ValueError("Simulation history is append-only")
