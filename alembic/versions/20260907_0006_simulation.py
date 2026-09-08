"""Isolated synthetic simulation history; marker is never created implicitly."""
from alembic import op

revision = "20260907_0006"
down_revision = "20260907_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE recovery_plans ADD CONSTRAINT uq_sim_plan_subject
        UNIQUE(id, merchant_id, source, payment_id);
    CREATE TABLE simulation_marker (
        database_name VARCHAR(63) PRIMARY KEY, version VARCHAR(32) NOT NULL);
    CREATE TABLE simulation_runs (
        id UUID PRIMARY KEY, merchant_id VARCHAR(100) NOT NULL,
        CONSTRAINT uq_simulation_runs_merchant_id UNIQUE(merchant_id),
        manifest JSONB NOT NULL, manifest_sha256 VARCHAR(64) NOT NULL,
        status VARCHAR(16) NOT NULL, report JSONB,
        CONSTRAINT uq_sim_run_merchant UNIQUE(id, merchant_id),
        CONSTRAINT ck_simulation_runs_merchant CHECK(merchant_id LIKE 'sim_%'),
        CONSTRAINT ck_simulation_runs_status CHECK(status IN ('running','complete')));
    CREATE TABLE simulation_payments (
        run_id UUID NOT NULL, payment_id VARCHAR(100) NOT NULL,
        merchant_id VARCHAR(100) NOT NULL, source VARCHAR(32) NOT NULL,
        event_id UUID NOT NULL, plan_id UUID, amount_paise BIGINT NOT NULL,
        fixture JSONB NOT NULL, PRIMARY KEY(run_id,payment_id),
        CONSTRAINT ck_simulation_payments_amount CHECK(amount_paise > 0),
        CONSTRAINT fk_sim_payment_run FOREIGN KEY(run_id,merchant_id)
            REFERENCES simulation_runs(id,merchant_id) ON DELETE RESTRICT,
        CONSTRAINT fk_sim_payment_event FOREIGN KEY(event_id,merchant_id,source,payment_id)
            REFERENCES payment_events(id,merchant_id,source,payment_id) ON DELETE RESTRICT,
        CONSTRAINT fk_sim_payment_plan FOREIGN KEY(plan_id,merchant_id,source,payment_id)
            REFERENCES recovery_plans(id,merchant_id,source,payment_id) ON DELETE RESTRICT);
    CREATE TABLE simulation_attempts (
        run_id UUID NOT NULL, payment_id VARCHAR(100) NOT NULL, ordinal INTEGER NOT NULL,
        reserved_at TIMESTAMPTZ NOT NULL, state VARCHAR(16) NOT NULL, truth VARCHAR(16),
        PRIMARY KEY(run_id,payment_id,ordinal),
        CONSTRAINT fk_sim_attempt_payment FOREIGN KEY(run_id,payment_id)
            REFERENCES simulation_payments(run_id,payment_id) ON DELETE RESTRICT,
        CONSTRAINT ck_simulation_attempts_ordinal CHECK(ordinal BETWEEN 1 AND 2),
        CONSTRAINT ck_simulation_attempts_state
            CHECK(state IN ('reserved','uncertain','failed','succeeded')));
    CREATE UNIQUE INDEX uq_sim_outstanding ON simulation_attempts(run_id,payment_id)
        WHERE state IN ('reserved','uncertain');
    CREATE UNIQUE INDEX uq_sim_success ON simulation_attempts(run_id,payment_id)
        WHERE state = 'succeeded';
    CREATE TABLE simulation_steps (
        run_id UUID NOT NULL REFERENCES simulation_runs(id) ON DELETE RESTRICT,
        key VARCHAR(160) NOT NULL, kind VARCHAR(32) NOT NULL,
        logical_at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL, PRIMARY KEY(run_id,key));
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM simulation_runs) THEN
        RAISE EXCEPTION 'Cannot downgrade while simulation history exists';
      END IF;
    END $$;
    DROP TABLE simulation_steps, simulation_attempts, simulation_payments,
        simulation_runs, simulation_marker;
    ALTER TABLE recovery_plans DROP CONSTRAINT uq_sim_plan_subject;
    """)
