from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from payrecover.domain.incidents import INCIDENT_AUDIT_SCHEMAS
from payrecover.domain.planning import PlanningAudit
from payrecover.domain.records import (
    NewAuditRecord,
    NewPaymentEvent,
    SourceIdentityScope,
    StoredAuditRecord,
    StoredPaymentEvent,
)
from payrecover.infrastructure.database.models import AuditRecordRow, PaymentEventRow


def _stored_payment_event(row: PaymentEventRow) -> StoredPaymentEvent:
    return StoredPaymentEvent(
        id=row.id,
        schema_version=row.schema_version,
        source=row.source,
        source_event_id=row.source_event_id,
        payload_sha256=row.payload_sha256,
        payment_id=row.payment_id,
        merchant_id=row.merchant_id,
        method=row.method,
        issuer=row.issuer,
        issuer_availability=row.issuer_availability,
        provider=row.provider,
        provider_availability=row.provider_availability,
        amount_paise=row.amount_paise,
        status=row.status,
        error_code=row.error_code,
        error_code_availability=row.error_code_availability,
        error_source=row.error_source,
        error_step=row.error_step,
        error_reason=row.error_reason,
        latency_ms=row.latency_ms,
        latency_availability=row.latency_availability,
        cohort_key=row.cohort_key,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
    )


def _stored_audit_record(row: AuditRecordRow) -> StoredAuditRecord:
    return StoredAuditRecord(
        id=row.id,
        diagnosis_id=row.diagnosis_id,
        recovery_plan_id=row.recovery_plan_id,
        incident_id=row.incident_id,
        correlation_id=row.correlation_id,
        payment_event_id=row.payment_event_id,
        event_type=row.event_type,
        actor_type=row.actor_type,
        actor_ref_digest=row.actor_ref_digest,
        details=row.details,
        recorded_at=row.recorded_at,
    )


class SqlAlchemyPaymentEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_if_absent(
        self,
        event: NewPaymentEvent,
        *,
        identity_scope: SourceIdentityScope = SourceIdentityScope.MERCHANT,
    ) -> tuple[StoredPaymentEvent, bool]:
        statement = (
            insert(PaymentEventRow)
            .values(**asdict(event))
            .on_conflict_do_nothing()
            .returning(PaymentEventRow)
        )
        inserted = self._session.execute(statement).scalar_one_or_none()
        if inserted is not None:
            return _stored_payment_event(inserted), True

        identity_conditions = [
            PaymentEventRow.source == event.source,
            PaymentEventRow.source_event_id == event.source_event_id,
        ]
        if identity_scope == SourceIdentityScope.MERCHANT:
            identity_conditions.append(PaymentEventRow.merchant_id == event.merchant_id)
        existing = self._session.execute(
            select(PaymentEventRow).where(*identity_conditions)
        ).scalar_one()
        return _stored_payment_event(existing), False

    def get_by_source_identity(
        self, source: str, merchant_id: str, source_event_id: str
    ) -> StoredPaymentEvent | None:
        row = self._session.execute(
            select(PaymentEventRow).where(
                PaymentEventRow.source == source,
                PaymentEventRow.merchant_id == merchant_id,
                PaymentEventRow.source_event_id == source_event_id,
            )
        ).scalar_one_or_none()
        return _stored_payment_event(row) if row is not None else None


class SqlAlchemyAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, record: NewAuditRecord) -> StoredAuditRecord:
        details = record.details
        if record.event_type in {"incident.diagnosed", "recovery_plan.created"}:
            if (record.incident_id is None or record.diagnosis_id is None
                    or record.payment_event_id is not None
                    or (record.recovery_plan_id is not None)
                    != (record.event_type == "recovery_plan.created")):
                raise ValueError("Invalid planning audit subject")
            details = PlanningAudit.model_validate(details).model_dump(mode="json")
        elif record.diagnosis_id is not None or record.recovery_plan_id is not None:
            raise ValueError("Unexpected planning audit subject")
        elif record.event_type in INCIDENT_AUDIT_SCHEMAS:
            if record.incident_id is None or record.payment_event_id is not None:
                raise ValueError("Incident audit requires only an incident subject")
            details = INCIDENT_AUDIT_SCHEMAS[record.event_type].model_validate(
                details
            ).model_dump(mode="json")
        elif record.incident_id is not None:
            raise ValueError("Incident audit event type is not allowed")
        row = AuditRecordRow(
            diagnosis_id=record.diagnosis_id,
            recovery_plan_id=record.recovery_plan_id,
            incident_id=record.incident_id,
            correlation_id=record.correlation_id,
            payment_event_id=record.payment_event_id,
            event_type=record.event_type,
            actor_type=record.actor_type,
            actor_ref_digest=record.actor_ref_digest,
            details=details,
        )
        self._session.add(row)
        self._session.flush()
        return _stored_audit_record(row)

    def list_for_payment_event(self, payment_event_id: UUID) -> list[StoredAuditRecord]:
        rows = self._session.execute(
            select(AuditRecordRow)
            .where(AuditRecordRow.payment_event_id == payment_event_id)
            .order_by(AuditRecordRow.id)
        ).scalars()
        return [_stored_audit_record(row) for row in rows]
