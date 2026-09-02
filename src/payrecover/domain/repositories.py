from typing import Protocol
from uuid import UUID

from payrecover.domain.records import (
    NewAuditRecord,
    NewPaymentEvent,
    SourceIdentityScope,
    StoredAuditRecord,
    StoredPaymentEvent,
)


class PaymentEventRepository(Protocol):
    def add_if_absent(
        self,
        event: NewPaymentEvent,
        *,
        identity_scope: SourceIdentityScope = SourceIdentityScope.MERCHANT,
    ) -> tuple[StoredPaymentEvent, bool]: ...

    def get_by_source_identity(
        self, source: str, merchant_id: str, source_event_id: str
    ) -> StoredPaymentEvent | None: ...


class AuditRepository(Protocol):
    def append(self, record: NewAuditRecord) -> StoredAuditRecord: ...

    def list_for_payment_event(self, payment_event_id: UUID) -> list[StoredAuditRecord]: ...


class UnitOfWork(Protocol):
    @property
    def payment_events(self) -> PaymentEventRepository: ...

    @property
    def audit_records(self) -> AuditRepository: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
