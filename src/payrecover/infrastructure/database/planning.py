from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, and_, distinct, func, select
from sqlalchemy.orm import Session

from payrecover.domain.analytics import AMOUNT_BAND_UPPER_BOUNDS_PAISE
from payrecover.domain.incidents import IncidentObservation
from payrecover.domain.planning import (
    DIAGNOSIS_VERSION,
    POLICY_VERSION,
    FailedPaymentCandidate,
    IncidentDiagnosis,
    PlanEvidence,
    PlanningConflict,
    PlanningNotFound,
    RecoveryRecommendation,
    Signal,
)
from payrecover.infrastructure.database.incident_models import (
    IncidentRow,
    ObservationRow,
    ScanRunRow,
)
from payrecover.infrastructure.database.incidents import SqlAlchemyIncidentRepository
from payrecover.infrastructure.database.models import PaymentEventRow
from payrecover.infrastructure.database.planning_models import DiagnosisRow, PlanRow

REVIEWED = ("network_timeout", "issuer_unavailable", "insufficient_funds", "incorrect_pin")


def record[T: BaseModel](model: type[T], row: Any) -> T:
    return model.model_validate(
        {key: getattr(row, key) for key in model.model_fields}, strict=False
    )


def candidate(row: PaymentEventRow) -> FailedPaymentCandidate:
    signal: Signal = "unknown"
    if (
        row.source == "normalized_api"
        and row.error_code_availability == "provided"
        and row.error_code in REVIEWED
    ):
        signal = cast(Signal, row.error_code)
    return FailedPaymentCandidate(
        row.id, row.source, row.payment_id, signal, row.amount_paise, row.occurred_at
    )


class SqlAlchemyPlanningRepository:
    def __init__(self, session: Session):
        self.session = session

    def observation(self, merchant: str, incident: int, observation: int) -> IncidentObservation:
        rows, _ = SqlAlchemyIncidentRepository(self.session).observations(
            merchant, incident, observation - 1, observation, 1
        )
        if not rows:
            raise PlanningNotFound("Planning evidence not found")
        return rows[0]

    def diagnosis(self, merchant: str, observation: int) -> IncidentDiagnosis | None:
        row = self.session.scalar(
            select(DiagnosisRow).where(
                DiagnosisRow.merchant_id == merchant,
                DiagnosisRow.observation_id == observation,
                DiagnosisRow.diagnosis_version == DIAGNOSIS_VERSION,
            )
        )
        return record(IncidentDiagnosis, row) if row else None

    def add_diagnosis(self, value: IncidentDiagnosis) -> None:
        self.observation(value.merchant_id, value.incident_id, value.observation_id)
        values = value.model_dump()
        values["evidence"] = value.evidence.model_dump(mode="json")
        self.session.add(DiagnosisRow(**values))
        self.session.flush()

    def _candidates(self, merchant: str, incident: int, observation: int) -> Select[Any]:
        obs = self.observation(merchant, incident, observation)
        band = int(obs.evidence.cohort.amount_band.value[-1])
        lower = 1 if band == 0 else AMOUNT_BAND_UPPER_BOUNDS_PAISE[band - 1]
        predicates = [PaymentEventRow.amount_paise >= lower]
        if band < len(AMOUNT_BAND_UPPER_BOUNDS_PAISE):
            predicates.append(PaymentEventRow.amount_paise < AMOUNT_BAND_UPPER_BOUNDS_PAISE[band])
        return (
            select(PaymentEventRow)
            .join(IncidentRow, IncidentRow.merchant_id == PaymentEventRow.merchant_id)
            .join(
                ObservationRow,
                and_(
                    ObservationRow.incident_id == IncidentRow.id,
                    ObservationRow.merchant_id == IncidentRow.merchant_id,
                ),
            )
            .join(ScanRunRow, ObservationRow.scan_id == ScanRunRow.id)
            .where(
                IncidentRow.merchant_id == merchant,
                IncidentRow.id == incident,
                ObservationRow.id == observation,
                PaymentEventRow.status == "failed",
                PaymentEventRow.method == IncidentRow.method,
                PaymentEventRow.issuer.is_not_distinct_from(IncidentRow.issuer),
                PaymentEventRow.provider.is_not_distinct_from(IncidentRow.provider),
                PaymentEventRow.issuer_availability == IncidentRow.issuer_availability,
                PaymentEventRow.provider_availability == IncidentRow.provider_availability,
                PaymentEventRow.occurred_at >= ScanRunRow.observation_start,
                PaymentEventRow.occurred_at < ScanRunRow.observation_end,
                *predicates,
            )
        )

    def candidate(
        self, merchant: str, incident: int, observation: int, event: UUID
    ) -> FailedPaymentCandidate:
        row = self.session.scalar(
            self._candidates(merchant, incident, observation).where(PaymentEventRow.id == event)
        )
        if row is None:
            raise PlanningNotFound("Associated failed event not found")
        return candidate(row)

    def candidates(
        self, merchant: str, incident: int, observation: int, after: UUID | None, limit: int
    ) -> tuple[FailedPaymentCandidate, ...]:
        if not 1 <= limit <= 101:
            raise ValueError("Invalid candidate limit")
        statement = self._candidates(merchant, incident, observation)
        if after is not None:
            statement = statement.where(PaymentEventRow.id > after)
        return tuple(
            candidate(row)
            for row in self.session.scalars(statement.order_by(PaymentEventRow.id).limit(limit))
        )

    def history(self, merchant: str, event: FailedPaymentCandidate) -> PlanEvidence:
        p = PaymentEventRow
        persisted = self.session.scalar(
            select(p).where(p.id == event.id, p.merchant_id == merchant)
            .execution_options(populate_existing=True)
        )
        if persisted is None:
            raise PlanningNotFound("Payment history subject not found")
        if (event.source, event.payment_id) != (persisted.source, persisted.payment_id):
            raise PlanningConflict("Conflicting payment history identity")
        individual_signal = candidate(persisted).signal
        row = self.session.execute(
            select(
                func.bool_or(p.status == "success"),
                func.bool_or(p.status == "failed"),
                func.count(
                    distinct(
                        func.row(
                            p.method,
                            p.issuer,
                            p.issuer_availability,
                            p.provider,
                            p.provider_availability,
                            p.amount_paise,
                        )
                    )
                ),
                func.count(distinct(p.error_code)).filter(
                    and_(
                        p.status == "failed",
                        p.source == "normalized_api",
                        p.error_code_availability == "provided",
                        p.error_code.in_(REVIEWED),
                    )
                ),
            ).where(
                p.merchant_id == persisted.merchant_id,
                p.source == persisted.source,
                p.payment_id == persisted.payment_id,
            )
        ).one()
        return PlanEvidence(
            individual_signal=individual_signal,
            stored_success=bool(row[0]),
            mixed_statuses=bool(row[0] and row[1]),
            inconsistent_dimensions_or_amount=row[2] > 1,
            conflicting_reviewed_signals=row[3] > 1,
        )

    def existing_plan(
        self, merchant: str, event: FailedPaymentCandidate
    ) -> RecoveryRecommendation | None:
        row = self.session.scalar(
            select(PlanRow).where(
                PlanRow.merchant_id == merchant,
                PlanRow.source == event.source,
                PlanRow.payment_id == event.payment_id,
                PlanRow.policy_version == POLICY_VERSION,
            )
        )
        return record(RecoveryRecommendation, row) if row else None

    def get_plan(self, merchant: str, plan: UUID) -> RecoveryRecommendation:
        row = self.session.scalar(
            select(PlanRow).where(PlanRow.merchant_id == merchant, PlanRow.id == plan)
        )
        if row is None:
            raise PlanningNotFound("Plan not found")
        return record(RecoveryRecommendation, row)

    def add_plan(self, value: RecoveryRecommendation, event: FailedPaymentCandidate) -> None:
        authoritative = self.candidate(
            value.merchant_id, value.incident_id, value.observation_id, value.payment_event_id
        )
        if authoritative != event:
            raise PlanningConflict("Conflicting event association")
        values = value.model_dump()
        values.update(
            source=event.source,
            payment_id=event.payment_id,
            evidence=value.evidence.model_dump(mode="json"),
            prerequisites=value.prerequisites.model_dump(mode="json"),
        )
        self.session.add(PlanRow(**values))
        self.session.flush()
