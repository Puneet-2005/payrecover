from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, inspect, make_url
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from payrecover.infrastructure.database.session import build_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_TABLES = {"alembic_version", "audit_records", "payment_events", "incident_scan_runs",
                   "incidents", "incident_observations", "incident_error_counts",
                   "incident_diagnoses", "recovery_plans"}


def alembic_config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    assert config.get_main_option("sqlalchemy.url") == database_url
    return config


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    try:
        container = PostgresContainer("postgres:17-alpine", driver="psycopg")
        container.start()
    except Exception as exc:
        reason = (
            "PostgreSQL integration tests cannot start because Docker or the PostgreSQL 17 "
            f"image is unavailable: {type(exc).__name__}: {exc}"
        )
        if os.getenv("PAYRECOVER_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail(reason)
        pytest.skip(
            reason.replace("cannot start", "skipped")
        )
    try:
        yield container.get_connection_url(driver="psycopg")
    finally:
        container.stop()


@pytest.fixture(scope="session")
def migrated_database_url(postgres_url: str) -> str:
    migration_config = alembic_config(postgres_url)
    command.upgrade(migration_config, "head")

    verification_engine = build_engine(postgres_url)
    try:
        assert verification_engine.url == make_url(postgres_url)
        migrated_tables = set(inspect(verification_engine).get_table_names())
    finally:
        verification_engine.dispose()
    assert REQUIRED_TABLES.issubset(migrated_tables), (
        f"Alembic did not commit the expected schema; found tables: {sorted(migrated_tables)}"
    )
    return postgres_url


@pytest.fixture(scope="session")
def database_engine(migrated_database_url: str) -> Iterator[Engine]:
    engine = build_engine(migrated_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def clean_database(database_engine: Engine) -> Iterator[Engine]:
    with database_engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE TABLE audit_records, payment_events, incident_error_counts, "
            "incident_observations, incidents, incident_scan_runs RESTART IDENTITY CASCADE"
        )
    yield database_engine
    with database_engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE TABLE audit_records, payment_events, incident_error_counts, "
            "incident_observations, incidents, incident_scan_runs RESTART IDENTITY CASCADE"
        )
