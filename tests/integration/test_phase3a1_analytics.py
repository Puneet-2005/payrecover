from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from alembic import command
from payrecover.domain.analytics import AmountBand, AnalyticsWindow
from payrecover.infrastructure.database.analytics import (
    SqlAlchemyPaymentAnalyticsRepository,
)
from payrecover.infrastructure.database.models import PaymentEventRow
from payrecover.infrastructure.database.session import build_engine

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MERCHANT_ID = "acc_AnalyticsMerchant"
OBSERVATION_START = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
WINDOW = AnalyticsWindow(
    baseline_start=OBSERVATION_START - timedelta(days=7),
    baseline_end=OBSERVATION_START,
    observation_start=OBSERVATION_START,
    observation_end=OBSERVATION_START + timedelta(minutes=15),
)


def add_event(
    session: Session,
    token: str,
    occurred_at: datetime,
    *,
    merchant_id: str = MERCHANT_ID,
    method: str = "card",
    issuer: str | None = "HDFC",
    issuer_availability: str = "provided",
    provider: str | None = "Visa",
    provider_availability: str = "provided",
    amount_paise: int = 100_000,
    status: str = "success",
    error_code: str | None = None,
    cohort_key: str | None = None,
) -> None:
    session.add(
        PaymentEventRow(
            schema_version=2,
            source="analytics_test",
            source_event_id=f"evt_{token}",
            payload_sha256="a" * 64,
            merchant_id=merchant_id,
            payment_id=f"pay_{token}",
            method=method,
            issuer=issuer,
            issuer_availability=issuer_availability,
            provider=provider,
            provider_availability=provider_availability,
            amount_paise=amount_paise,
            status=status,
            error_code=error_code,
            error_code_availability=(
                "provided"
                if error_code is not None
                else "missing"
                if status == "failed"
                else "not_applicable"
            ),
            error_source=None,
            error_step=None,
            error_reason=None,
            latency_ms=None,
            latency_availability="missing",
            cohort_key=cohort_key,
            occurred_at=occurred_at,
        )
    )


def aggregate(database_engine, merchant_id: str = MERCHANT_ID):
    with Session(database_engine) as session:
        return SqlAlchemyPaymentAnalyticsRepository(session).aggregate_for_merchant(
            merchant_id, WINDOW
        )


def test_phase3a1_migration_upgrade_downgrade_upgrade_cycle(
    clean_database, migrated_database_url: str
):
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    cycle_engine = build_engine(migrated_database_url)
    index_name = "ix_payment_events_merchant_occurred_analytics"
    try:
        indexes = {
            item["name"]: item
            for item in inspect(cycle_engine).get_indexes("payment_events")
        }
        assert index_name in indexes
        assert indexes[index_name]["column_names"] == ["merchant_id", "occurred_at"]
        with cycle_engine.connect() as connection:
            definition = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND indexname = :index_name"
                ),
                {"index_name": index_name},
            ).scalar_one()
        assert (
            "INCLUDE (method, issuer, issuer_availability, provider, "
            "provider_availability, amount_paise, status, error_code)" in definition
        )

        command.downgrade(config, "-1")
        assert index_name not in {
            item["name"] for item in inspect(cycle_engine).get_indexes("payment_events")
        }
        command.upgrade(config, "head")
        assert index_name in {
            item["name"] for item in inspect(cycle_engine).get_indexes("payment_events")
        }
    finally:
        command.upgrade(config, "head")
        cycle_engine.dispose()


def test_aggregation_uses_half_open_windows_and_includes_one_sided_cohorts(
    clean_database,
):
    with Session(clean_database) as session:
        add_event(session, "before", WINDOW.baseline_start - timedelta(microseconds=1))
        add_event(session, "baseline_start", WINDOW.baseline_start)
        add_event(
            session,
            "baseline_failed",
            WINDOW.baseline_end - timedelta(microseconds=1),
            status="failed",
            error_code="BASELINE_ONLY_ERROR",
            amount_paise=150_000,
        )
        add_event(session, "observation_start", WINDOW.observation_start)
        add_event(
            session,
            "observation_failed",
            WINDOW.observation_end - timedelta(microseconds=1),
            status="failed",
            error_code="OBSERVATION_ERROR",
            amount_paise=150_000,
        )
        add_event(session, "observation_end", WINDOW.observation_end)
        add_event(
            session,
            "baseline_only",
            WINDOW.baseline_start,
            method="netbanking",
            provider=None,
            provider_availability="not_applicable",
        )
        add_event(
            session,
            "observation_only",
            WINDOW.observation_start,
            method="wallet",
            issuer=None,
            issuer_availability="not_applicable",
            provider="payzapp",
        )
        add_event(
            session,
            "other_merchant",
            WINDOW.observation_start,
            merchant_id="acc_OtherMerchant",
        )
        session.commit()

    values = aggregate(clean_database)
    assert len(values) == 3
    card = next(value for value in values if value.cohort.method == "card")
    assert card.baseline_total == 2
    assert card.baseline_success_count == 1
    assert card.baseline_failed_count == 1
    assert card.baseline_failed_amount_paise == 150_000
    assert card.observation_total == 2
    assert card.observation_success_count == 1
    assert card.observation_failed_count == 1
    assert card.observation_failed_amount_paise == 150_000
    assert card.dominant_observation_error_code == "OBSERVATION_ERROR"

    baseline_only = next(value for value in values if value.cohort.method == "netbanking")
    assert baseline_only.baseline_total == 1
    assert baseline_only.observation_total == 0
    assert baseline_only.observation_success_rate is None

    observation_only = next(value for value in values if value.cohort.method == "wallet")
    assert observation_only.baseline_total == 0
    assert observation_only.observation_total == 1
    assert observation_only.baseline_success_rate is None
    assert aggregate(clean_database, "acc_OtherMerchant")[0].cohort.merchant_id == (
        "acc_OtherMerchant"
    )


def test_complete_cohort_dimensions_are_separate(clean_database):
    with Session(clean_database) as session:
        variants = [
            ("provided", "HDFC", "provided", "Visa", 49_999, "card"),
            ("missing", None, "provided", "Visa", 49_999, "card"),
            ("redacted", None, "provided", "Visa", 49_999, "card"),
            ("not_applicable", None, "provided", "Visa", 49_999, "card"),
            ("provided", "HDFC", "missing", None, 49_999, "card"),
            ("provided", "HDFC", "redacted", None, 49_999, "card"),
            ("provided", "HDFC", "not_applicable", None, 49_999, "card"),
            ("provided", "HDFC", "provided", "Visa", 50_000, "card"),
            ("provided", "HDFC", "provided", "Visa", 49_999, "emi"),
        ]
        for number, (issuer_state, issuer, provider_state, provider, amount, method) in enumerate(
            variants
        ):
            add_event(
                session,
                f"variant_{number}",
                WINDOW.observation_start,
                method=method,
                issuer=issuer,
                issuer_availability=issuer_state,
                provider=provider,
                provider_availability=provider_state,
                amount_paise=amount,
            )
        session.commit()

    values = aggregate(clean_database)
    assert len(values) == len(variants)
    assert len({value.cohort.cohort_hash for value in values}) == len(variants)
    assert {value.cohort.amount_band for value in values} == {
        AmountBand.BAND_0,
        AmountBand.BAND_1,
    }


@pytest.mark.parametrize(
    ("amount_paise", "expected_band"),
    [
        (49_999, AmountBand.BAND_0),
        (50_000, AmountBand.BAND_1),
        (50_001, AmountBand.BAND_1),
        (99_999, AmountBand.BAND_1),
        (100_000, AmountBand.BAND_2),
        (100_001, AmountBand.BAND_2),
        (199_999, AmountBand.BAND_2),
        (200_000, AmountBand.BAND_3),
        (200_001, AmountBand.BAND_3),
        (499_999, AmountBand.BAND_3),
        (500_000, AmountBand.BAND_4),
        (500_001, AmountBand.BAND_4),
        (999_999, AmountBand.BAND_4),
        (1_000_000, AmountBand.BAND_5),
        (1_000_001, AmountBand.BAND_5),
    ],
)
def test_postgresql_amount_band_expression_matches_all_v2_boundaries(
    clean_database, amount_paise: int, expected_band: AmountBand
):
    with Session(clean_database) as session:
        add_event(
            session,
            f"band_{amount_paise}",
            WINDOW.observation_start,
            amount_paise=amount_paise,
        )
        session.commit()

    values = aggregate(clean_database)
    assert len(values) == 1
    assert values[0].cohort.amount_band == expected_band


def test_same_cohort_v2_ignores_conflicting_legacy_cohort_keys(clean_database):
    with Session(clean_database) as session:
        add_event(
            session,
            "legacy_conflict_a",
            WINDOW.observation_start,
            cohort_key="legacy:cohort:a",
        )
        add_event(
            session,
            "legacy_conflict_b",
            WINDOW.observation_start,
            cohort_key="legacy:cohort:b",
        )
        session.commit()

    values = aggregate(clean_database)
    assert len(values) == 1
    assert values[0].observation_total == 2


def test_different_cohort_v2_values_ignore_shared_legacy_cohort_key(clean_database):
    with Session(clean_database) as session:
        add_event(
            session,
            "legacy_shared_card",
            WINDOW.observation_start,
            method="card",
            cohort_key="legacy:shared",
        )
        add_event(
            session,
            "legacy_shared_emi",
            WINDOW.observation_start,
            method="emi",
            cohort_key="legacy:shared",
        )
        session.commit()

    values = aggregate(clean_database)
    assert len(values) == 2
    assert {value.cohort.method for value in values} == {"card", "emi"}
    assert all(value.observation_total == 1 for value in values)


def test_error_distribution_is_bounded_ordered_and_ignores_nulls(clean_database):
    with Session(clean_database) as session:
        for number in range(22):
            add_event(
                session,
                f"error_{number:02d}",
                WINDOW.observation_start,
                status="failed",
                error_code=f"ERROR_{number:02d}",
            )
        add_event(
            session,
            "error_a_extra",
            WINDOW.observation_start,
            status="failed",
            error_code="ERROR_00",
        )
        add_event(
            session,
            "error_b_extra",
            WINDOW.observation_start,
            status="failed",
            error_code="ERROR_01",
        )
        add_event(
            session,
            "missing_error",
            WINDOW.observation_start,
            status="failed",
            error_code=None,
        )
        add_event(
            session,
            "missing_only_error_a",
            WINDOW.observation_start,
            method="wallet",
            issuer=None,
            issuer_availability="not_applicable",
            provider="payzapp",
            status="failed",
            error_code=None,
        )
        add_event(
            session,
            "missing_only_error_b",
            WINDOW.observation_start,
            method="wallet",
            issuer=None,
            issuer_availability="not_applicable",
            provider="payzapp",
            status="failed",
            error_code=None,
        )
        session.commit()

    values = aggregate(clean_database)
    value = next(item for item in values if item.cohort.method == "card")
    distribution = tuple(
        (entry.error_code, entry.failed_count)
        for entry in value.observation_error_code_distribution
    )
    assert distribution == (
        ("ERROR_00", 2),
        ("ERROR_01", 2),
        *((f"ERROR_{number:02d}", 1) for number in range(2, 20)),
    )
    assert len(distribution) == 20
    assert distribution[-1] == ("ERROR_19", 1)
    assert "ERROR_20" not in {error_code for error_code, _ in distribution}
    assert "ERROR_21" not in {error_code for error_code, _ in distribution}
    assert value.dominant_observation_error_code == "ERROR_00"
    assert value.observation_failed_count == 25

    missing_only = next(item for item in values if item.cohort.method == "wallet")
    assert missing_only.observation_error_code_distribution == ()
    assert missing_only.dominant_observation_error_code is None
    assert missing_only.observation_failed_count == 2


def test_failed_amount_sum_exceeds_bigint_without_float_conversion(clean_database):
    amount = 9_000_000_000_000_000_000
    with Session(clean_database) as session:
        for number in range(2):
            add_event(
                session,
                f"large_{number}",
                WINDOW.observation_start,
                amount_paise=amount,
                status="failed",
                error_code="SYNTHETIC_FAILURE",
            )
        session.commit()

    value = aggregate(clean_database)[0]
    assert value.observation_failed_amount_paise == 18_000_000_000_000_000_000
    assert isinstance(value.observation_failed_amount_paise, int)
