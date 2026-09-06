from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from payrecover.infrastructure.database.base import Base


class ScanRunRow(Base):
    __tablename__ = "incident_scan_runs"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "detector_version",
            "observation_start",
            name="uq_scan_merchant_detector_window",
        ),
        UniqueConstraint("id", "merchant_id", "detector_version", name="uq_scan_subject"),
        CheckConstraint("btrim(merchant_id) <> ''", name="merchant_nonblank"),
        CheckConstraint("btrim(detector_version) <> ''", name="detector_nonblank"),
        CheckConstraint(
            "baseline_start = baseline_end - interval '7 days' AND "
            "baseline_end = observation_start AND "
            "observation_end = observation_start + interval '15 minutes' AND "
            "mod(extract(epoch from observation_start), 900) = 0",
            name="windows",
        ),
        CheckConstraint(
            "evaluated >= 0 AND opened >= 0 AND updated >= 0 AND resolved >= 0 "
            "AND opened + updated <= evaluated AND resolved <= updated",
            name="summary",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(100))
    detector_version: Mapped[str] = mapped_column(String(64))
    baseline_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    baseline_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observation_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evaluated: Mapped[int] = mapped_column(BigInteger)
    opened: Mapped[int] = mapped_column(BigInteger)
    updated: Mapped[int] = mapped_column(BigInteger)
    resolved: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncidentRow(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["opened_scan_id", "merchant_id", "detector_version"],
            [
                "incident_scan_runs.id",
                "incident_scan_runs.merchant_id",
                "incident_scan_runs.detector_version",
            ],
            name="fk_incident_open_scan",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "merchant_id", "detector_version", name="uq_incident_subject"),
        CheckConstraint("cohort_version = 2", name="cohort_version"),
        CheckConstraint("cohort_hash ~ '^[0-9a-f]{64}$'", name="cohort_hex"),
        CheckConstraint(
            "btrim(merchant_id) <> '' AND btrim(method) <> '' AND btrim(detector_version) <> ''",
            name="dimensions_nonblank",
        ),
        CheckConstraint(
            "amount_band IN ('band_0','band_1','band_2','band_3','band_4','band_5')",
            name="amount_band",
        ),
        *[
            CheckConstraint(
                f"({dimension}_availability = 'provided' AND {dimension} IS NOT NULL "
                f"AND btrim({dimension}) <> '') OR ({dimension}_availability IN "
                f"('missing','not_applicable','redacted') AND {dimension} IS NULL)",
                name=f"{dimension}_availability",
            )
            for dimension in ("issuer", "provider")
        ],
        CheckConstraint(
            "opening_severity IN ('medium','high','critical') AND "
            "peak_severity IN ('medium','high','critical')",
            name="severities",
        ),
        CheckConstraint(
            "(opening_severity = 'medium') OR "
            "(opening_severity = 'high' AND peak_severity IN ('high','critical')) OR "
            "(opening_severity = 'critical' AND peak_severity = 'critical')",
            name="peak_at_least_opening",
        ),
        CheckConstraint(
            "latest_outcome IN ('degraded','healthy','watch','insufficient')", name="outcome"
        ),
        CheckConstraint(
            "healthy_streak BETWEEN 0 AND 2 AND "
            "((latest_outcome = 'healthy' AND healthy_streak > 0) OR "
            "(latest_outcome <> 'healthy' AND healthy_streak = 0))",
            name="streak",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND healthy_streak < 2) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL AND healthy_streak = 2)",
            name="status",
        ),
        CheckConstraint(
            "last_window_start >= opening_window_start AND "
            "(resolved_at IS NULL OR resolved_at >= opened_at)",
            name="chronology",
        ),
        Index(
            "uq_incident_open_cohort",
            "merchant_id",
            "cohort_version",
            "cohort_hash",
            "detector_version",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_incident_merchant_history", "merchant_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(100))
    cohort_version: Mapped[int] = mapped_column(Integer)
    cohort_hash: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(32))
    issuer: Mapped[str | None] = mapped_column(String(100))
    issuer_availability: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str | None] = mapped_column(String(100))
    provider_availability: Mapped[str] = mapped_column(String(20))
    amount_band: Mapped[str] = mapped_column(String(16))
    detector_version: Mapped[str] = mapped_column(String(64))
    opened_scan_id: Mapped[UUID]
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opening_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opening_severity: Mapped[str] = mapped_column(String(16))
    peak_severity: Mapped[str] = mapped_column(String(16))
    latest_outcome: Mapped[str] = mapped_column(String(16))
    last_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    healthy_streak: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObservationRow(Base):
    __tablename__ = "incident_observations"
    __table_args__ = (
        UniqueConstraint("incident_id", "scan_id", name="uq_incident_observation_scan"),
        ForeignKeyConstraint(
            ["incident_id", "merchant_id", "detector_version"],
            ["incidents.id", "incidents.merchant_id", "incidents.detector_version"],
            name="fk_observation_incident",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scan_id", "merchant_id", "detector_version"],
            [
                "incident_scan_runs.id",
                "incident_scan_runs.merchant_id",
                "incident_scan_runs.detector_version",
            ],
            name="fk_observation_scan",
            ondelete="RESTRICT",
        ),
        CheckConstraint("outcome IN ('degraded','healthy','watch','insufficient')", name="outcome"),
        CheckConstraint(
            "(outcome = 'degraded' AND severity IS NOT NULL AND "
            "severity IN ('medium','high','critical')) OR "
            "(outcome <> 'degraded' AND severity IS NULL)",
            name="severity",
        ),
        *[
            CheckConstraint(
                f"{prefix}_total >= 0 AND {prefix}_success_count >= 0 "
                f"AND {prefix}_failed_count >= 0 "
                f"AND {prefix}_total = {prefix}_success_count + {prefix}_failed_count AND "
                f"{prefix}_failed_amount_paise >= 0 AND "
                f"{prefix}_failed_amount_paise < 'Infinity'::numeric AND "
                f"(({prefix}_total = 0 AND {prefix}_success_rate IS NULL) OR "
                f"({prefix}_total > 0 AND {prefix}_success_rate IS NOT NULL AND "
                f"{prefix}_success_rate BETWEEN 0 AND 1))",
                name=f"{prefix}_evidence",
            )
            for prefix in ("baseline", "observation")
        ],
        CheckConstraint(
            "absolute_success_rate_drop IS NULL OR absolute_success_rate_drop BETWEEN -1 AND 1",
            name="drop_range",
        ),
        CheckConstraint(
            "z_score IS NULL OR (z_score > '-Infinity'::numeric AND z_score < 'Infinity'::numeric)",
            name="score_finite",
        ),
        Index("ix_observation_history", "incident_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    incident_id: Mapped[int] = mapped_column(BigInteger)
    scan_id: Mapped[UUID]
    merchant_id: Mapped[str] = mapped_column(String(100))
    detector_version: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str | None] = mapped_column(String(16))
    baseline_total: Mapped[int] = mapped_column(BigInteger)
    baseline_success_count: Mapped[int] = mapped_column(BigInteger)
    baseline_failed_count: Mapped[int] = mapped_column(BigInteger)
    baseline_failed_amount_paise: Mapped[Decimal] = mapped_column(Numeric(38, 0))
    baseline_success_rate: Mapped[Decimal | None] = mapped_column(Numeric(13, 12))
    observation_total: Mapped[int] = mapped_column(BigInteger)
    observation_success_count: Mapped[int] = mapped_column(BigInteger)
    observation_failed_count: Mapped[int] = mapped_column(BigInteger)
    observation_failed_amount_paise: Mapped[Decimal] = mapped_column(Numeric(38, 0))
    observation_success_rate: Mapped[Decimal | None] = mapped_column(Numeric(13, 12))
    absolute_success_rate_drop: Mapped[Decimal | None] = mapped_column(Numeric(13, 12))
    z_score: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncidentErrorRow(Base):
    __tablename__ = "incident_error_counts"
    __table_args__ = (
        UniqueConstraint("observation_id", "error_code", name="uq_observation_error_code"),
        CheckConstraint("rank BETWEEN 1 AND 20", name="rank_bound"),
        CheckConstraint("btrim(error_code) <> ''", name="code_nonblank"),
        CheckConstraint("failed_count > 0", name="count_positive"),
    )
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("incident_observations.id", ondelete="RESTRICT"), primary_key=True
    )
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    error_code: Mapped[str] = mapped_column(String(100))
    failed_count: Mapped[int] = mapped_column(BigInteger)


def reject_peak_decrease(persisted: str, proposed: str) -> None:
    ranks = {"medium": 0, "high": 1, "critical": 2}
    if ranks[proposed] < ranks[persisted]:
        raise ValueError("Incident peak severity cannot decrease")


@event.listens_for(Session, "before_flush")
def protect_incident_evidence(session: Session, flush_context: object, instances: object) -> None:
    immutable = (ObservationRow, IncidentErrorRow)
    opening_fields = (
        "merchant_id",
        "cohort_version",
        "cohort_hash",
        "method",
        "issuer",
        "issuer_availability",
        "provider",
        "provider_availability",
        "amount_band",
        "detector_version",
        "opened_scan_id",
        "opened_at",
        "opening_window_start",
        "opening_severity",
    )
    for row in session.dirty.union(session.deleted):
        if isinstance(row, ScanRunRow):
            if row in session.deleted or row.id not in session.info.get(
                "incident_scan_claims", set()
            ):
                raise ValueError("Committed scan summaries are immutable")
        if isinstance(row, immutable):
            raise ValueError("Incident evidence is append-only through the ORM")
        if isinstance(row, IncidentRow):
            state = inspect(row)
            if row in session.deleted or any(
                state.attrs[key].history.has_changes() for key in opening_fields
            ):
                raise ValueError("Incident opening evidence is immutable")
            if state.persistent:
                assert state.identity is not None
                # Read columns through the connection to bypass identity-map state and avoid
                # refreshing away pending changes. History may be absent or stale. The lock
                # spans this check and the ensuing UPDATE until commit/rollback.
                persisted = session.connection().execute(
                    select(IncidentRow.__table__.c.peak_severity, IncidentRow.__table__.c.status)
                    .where(IncidentRow.__table__.c.id == state.identity[0])
                    .with_for_update()
                ).one()
                if persisted.status == "resolved":
                    raise ValueError("Resolved incidents are immutable")
                reject_peak_decrease(
                    persisted.peak_severity,
                    state.dict.get("peak_severity", persisted.peak_severity),
                )
