"""Create Phase 2A payment event and audit persistence.

Revision ID: 20260902_0001
Revises:
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260902_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_events",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=100), nullable=False),
        sa.Column("payment_id", sa.String(length=100), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cohort_key", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_paise > 0", name=op.f("ck_payment_events_amount_paise_positive")
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR NULLIF(BTRIM(error_code), '') IS NOT NULL",
            name=op.f("ck_payment_events_failed_event_has_error_code"),
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(BTRIM(issuer)) > 0",
            name=op.f("ck_payment_events_issuer_nonblank"),
        ),
        sa.CheckConstraint(
            "latency_ms >= 0", name=op.f("ck_payment_events_latency_ms_nonnegative")
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(BTRIM(method)) > 0",
            name=op.f("ck_payment_events_method_nonblank"),
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(payload_sha256) = 64",
            name=op.f("ck_payment_events_payload_sha256_length"),
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(BTRIM(provider)) > 0",
            name=op.f("ck_payment_events_provider_nonblank"),
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_payment_events_schema_version_positive")
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(BTRIM(source)) > 0",
            name=op.f("ck_payment_events_source_nonblank"),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'failed')",
            name=op.f("ck_payment_events_status_allowed"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_events")),
        sa.UniqueConstraint(
            "source",
            "merchant_id",
            "source_event_id",
            name=op.f("uq_payment_events_source_merchant_event"),
        ),
    )
    op.create_index(
        "ix_payment_events_cohort_time",
        "payment_events",
        ["merchant_id", "cohort_key", "occurred_at"],
    )
    op.create_index(
        "ix_payment_events_failed_error_time",
        "payment_events",
        ["merchant_id", "error_code", "occurred_at"],
        postgresql_where=sa.text("status = 'failed'"),
    )
    op.create_index(
        "ix_payment_events_payment_time",
        "payment_events",
        ["merchant_id", "payment_id", "occurred_at"],
    )

    op.create_table(
        "audit_records",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column("payment_event_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_ref_digest", sa.String(length=64), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(BTRIM(actor_type)) > 0",
            name=op.f("ck_audit_records_actor_type_nonblank"),
        ),
        sa.CheckConstraint(
            "actor_ref_digest IS NULL OR CHAR_LENGTH(actor_ref_digest) = 64",
            name=op.f("ck_audit_records_actor_ref_digest_length"),
        ),
        sa.CheckConstraint(
            "CHAR_LENGTH(BTRIM(event_type)) > 0",
            name=op.f("ck_audit_records_event_type_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["payment_event_id"],
            ["payment_events.id"],
            name=op.f("fk_audit_records_payment_event_id_payment_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_records")),
    )
    op.create_index(
        "ix_audit_records_correlation",
        "audit_records",
        ["correlation_id", "id"],
    )
    op.create_index(
        "ix_audit_records_payment_event",
        "audit_records",
        ["payment_event_id", "recorded_at", "id"],
        postgresql_where=sa.text("payment_event_id IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_records_recorded",
        "audit_records",
        ["recorded_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_records_recorded", table_name="audit_records")
    op.drop_index("ix_audit_records_payment_event", table_name="audit_records")
    op.drop_index("ix_audit_records_correlation", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_index("ix_payment_events_payment_time", table_name="payment_events")
    op.drop_index("ix_payment_events_failed_error_time", table_name="payment_events")
    op.drop_index("ix_payment_events_cohort_time", table_name="payment_events")
    op.drop_table("payment_events")
