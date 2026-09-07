import base64
import json
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from payrecover.domain.incidents import utc, validate_merchant
from payrecover.domain.planning import (
    Candidate,
    CandidatePage,
    DiagnosisResult,
    FrozenRecord,
    IncidentDiagnosis,
    PlanningAudit,
    PlanningConflict,
    PlanningNotFound,
    PlanningResult,
    PlanningUnavailable,
    RecoveryRecommendation,
    digest,
)
from payrecover.domain.records import NewAuditRecord
from payrecover.domain.repositories import UnitOfWork
from payrecover.services.diagnosis import diagnose
from payrecover.services.policy import decide_recovery_plan


def _diagnose(
    uow: UnitOfWork, merchant: str, incident: int, observation: int, now: datetime
) -> DiagnosisResult:
    evidence = uow.planning.observation(merchant, incident, observation)
    existing = uow.planning.diagnosis(merchant, observation)
    if existing is not None:
        return DiagnosisResult(diagnosis=existing, replayed=True)
    value = diagnose(evidence, now)
    uow.planning.add_diagnosis(value)
    _audit(uow, value)
    return DiagnosisResult(diagnosis=value, replayed=False)


def _audit(uow: UnitOfWork, value: IncidentDiagnosis | RecoveryRecommendation) -> None:
    is_plan = isinstance(value, RecoveryRecommendation)
    uow.audit_records.append(
        NewAuditRecord(
            correlation_id=value.id,
            payment_event_id=None,
            incident_id=value.incident_id,
            diagnosis_id=value.diagnosis_id
            if isinstance(value, RecoveryRecommendation)
            else value.id,
            recovery_plan_id=value.id if is_plan else None,
            event_type="recovery_plan.created" if is_plan else "incident.diagnosed",
            actor_type="local_planner",
            actor_ref_digest=None,
            details=PlanningAudit(
                evidence_sha256=value.evidence_sha256,
                rule_version=value.policy_version
                if isinstance(value, RecoveryRecommendation)
                else value.diagnosis_version,
            ).model_dump(),
        )
    )


def diagnose_incident(
    uow: UnitOfWork, merchant: str, incident: int, observation: int, *, now: datetime
) -> DiagnosisResult:
    validate_merchant(merchant)
    now = utc(now)
    try:
        uow.incidents.lock_merchant(merchant)
        result = _diagnose(uow, merchant, incident, observation, now)
        uow.commit()
        return result
    except (PlanningNotFound, PlanningConflict):
        uow.rollback()
        raise
    except Exception as exc:
        uow.rollback()
        raise PlanningUnavailable("Diagnosis unavailable") from exc


def plan_recovery(
    uow: UnitOfWork,
    merchant: str,
    incident: int,
    observation: int,
    event_id: UUID,
    *,
    now: datetime,
) -> PlanningResult:
    validate_merchant(merchant)
    now = utc(now)
    try:
        uow.incidents.lock_merchant(merchant)
        event = uow.planning.candidate(merchant, incident, observation, event_id)
        existing = uow.planning.existing_plan(merchant, event)
        if existing is not None:
            if (existing.incident_id, existing.observation_id, existing.payment_event_id) != (
                incident,
                observation,
                event_id,
            ):
                raise PlanningConflict("Payment/policy identity has a different association")
            uow.commit()
            return PlanningResult(recommendation=existing, replayed=True)
        diagnosis = _diagnose(uow, merchant, incident, observation, now).diagnosis
        evidence = uow.planning.history(merchant, event)
        decision = decide_recovery_plan(evidence)
        value = RecoveryRecommendation(
            **decision.model_dump(),
            id=uuid4(),
            merchant_id=merchant,
            incident_id=incident,
            observation_id=observation,
            diagnosis_id=diagnosis.id,
            payment_event_id=event_id,
            evidence=evidence,
            evidence_sha256=digest(evidence),
            association_evaluated_at=now,
            created_at=now,
        )
        uow.planning.add_plan(value, event)
        _audit(uow, value)
        uow.commit()
        return PlanningResult(recommendation=value, replayed=False)
    except (PlanningNotFound, PlanningConflict):
        uow.rollback()
        raise
    except Exception as exc:
        uow.rollback()
        raise PlanningUnavailable("Planning unavailable") from exc


class CandidateCursor(FrozenRecord):
    version: int = Field(default=1, ge=1, le=1)
    merchant: str
    incident: int = Field(gt=0)
    observation: int = Field(gt=0)
    after: UUID

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def list_candidates(
    uow: UnitOfWork,
    merchant: str,
    incident: int,
    observation: int,
    limit: int,
    cursor: str | None,
    *,
    now: datetime,
) -> CandidatePage:
    validate_merchant(merchant)
    now = utc(now)
    if not 1 <= limit <= 100:
        raise ValueError("Limit must be 1 to 100")
    after = None
    if cursor is not None:
        try:
            if len(cursor) > 1024:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            # JSON validation accepts UUID strings but retains strict numeric typing.
            parsed = CandidateCursor.model_validate_json(raw)
            if parsed.encode() != cursor or (
                parsed.merchant,
                parsed.incident,
                parsed.observation,
            ) != (merchant, incident, observation):
                raise ValueError
            after = parsed.after
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid candidate cursor") from exc
    rows = uow.planning.candidates(merchant, incident, observation, after, limit + 1)
    continuation = (
        CandidateCursor(
            merchant=merchant, incident=incident, observation=observation, after=rows[limit - 1].id
        ).encode()
        if len(rows) > limit
        else None
    )
    return CandidatePage(
        items=tuple(
            Candidate(
                payment_event_id=row.id,
                amount_paise=row.amount_paise,
                occurred_at=row.occurred_at,
                association_evaluated_at=now,
            )
            for row in rows[:limit]
        ),
        next_cursor=continuation,
    )
