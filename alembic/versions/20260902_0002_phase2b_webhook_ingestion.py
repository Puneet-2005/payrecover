"""Add the Phase 2B Razorpay webhook persistence contract.

Revision ID: 20260902_0002
Revises: 20260902_0001
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0002"
down_revision: str | None = "20260902_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AVAILABILITY_VALUES = "('provided', 'missing', 'not_applicable', 'redacted')"


def upgrade() -> None:
    op.add_column(
        "payment_events", sa.Column("issuer_availability", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "payment_events", sa.Column("provider_availability", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "payment_events", sa.Column("error_code_availability", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "payment_events", sa.Column("error_source", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "payment_events", sa.Column("error_step", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "payment_events", sa.Column("error_reason", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "payment_events", sa.Column("latency_availability", sa.String(length=20), nullable=True)
    )

    op.execute(
        sa.text(
            """
            UPDATE payment_events
            SET issuer_availability = 'provided',
                provider_availability = 'provided',
                latency_availability = 'provided',
                error_code_availability = CASE
                    WHEN NULLIF(BTRIM(error_code), '') IS NOT NULL THEN 'provided'
                    WHEN status = 'success' THEN 'not_applicable'
                    ELSE 'missing'
                END
            """
        )
    )

    op.alter_column("payment_events", "issuer_availability", nullable=False)
    op.alter_column("payment_events", "provider_availability", nullable=False)
    op.alter_column("payment_events", "error_code_availability", nullable=False)
    op.alter_column("payment_events", "latency_availability", nullable=False)

    op.drop_constraint(
        op.f("ck_payment_events_failed_event_has_error_code"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_issuer_nonblank"), "payment_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_payment_events_provider_nonblank"), "payment_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_payment_events_latency_ms_nonnegative"),
        "payment_events",
        type_="check",
    )

    op.alter_column(
        "payment_events", "issuer", existing_type=sa.String(length=100), nullable=True
    )
    op.alter_column(
        "payment_events", "provider", existing_type=sa.String(length=100), nullable=True
    )
    op.alter_column("payment_events", "latency_ms", existing_type=sa.Integer(), nullable=True)
    op.alter_column(
        "payment_events", "cohort_key", existing_type=sa.String(length=255), nullable=True
    )

    op.create_check_constraint(
        op.f("ck_payment_events_issuer_availability_allowed"),
        "payment_events",
        f"issuer_availability IN {AVAILABILITY_VALUES}",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_issuer_matches_availability"),
        "payment_events",
        "(issuer_availability = 'provided' AND issuer IS NOT NULL "
        "AND CHAR_LENGTH(BTRIM(issuer)) > 0) OR "
        "(issuer_availability <> 'provided' AND issuer IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_provider_availability_allowed"),
        "payment_events",
        f"provider_availability IN {AVAILABILITY_VALUES}",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_provider_matches_availability"),
        "payment_events",
        "(provider_availability = 'provided' AND provider IS NOT NULL "
        "AND CHAR_LENGTH(BTRIM(provider)) > 0) OR "
        "(provider_availability <> 'provided' AND provider IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_latency_availability_allowed"),
        "payment_events",
        f"latency_availability IN {AVAILABILITY_VALUES}",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_latency_matches_availability"),
        "payment_events",
        "(latency_availability = 'provided' AND latency_ms IS NOT NULL "
        "AND latency_ms >= 0) OR "
        "(latency_availability <> 'provided' AND latency_ms IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_error_code_availability_allowed"),
        "payment_events",
        f"error_code_availability IN {AVAILABILITY_VALUES}",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_error_code_matches_availability"),
        "payment_events",
        "(error_code_availability = 'provided' AND error_code IS NOT NULL "
        "AND CHAR_LENGTH(BTRIM(error_code)) > 0) OR "
        "(error_code_availability <> 'provided' AND error_code IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_status_matches_error_code_availability"),
        "payment_events",
        "(status = 'failed' AND error_code_availability IN ('provided', 'missing')) OR "
        "(status = 'success' AND "
        "error_code_availability IN ('provided', 'not_applicable'))",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_error_source_nonblank"),
        "payment_events",
        "error_source IS NULL OR CHAR_LENGTH(BTRIM(error_source)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_error_step_nonblank"),
        "payment_events",
        "error_step IS NULL OR CHAR_LENGTH(BTRIM(error_step)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_error_reason_nonblank"),
        "payment_events",
        "error_reason IS NULL OR CHAR_LENGTH(BTRIM(error_reason)) > 0",
    )
    op.create_index(
        "ix_payment_events_method_time",
        "payment_events",
        ["merchant_id", "method", "occurred_at"],
    )
    op.create_index(
        "uq_payment_events_razorpay_event_id",
        "payment_events",
        ["source_event_id"],
        unique=True,
        postgresql_where=sa.text("source = 'razorpay_webhook'"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    incompatible_rows_exist = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM payment_events
                WHERE issuer IS NULL
                   OR provider IS NULL
                   OR latency_ms IS NULL
                   OR cohort_key IS NULL
                   OR (status = 'failed' AND NULLIF(BTRIM(error_code), '') IS NULL)
            )
            """
        )
    ).scalar_one()
    if incompatible_rows_exist:
        raise RuntimeError(
            "Cannot downgrade Phase 2B while payment events use nullable webhook fields"
        )

    op.drop_index("uq_payment_events_razorpay_event_id", table_name="payment_events")
    op.drop_index("ix_payment_events_method_time", table_name="payment_events")
    op.drop_constraint(
        op.f("ck_payment_events_error_reason_nonblank"), "payment_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_payment_events_error_step_nonblank"), "payment_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_payment_events_error_source_nonblank"), "payment_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_payment_events_status_matches_error_code_availability"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_error_code_matches_availability"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_error_code_availability_allowed"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_latency_matches_availability"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_latency_availability_allowed"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_provider_matches_availability"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_provider_availability_allowed"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_issuer_matches_availability"),
        "payment_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_payment_events_issuer_availability_allowed"),
        "payment_events",
        type_="check",
    )

    op.alter_column(
        "payment_events", "cohort_key", existing_type=sa.String(length=255), nullable=False
    )
    op.alter_column("payment_events", "latency_ms", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "payment_events", "provider", existing_type=sa.String(length=100), nullable=False
    )
    op.alter_column(
        "payment_events", "issuer", existing_type=sa.String(length=100), nullable=False
    )

    op.drop_column("payment_events", "latency_availability")
    op.drop_column("payment_events", "error_reason")
    op.drop_column("payment_events", "error_step")
    op.drop_column("payment_events", "error_source")
    op.drop_column("payment_events", "error_code_availability")
    op.drop_column("payment_events", "provider_availability")
    op.drop_column("payment_events", "issuer_availability")

    op.create_check_constraint(
        op.f("ck_payment_events_latency_ms_nonnegative"),
        "payment_events",
        "latency_ms >= 0",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_failed_event_has_error_code"),
        "payment_events",
        "status <> 'failed' OR NULLIF(BTRIM(error_code), '') IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_issuer_nonblank"),
        "payment_events",
        "CHAR_LENGTH(BTRIM(issuer)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_payment_events_provider_nonblank"),
        "payment_events",
        "CHAR_LENGTH(BTRIM(provider)) > 0",
    )
