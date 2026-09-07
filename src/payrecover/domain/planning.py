"""Immutable planning contracts. No record authorizes an external action."""

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from payrecover.domain.incidents import IncidentObservation, utc
from payrecover.domain.models import RecoveryAction

DIAGNOSIS_VERSION: Literal["diagnosis-v1"] = "diagnosis-v1"
POLICY_VERSION: Literal["recovery-planning-v1"] = "recovery-planning-v1"
Signal = Literal[
    "network_timeout", "issuer_unavailable", "insufficient_funds", "incorrect_pin", "unknown"
]
DiagnosisOutcome = Literal[
    "probable_transient_network_failure",
    "probable_issuer_unavailability",
    "probable_customer_funding_issue",
    "probable_authentication_failure",
    "unknown",
    "insufficient_evidence",
]


class FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @field_validator("created_at", "association_evaluated_at", check_fields=False)
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return utc(value)


class DiagnosisEvidence(FrozenRecord):
    detector_outcome: Literal["degraded", "healthy", "watch", "insufficient"]
    supporting_signal: Signal
    retained_failed_count: int = Field(ge=0)
    unrepresented_failed_count: int = Field(ge=0)
    limitation: Literal["reported_labels_do_not_prove_causation"] = (
        "reported_labels_do_not_prove_causation"
    )


class IncidentDiagnosis(FrozenRecord):
    id: UUID
    merchant_id: str
    incident_id: int = Field(gt=0)
    observation_id: int = Field(gt=0)
    diagnosis_version: Literal["diagnosis-v1"] = DIAGNOSIS_VERSION
    outcome: DiagnosisOutcome
    reason_code: Literal["strict_majority", "mixed_or_unmapped_failures", "insufficient_failures"]
    supporting_failed_count: int = Field(ge=0)
    total_failed_count: int = Field(ge=0)
    evidence: DiagnosisEvidence
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def consistent_counts(self) -> "IncidentDiagnosis":
        if not 0 <= self.supporting_failed_count <= self.total_failed_count:
            raise ValueError("Invalid diagnosis counts")
        if (
            self.evidence.retained_failed_count + self.evidence.unrepresented_failed_count
            != self.total_failed_count
        ):
            raise ValueError("Invalid evidence coverage")
        return self


class Prerequisites(FrozenRecord):
    consent: Literal["missing"] = "missing"
    provider_flow: Literal["unverified"] = "unverified"
    payment_status: Literal["unreconciled"] = "unreconciled"
    attempt_history: Literal["unverified"] = "unverified"
    integrity_blocker: bool


class PlanEvidence(FrozenRecord):
    individual_signal: Signal
    stored_success: bool
    mixed_statuses: bool
    inconsistent_dimensions_or_amount: bool
    conflicting_reviewed_signals: bool
    association: Literal["cohort_window_at_planning_time"] = "cohort_window_at_planning_time"


class PolicyRecommendation(FrozenRecord):
    action: RecoveryAction
    decision: Literal["blocked", "manual_review", "no_action"]
    reason_code: Literal[
        "stored_success",
        "contradictory_history",
        "unknown_signal",
        "authentication_failure",
        "customer_funding_action",
        "transient_network",
        "issuer_health_wait",
    ]
    retry_after_seconds: int | None = Field(default=None, ge=0)
    max_attempts: int = Field(ge=0, le=3)
    execution_authorized: Literal[False] = False
    prerequisites: Prerequisites


class RecoveryRecommendation(PolicyRecommendation):
    id: UUID
    merchant_id: str
    incident_id: int = Field(gt=0)
    observation_id: int = Field(gt=0)
    diagnosis_id: UUID
    payment_event_id: UUID
    policy_version: Literal["recovery-planning-v1"] = POLICY_VERSION
    evidence: PlanEvidence
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    association_evaluated_at: datetime
    created_at: datetime


class PlanningResult(FrozenRecord):
    recommendation: RecoveryRecommendation
    replayed: bool


class DiagnosisResult(FrozenRecord):
    diagnosis: IncidentDiagnosis
    replayed: bool


@dataclass(frozen=True)
class FailedPaymentCandidate:
    id: UUID
    source: str
    payment_id: str
    signal: Signal
    amount_paise: int
    occurred_at: datetime


class Candidate(FrozenRecord):
    payment_event_id: UUID
    amount_paise: int = Field(gt=0)
    occurred_at: datetime
    association_evaluated_at: datetime


class CandidatePage(FrozenRecord):
    items: tuple[Candidate, ...]
    next_cursor: str | None


class PlanningAudit(FrozenRecord):
    version: Literal[1] = 1
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rule_version: Literal["diagnosis-v1", "recovery-planning-v1"]


def digest(value: BaseModel) -> str:
    return sha256(
        json.dumps(
            value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class PlanningNotFound(LookupError):
    pass


class PlanningConflict(ValueError):
    pass


class PlanningUnavailable(RuntimeError):
    pass


class PlanningRepository(Protocol):
    def observation(
        self, merchant: str, incident: int, observation: int
    ) -> IncidentObservation: ...
    def diagnosis(self, merchant: str, observation: int) -> IncidentDiagnosis | None: ...
    def add_diagnosis(self, value: IncidentDiagnosis) -> None: ...
    def candidate(
        self, merchant: str, incident: int, observation: int, event: UUID
    ) -> FailedPaymentCandidate: ...
    def candidates(
        self, merchant: str, incident: int, observation: int, after: UUID | None, limit: int
    ) -> tuple[FailedPaymentCandidate, ...]: ...
    def history(self, merchant: str, event: FailedPaymentCandidate) -> PlanEvidence: ...
    def existing_plan(
        self, merchant: str, event: FailedPaymentCandidate
    ) -> RecoveryRecommendation | None: ...
    def get_plan(self, merchant: str, plan: UUID) -> RecoveryRecommendation: ...
    def add_plan(self, value: RecoveryRecommendation, event: FailedPaymentCandidate) -> None: ...
