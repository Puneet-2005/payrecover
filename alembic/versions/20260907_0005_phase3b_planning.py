"""Immutable diagnosis and non-executable recommendations. PostgreSQL only.

Static DDL: does not import application models. Refuse downgrade before DDL when history exists.
"""
from alembic import op

revision = "20260907_0005"
down_revision = "20260905_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE payment_events ADD CONSTRAINT uq_payment_planning_subject UNIQUE (id,
    merchant_id, source, payment_id)
    """)
    op.execute("""
    ALTER TABLE incident_observations ADD CONSTRAINT uq_observation_planning_subject UNIQUE (id,
    incident_id, merchant_id)
    """)
    op.execute("""
    CREATE TABLE incident_diagnoses (
    id UUID NOT NULL,
    merchant_id VARCHAR(100) NOT NULL,
    incident_id BIGINT NOT NULL,
    observation_id BIGINT NOT NULL,
    diagnosis_version VARCHAR(64) NOT NULL,
    outcome VARCHAR(64) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    supporting_failed_count BIGINT NOT NULL,
    total_failed_count BIGINT NOT NULL,
    evidence JSONB NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_incident_diagnoses PRIMARY KEY (id),
    CONSTRAINT uq_diagnosis_version UNIQUE (merchant_id, observation_id, diagnosis_version),
    CONSTRAINT uq_diagnosis_subject UNIQUE (id, incident_id, observation_id, merchant_id),
    CONSTRAINT uq_diagnosis_audit_subject UNIQUE (id, incident_id),
    CONSTRAINT fk_diagnosis_observation FOREIGN KEY(observation_id, incident_id, merchant_id)
    REFERENCES incident_observations (id, incident_id, merchant_id) ON DELETE RESTRICT,
    CONSTRAINT ck_incident_diagnoses_version CHECK (diagnosis_version = 'diagnosis-v1'),
    CONSTRAINT ck_incident_diagnoses_merchant CHECK (btrim(merchant_id) <> ''),
    CONSTRAINT ck_incident_diagnoses_outcome CHECK (outcome IN
    ('probable_transient_network_failure', 'probable_issuer_unavailability',
    'probable_customer_funding_issue', 'probable_authentication_failure', 'unknown',
    'insufficient_evidence')),
    CONSTRAINT ck_incident_diagnoses_reason CHECK (reason_code IN ('strict_majority',
    'mixed_or_unmapped_failures', 'insufficient_failures')),
    CONSTRAINT ck_incident_diagnoses_counts CHECK (supporting_failed_count >= 0 AND
    total_failed_count >= supporting_failed_count),
    CONSTRAINT ck_incident_diagnoses_evidence_object CHECK (jsonb_typeof(evidence) = 'object'),
    CONSTRAINT ck_incident_diagnoses_digest CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$')
    )
    """)
    op.execute("""
    CREATE INDEX ix_diagnosis_history ON incident_diagnoses (merchant_id, incident_id,
    created_at, id)
    """)
    op.execute("""
    CREATE TABLE recovery_plans (
    id UUID NOT NULL,
    merchant_id VARCHAR(100) NOT NULL,
    incident_id BIGINT NOT NULL,
    observation_id BIGINT NOT NULL,
    diagnosis_id UUID NOT NULL,
    payment_event_id UUID NOT NULL,
    source VARCHAR(32) NOT NULL,
    payment_id VARCHAR(100) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    decision VARCHAR(32) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    retry_after_seconds INTEGER,
    max_attempts SMALLINT NOT NULL,
    execution_authorized BOOLEAN NOT NULL,
    prerequisites JSONB NOT NULL,
    evidence JSONB NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    association_evaluated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_recovery_plans PRIMARY KEY (id),
    CONSTRAINT uq_plan_payment_policy UNIQUE (merchant_id, source, payment_id, policy_version),
    CONSTRAINT uq_plan_audit_subject UNIQUE (id, diagnosis_id, incident_id),
    CONSTRAINT fk_plan_diagnosis FOREIGN KEY(diagnosis_id, incident_id, observation_id,
    merchant_id) REFERENCES incident_diagnoses (id, incident_id, observation_id, merchant_id) ON
    DELETE RESTRICT,
    CONSTRAINT fk_plan_payment FOREIGN KEY(payment_event_id, merchant_id, source, payment_id)
    REFERENCES payment_events (id, merchant_id, source, payment_id) ON DELETE RESTRICT,
    CONSTRAINT ck_recovery_plans_version CHECK (policy_version = 'recovery-planning-v1'),
    CONSTRAINT ck_recovery_plans_identity CHECK (btrim(merchant_id) <> '' AND btrim(source) <> ''
    AND btrim(payment_id) <> ''),
    CONSTRAINT ck_recovery_plans_action CHECK (action IN ('retry_now', 'wait_and_retry',
    'notify_customer', 'escalate', 'no_action')),
    CONSTRAINT ck_recovery_plans_decision CHECK (decision IN ('blocked', 'manual_review',
    'no_action')),
    CONSTRAINT ck_recovery_plans_reason CHECK (reason_code IN ('stored_success',
    'contradictory_history', 'unknown_signal', 'authentication_failure',
    'customer_funding_action', 'transient_network', 'issuer_health_wait')),
    CONSTRAINT ck_recovery_plans_bounds CHECK (max_attempts BETWEEN 0 AND 3 AND
    (retry_after_seconds IS NULL OR retry_after_seconds >= 0)),
    CONSTRAINT ck_recovery_plans_terminal CHECK ((action IN ('retry_now', 'wait_and_retry') AND
    decision = 'blocked' AND max_attempts > 0 AND retry_after_seconds IS NOT NULL) OR (action NOT
    IN ('retry_now', 'wait_and_retry') AND max_attempts = 0 AND retry_after_seconds IS NULL)),
    CONSTRAINT ck_recovery_plans_no_execution CHECK (execution_authorized = false),
    CONSTRAINT ck_recovery_plans_objects CHECK (jsonb_typeof(evidence) = 'object' AND
    jsonb_typeof(prerequisites) = 'object'),
    CONSTRAINT ck_recovery_plans_digest CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_recovery_plans_association_time CHECK (association_evaluated_at <= created_at)
    )
    """)
    op.execute("""
    CREATE INDEX ix_plan_diagnosis ON recovery_plans (diagnosis_id)
    """)
    op.execute("""
    CREATE INDEX ix_plan_history ON recovery_plans (merchant_id, incident_id, created_at, id)
    """)
    op.execute("""
    CREATE INDEX ix_plan_payment_event ON recovery_plans (payment_event_id)
    """)
    op.execute("""
    ALTER TABLE audit_records ADD COLUMN diagnosis_id UUID, ADD COLUMN recovery_plan_id UUID
    """)
    op.execute("""
    ALTER TABLE audit_records DROP CONSTRAINT ck_audit_records_incident_subject
    """)
    op.execute("""
    ALTER TABLE audit_records ADD CONSTRAINT ck_audit_records_incident_subject CHECK (
    (event_type IN ('incident.opened', 'incident.observation_updated', 'incident.resolved')
    AND incident_id IS NOT NULL AND payment_event_id IS NULL
    AND diagnosis_id IS NULL AND recovery_plan_id IS NULL) OR
    (event_type = 'incident.diagnosed' AND incident_id IS NOT NULL AND payment_event_id IS NULL
    AND diagnosis_id IS NOT NULL AND recovery_plan_id IS NULL) OR
    (event_type = 'recovery_plan.created' AND incident_id IS NOT NULL AND payment_event_id IS
    NULL
    AND diagnosis_id IS NOT NULL AND recovery_plan_id IS NOT NULL) OR
    (event_type NOT IN ('incident.opened', 'incident.observation_updated', 'incident.resolved',
    'incident.diagnosed', 'recovery_plan.created') AND incident_id IS NULL
    AND diagnosis_id IS NULL AND recovery_plan_id IS NULL)
    )
    """)
    op.execute("""
    ALTER TABLE audit_records ADD CONSTRAINT fk_audit_diagnosis_subject FOREIGN KEY(diagnosis_id,
    incident_id) REFERENCES incident_diagnoses (id, incident_id) ON DELETE RESTRICT
    """)
    op.execute("""
    ALTER TABLE audit_records ADD CONSTRAINT fk_audit_plan_subject FOREIGN KEY(recovery_plan_id,
    diagnosis_id, incident_id) REFERENCES recovery_plans (id, diagnosis_id, incident_id) ON
    DELETE RESTRICT
    """)
    op.execute("""
    CREATE INDEX ix_audit_diagnosis_history ON audit_records (diagnosis_id, id) WHERE
    diagnosis_id IS NOT NULL
    """)
    op.execute("""
    CREATE INDEX ix_audit_plan_history ON audit_records (recovery_plan_id, id) WHERE
    recovery_plan_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM incident_diagnoses) OR EXISTS (SELECT 1 FROM recovery_plans)
       OR EXISTS (SELECT 1 FROM audit_records
                  WHERE diagnosis_id IS NOT NULL OR recovery_plan_id IS NOT NULL) THEN
      RAISE EXCEPTION 'Cannot downgrade while planning history exists';
    END IF;
    END $$;
    """)
    op.drop_index("ix_audit_plan_history", table_name="audit_records")
    op.drop_index("ix_audit_diagnosis_history", table_name="audit_records")
    op.drop_constraint("fk_audit_plan_subject", "audit_records", type_="foreignkey")
    op.drop_constraint("fk_audit_diagnosis_subject", "audit_records", type_="foreignkey")
    op.drop_constraint(op.f("ck_audit_records_incident_subject"), "audit_records", type_="check")
    op.drop_column("audit_records", "recovery_plan_id")
    op.drop_column("audit_records", "diagnosis_id")
    op.create_check_constraint(
        op.f("ck_audit_records_incident_subject"), "audit_records",
        "(event_type IN ('incident.opened','incident.observation_updated','incident.resolved') "
        "AND incident_id IS NOT NULL AND payment_event_id IS NULL) OR "
        "(event_type NOT IN ('incident.opened','incident.observation_updated','incident.resolved') "
        "AND incident_id IS NULL)",
    )
    op.drop_table("recovery_plans")
    op.drop_table("incident_diagnoses")
    op.drop_constraint("uq_observation_planning_subject", "incident_observations", type_="unique")
    op.drop_constraint("uq_payment_planning_subject", "payment_events", type_="unique")
