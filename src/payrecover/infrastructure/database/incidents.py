from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from payrecover.domain.analytics import (
    AmountBand,
    AnalyticsWindow,
    CohortAggregate,
    CohortDimension,
    DegradationDetection,
    DegradationSeverity,
    DetectionOutcome,
    ErrorCodeCount,
    PaymentCohortV2,
)
from payrecover.domain.incidents import Incident, IncidentObservation, ScanSummary
from payrecover.domain.models import FieldAvailability
from payrecover.infrastructure.database.incident_models import (
    IncidentErrorRow,
    IncidentRow,
    ObservationRow,
    ScanRunRow,
    reject_peak_decrease,
)


def stored_scan(row: ScanRunRow) -> ScanSummary:
    return ScanSummary(
        row.id,
        row.merchant_id,
        row.detector_version,
        AnalyticsWindow(
            row.baseline_start, row.baseline_end, row.observation_start, row.observation_end
        ),
        row.evaluated,
        row.opened,
        row.updated,
        row.resolved,
        row.recorded_at,
    )


def stored_incident(row: IncidentRow) -> Incident:
    cohort = PaymentCohortV2(
        row.merchant_id,
        row.method,
        CohortDimension(row.issuer, FieldAvailability(row.issuer_availability)),
        CohortDimension(row.provider, FieldAvailability(row.provider_availability)),
        AmountBand(row.amount_band),
        row.cohort_version,
    )
    if cohort.cohort_hash != row.cohort_hash:
        raise ValueError("Stored incident cohort identity is inconsistent")
    return Incident(
        row.id,
        cohort,
        row.detector_version,
        row.opened_scan_id,
        row.opened_at,
        row.opening_window_start,
        DegradationSeverity(row.opening_severity),
        DegradationSeverity(row.peak_severity),
        DetectionOutcome(row.latest_outcome),
        row.last_window_start,
        row.healthy_streak,
        cast("LiteralStatus", row.status),
        row.resolved_at,
    )


LiteralStatus = Literal["open", "resolved"]


class SqlAlchemyIncidentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_merchant(self, merchant_id: str) -> None:
        lock_id = int.from_bytes(
            sha256(("payrecover:incident-scan:" + merchant_id).encode()).digest()[:8],
            "big",
            signed=True,
        )
        self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})

    def find_scan(self, merchant_id: str, detector: str, start: datetime) -> ScanSummary | None:
        row = self._session.scalar(
            select(ScanRunRow).where(
                ScanRunRow.merchant_id == merchant_id,
                ScanRunRow.detector_version == detector,
                ScanRunRow.observation_start == start,
            )
        )
        return stored_scan(row) if row is not None else None

    def latest_scan(self, merchant_id: str, detector: str) -> datetime | None:
        return self._session.scalar(
            select(func.max(ScanRunRow.observation_start)).where(
                ScanRunRow.merchant_id == merchant_id,
                ScanRunRow.detector_version == detector,
            )
        )

    def claim_scan(self, summary: ScanSummary) -> None:
        values = asdict(summary)
        values.update(values.pop("window"))
        self._session.add(ScanRunRow(**values))
        self._session.flush()
        self._session.info.setdefault("incident_scan_claims", set()).add(summary.id)

    def finish_scan(self, summary: ScanSummary) -> None:
        if summary.id not in self._session.info.get("incident_scan_claims", set()):
            raise ValueError("Only a new scan claim can be finalized")
        row = self._session.get(ScanRunRow, summary.id)
        if row is None:
            raise ValueError("Scan claim is missing")
        row.evaluated, row.opened = summary.evaluated, summary.opened
        row.updated, row.resolved = summary.updated, summary.resolved
        self._session.flush()
        self._session.info["incident_scan_claims"].remove(summary.id)

    def open_incidents(self, merchant_id: str, detector: str) -> tuple[Incident, ...]:
        return tuple(
            stored_incident(row)
            for row in self._session.scalars(
                select(IncidentRow)
                .where(
                    IncidentRow.merchant_id == merchant_id,
                    IncidentRow.detector_version == detector,
                    IncidentRow.status == "open",
                )
                .order_by(IncidentRow.id)
            )
        )

    def create_incident(
        self,
        cohort: PaymentCohortV2,
        detection: DegradationDetection,
        scan: ScanSummary,
    ) -> Incident:
        if detection.severity is None or detection.outcome != DetectionOutcome.DEGRADED:
            raise ValueError("Only degraded cohorts open incidents")
        row = IncidentRow(
            merchant_id=cohort.merchant_id,
            cohort_version=cohort.cohort_version,
            cohort_hash=cohort.cohort_hash,
            method=cohort.method,
            issuer=cohort.issuer.value,
            issuer_availability=cohort.issuer.availability.value,
            provider=cohort.provider.value,
            provider_availability=cohort.provider.availability.value,
            amount_band=cohort.amount_band.value,
            detector_version=detection.detector_version,
            opened_scan_id=scan.id,
            opened_at=scan.recorded_at,
            opening_window_start=scan.window.observation_start,
            opening_severity=detection.severity.value,
            peak_severity=detection.severity.value,
            latest_outcome=detection.outcome.value,
            last_window_start=scan.window.observation_start,
            healthy_streak=0,
            status="open",
            resolved_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return stored_incident(row)

    def update_incident(self, incident: Incident) -> None:
        # Refresh even an already-loaded identity, and retain the row lock until UoW completion.
        with self._session.no_autoflush:
            row = self._session.scalar(
                select(IncidentRow)
                .where(
                    IncidentRow.id == incident.id,
                    IncidentRow.merchant_id == incident.cohort.merchant_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if row is None or row.status != "open":
            raise ValueError("Open incident is missing")
        reject_peak_decrease(row.peak_severity, incident.peak_severity)
        for field in (
            "peak_severity",
            "latest_outcome",
            "last_window_start",
            "healthy_streak",
            "status",
            "resolved_at",
        ):
            setattr(row, field, getattr(incident, field))
        self._session.flush()

    def append_observation(
        self,
        incident: Incident,
        scan: ScanSummary,
        evidence: CohortAggregate,
        detection: DegradationDetection,
    ) -> IncidentObservation:
        values = asdict(evidence)
        for key in (
            "cohort",
            "observation_error_code_distribution",
            "dominant_observation_error_code",
        ):
            values.pop(key)
        row = ObservationRow(
            incident_id=incident.id,
            scan_id=scan.id,
            merchant_id=incident.cohort.merchant_id,
            detector_version=incident.detector_version,
            outcome=detection.outcome.value,
            severity=detection.severity.value if detection.severity else None,
            z_score=detection.z_score,
            recorded_at=scan.recorded_at,
            **values,
        )
        self._session.add(row)
        self._session.flush()
        for rank, error in enumerate(evidence.observation_error_code_distribution, 1):
            self._session.add(
                IncidentErrorRow(
                    observation_id=row.id,
                    rank=rank,
                    error_code=error.error_code,
                    failed_count=error.failed_count,
                )
            )
        self._session.flush()
        return IncidentObservation(
            row.id, incident.id, scan.id, scan.window, evidence, detection, scan.recorded_at
        )

    def get_incident(self, merchant_id: str, incident_id: int) -> Incident | None:
        row = self._session.scalar(
            select(IncidentRow).where(
                IncidentRow.id == incident_id,
                IncidentRow.merchant_id == merchant_id,
            )
        )
        return stored_incident(row) if row is not None else None

    def list_incidents(
        self,
        merchant_id: str,
        after: int,
        upper: int | None,
        limit: int,
    ) -> tuple[tuple[Incident, ...], int]:
        if upper is None:
            upper = (
                self._session.scalar(
                    select(func.max(IncidentRow.id)).where(IncidentRow.merchant_id == merchant_id)
                )
                or 0
            )
        rows = self._session.scalars(
            select(IncidentRow)
            .where(
                IncidentRow.merchant_id == merchant_id,
                IncidentRow.id > after,
                IncidentRow.id <= upper,
            )
            .order_by(IncidentRow.id)
            .limit(limit)
        )
        return tuple(stored_incident(row) for row in rows), upper

    def observations(
        self,
        merchant_id: str,
        incident_id: int,
        after: int,
        upper: int | None,
        limit: int,
    ) -> tuple[tuple[IncidentObservation, ...], int]:
        if upper is None:
            upper = (
                self._session.scalar(
                    select(func.max(ObservationRow.id))
                    .join(IncidentRow, ObservationRow.incident_id == IncidentRow.id)
                    .where(
                        IncidentRow.id == incident_id,
                        IncidentRow.merchant_id == merchant_id,
                    )
                )
                or 0
            )
        rows = list(
            self._session.execute(
                select(ObservationRow, ScanRunRow, IncidentRow)
                .join(IncidentRow, ObservationRow.incident_id == IncidentRow.id)
                .join(
                    ScanRunRow,
                    ObservationRow.scan_id == ScanRunRow.id,
                )
                .where(
                    IncidentRow.id == incident_id,
                    IncidentRow.merchant_id == merchant_id,
                    ObservationRow.id > after,
                    ObservationRow.id <= upper,
                )
                .order_by(ObservationRow.id)
                .limit(limit)
            )
        )
        errors: dict[int, list[ErrorCodeCount]] = {}
        if rows:
            for error in self._session.scalars(
                select(IncidentErrorRow)
                .join(ObservationRow, IncidentErrorRow.observation_id == ObservationRow.id)
                .join(IncidentRow, ObservationRow.incident_id == IncidentRow.id)
                .where(
                    IncidentRow.id == incident_id,
                    IncidentRow.merchant_id == merchant_id,
                    IncidentErrorRow.observation_id.in_([row.id for row, _, _ in rows]),
                )
                .order_by(IncidentErrorRow.observation_id, IncidentErrorRow.rank)
            ):
                errors.setdefault(error.observation_id, []).append(
                    ErrorCodeCount(error.error_code, error.failed_count)
                )
        result = []
        for row, scan, incident_row in rows:
            incident = stored_incident(incident_row)
            distribution = tuple(errors.get(row.id, []))
            evidence = CohortAggregate(
                incident.cohort,
                row.baseline_total,
                row.baseline_success_count,
                row.baseline_failed_count,
                row.baseline_success_rate,
                int(Decimal(row.baseline_failed_amount_paise)),
                row.observation_total,
                row.observation_success_count,
                row.observation_failed_count,
                row.observation_success_rate,
                int(Decimal(row.observation_failed_amount_paise)),
                row.absolute_success_rate_drop,
                distribution,
                distribution[0].error_code if distribution else None,
            )
            detection = DegradationDetection(
                incident.detector_version,
                DetectionOutcome(row.outcome),
                DegradationSeverity(row.severity) if row.severity else None,
                row.baseline_success_rate,
                row.observation_success_rate,
                row.absolute_success_rate_drop,
                row.z_score,
            )
            result.append(
                IncidentObservation(
                    row.id,
                    incident.id,
                    row.scan_id,
                    stored_scan(scan).window,
                    evidence,
                    detection,
                    row.recorded_at,
                )
            )
        return tuple(result), upper
