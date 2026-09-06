from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from payrecover.domain.analytics import (
    AnalyticsWindow,
    CohortAggregate,
    DegradationDetection,
    DegradationSeverity,
    DetectionOutcome,
    PaymentCohortV2,
)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC)


def validate_merchant(merchant_id: str) -> None:
    if not 1 <= len(merchant_id) <= 100 or not merchant_id.strip():
        raise ValueError("Merchant ID must contain 1 to 100 nonblank characters")


@dataclass(frozen=True)
class ScanSummary:
    id: UUID
    merchant_id: str
    detector_version: str
    window: AnalyticsWindow
    evaluated: int
    opened: int
    updated: int
    resolved: int
    recorded_at: datetime


@dataclass(frozen=True)
class ScanResult:
    summary: ScanSummary
    replayed: bool


@dataclass(frozen=True)
class Incident:
    id: int
    cohort: PaymentCohortV2
    detector_version: str
    opened_scan_id: UUID
    opened_at: datetime
    opening_window_start: datetime
    opening_severity: DegradationSeverity
    peak_severity: DegradationSeverity
    latest_outcome: DetectionOutcome
    last_window_start: datetime
    healthy_streak: int
    status: Literal["open", "resolved"]
    resolved_at: datetime | None


@dataclass(frozen=True)
class IncidentObservation:
    id: int
    incident_id: int
    scan_id: UUID
    window: AnalyticsWindow
    evidence: CohortAggregate
    detection: DegradationDetection
    recorded_at: datetime


class IncidentRepository(Protocol):
    def lock_merchant(self, merchant_id: str) -> None: ...
    def find_scan(self, merchant_id: str, detector: str, start: datetime) -> ScanSummary | None: ...
    def latest_scan(self, merchant_id: str, detector: str) -> datetime | None: ...
    def claim_scan(self, summary: ScanSummary) -> None: ...
    def finish_scan(self, summary: ScanSummary) -> None: ...
    def open_incidents(self, merchant_id: str, detector: str) -> tuple[Incident, ...]: ...
    def create_incident(
        self, cohort: PaymentCohortV2, detection: DegradationDetection, scan: ScanSummary
    ) -> Incident: ...
    def update_incident(self, incident: Incident) -> None: ...
    def append_observation(
        self,
        incident: Incident,
        scan: ScanSummary,
        evidence: CohortAggregate,
        detection: DegradationDetection,
    ) -> IncidentObservation: ...
    def get_incident(self, merchant_id: str, incident_id: int) -> Incident | None: ...
    def list_incidents(
        self,
        merchant_id: str,
        after: int,
        upper: int | None,
        limit: int,
    ) -> tuple[tuple[Incident, ...], int]: ...
    def observations(
        self,
        merchant_id: str,
        incident_id: int,
        after: int,
        upper: int | None,
        limit: int,
    ) -> tuple[tuple[IncidentObservation, ...], int]: ...


class IncidentAuditDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    scan_id: UUID
    observation_id: int = Field(gt=0)
    cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detector_version: str = Field(min_length=1, max_length=64)
    observation_start: datetime
    observation_end: datetime

    @field_validator("observation_start", "observation_end")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return utc(value)


class IncidentOpenedAudit(IncidentAuditDetails):
    opening_severity: Literal["medium", "high", "critical"]


class IncidentUpdatedAudit(IncidentAuditDetails):
    outcome: Literal["degraded", "healthy", "watch", "insufficient"]
    healthy_streak: int = Field(ge=0, le=2)


class IncidentResolvedAudit(IncidentAuditDetails):
    reason: Literal["two_adjacent_healthy_windows"]


INCIDENT_AUDIT_SCHEMAS: dict[str, type[IncidentAuditDetails]] = {
    "incident.opened": IncidentOpenedAudit,
    "incident.observation_updated": IncidentUpdatedAudit,
    "incident.resolved": IncidentResolvedAudit,
}
