from collections.abc import Callable

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

SessionFactory = sessionmaker[Session]


def build_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, pool_pre_ping=True)

    @event.listens_for(engine, "connect")
    def set_utc_timezone(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            cursor.close()

    return engine


def build_session_factory(
    database_url: str | None = None, *, engine: Engine | None = None
) -> SessionFactory:
    if engine is None:
        if database_url is None:
            raise ValueError("database_url is required when engine is not supplied")
        engine = build_engine(database_url)
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_factory_builder(database_url: str) -> Callable[[], Session]:
    return build_session_factory(database_url)
