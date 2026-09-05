"""Add the Phase 3A1 payment analytics access path.

Existing indexes put cohort, payment, method or error columns between merchant
and time. None provides the contiguous merchant/time prefix used by the bounded
analytics query, so this migration adds that access path without replacing them.

Revision ID: 20260904_0003
Revises: 20260902_0002
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_0003"
down_revision: str | None = "20260902_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_payment_events_merchant_occurred_analytics"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "payment_events",
        ["merchant_id", "occurred_at"],
        postgresql_include=[
            "method",
            "issuer",
            "issuer_availability",
            "provider",
            "provider_availability",
            "amount_paise",
            "status",
            "error_code",
        ],
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="payment_events")
