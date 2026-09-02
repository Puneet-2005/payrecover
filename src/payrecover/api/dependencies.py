from collections.abc import Callable
from functools import lru_cache
from types import TracebackType
from typing import Protocol

from fastapi import HTTPException, status
from pydantic import ValidationError

from payrecover.config import Settings, load_settings
from payrecover.domain.repositories import UnitOfWork
from payrecover.infrastructure.database.session import SessionFactory, build_session_factory
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork


class UnitOfWorkContext(Protocol):
    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWorkContext]


def get_settings() -> Settings:
    try:
        return load_settings()
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Persistence configuration is unavailable",
        ) from exc


@lru_cache(maxsize=4)
def get_session_factory(database_url: str) -> SessionFactory:
    return build_session_factory(database_url)


def get_unit_of_work_factory() -> UnitOfWorkFactory:
    def factory() -> UnitOfWorkContext:
        settings = get_settings()
        if not settings.persistence_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Persistence is disabled",
            )
        session_factory = get_session_factory(settings.require_database_url())
        return SqlAlchemyUnitOfWork(session_factory)

    return factory
