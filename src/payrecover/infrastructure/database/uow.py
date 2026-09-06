from types import TracebackType

from sqlalchemy.orm import Session

from payrecover.infrastructure.database.analytics import (
    SqlAlchemyPaymentAnalyticsRepository,
)
from payrecover.infrastructure.database.incidents import SqlAlchemyIncidentRepository
from payrecover.infrastructure.database.repositories import (
    SqlAlchemyAuditRepository,
    SqlAlchemyPaymentEventRepository,
)
from payrecover.infrastructure.database.session import SessionFactory


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None
        self.payment_events: SqlAlchemyPaymentEventRepository
        self.audit_records: SqlAlchemyAuditRepository
        self.payment_analytics: SqlAlchemyPaymentAnalyticsRepository
        self.incidents: SqlAlchemyIncidentRepository
        self._committed = False

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self.session = self._session_factory()
        self.payment_events = SqlAlchemyPaymentEventRepository(self.session)
        self.audit_records = SqlAlchemyAuditRepository(self.session)
        self.payment_analytics = SqlAlchemyPaymentAnalyticsRepository(self.session)
        self.incidents = SqlAlchemyIncidentRepository(self.session)
        self._committed = False
        return self

    def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("Unit of work has not been entered")
        self.session.commit()
        self._committed = True

    def rollback(self) -> None:
        if self.session is not None:
            self.session.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        if self.session is None:
            return
        if exc_type is not None or not self._committed:
            self.session.rollback()
        self.session.close()
        self.session = None
