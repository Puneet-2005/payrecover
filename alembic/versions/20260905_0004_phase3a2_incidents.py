"""
Persist incident scans and lifecycle evidence (PostgreSQL only).

Existing payment audits retain their nullable subject contract. Downgrade refuses
to orphan incident audits; export/retention decisions require explicit operator action.
"""

import sqlalchemy as sa

from alembic import op

revision = "20260905_0004"
down_revision = "20260904_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE incident_scan_runs (
    id UUID NOT NULL,
    merchant_id VARCHAR(100) NOT NULL,
    detector_version VARCHAR(64) NOT NULL,
    baseline_start TIMESTAMP WITH TIME ZONE NOT NULL,
    baseline_end TIMESTAMP WITH TIME ZONE NOT NULL,
    observation_start TIMESTAMP WITH TIME ZONE NOT NULL,
    observation_end TIMESTAMP WITH TIME ZONE NOT NULL,
    evaluated BIGINT NOT NULL,
    opened BIGINT NOT NULL,
    updated BIGINT NOT NULL,
    resolved BIGINT NOT NULL,
    recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_incident_scan_runs PRIMARY KEY (id),
    CONSTRAINT uq_scan_merchant_detector_window UNIQUE (merchant_id, detector_version,
    observation_start),
    CONSTRAINT uq_scan_subject UNIQUE (id, merchant_id, detector_version),
    CONSTRAINT ck_incident_scan_runs_merchant_nonblank CHECK (btrim(merchant_id) <> ''),
    CONSTRAINT ck_incident_scan_runs_detector_nonblank CHECK (btrim(detector_version) <> ''),
    CONSTRAINT ck_incident_scan_runs_windows CHECK (baseline_start = baseline_end - interval
    '7 days' AND baseline_end = observation_start AND observation_end = observation_start +
    interval '15 minutes' AND mod(extract(epoch from observation_start), 900) = 0),
    CONSTRAINT ck_incident_scan_runs_summary CHECK (evaluated >= 0 AND opened >= 0 AND
    updated >= 0 AND resolved >= 0 AND opened + updated <= evaluated AND resolved <= updated)
    )
    """)
    op.execute("""
    CREATE TABLE incidents (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    merchant_id VARCHAR(100) NOT NULL,
    cohort_version INTEGER NOT NULL,
    cohort_hash VARCHAR(64) NOT NULL,
    method VARCHAR(32) NOT NULL,
    issuer VARCHAR(100),
    issuer_availability VARCHAR(20) NOT NULL,
    provider VARCHAR(100),
    provider_availability VARCHAR(20) NOT NULL,
    amount_band VARCHAR(16) NOT NULL,
    detector_version VARCHAR(64) NOT NULL,
    opened_scan_id UUID NOT NULL,
    opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
    opening_window_start TIMESTAMP WITH TIME ZONE NOT NULL,
    opening_severity VARCHAR(16) NOT NULL,
    peak_severity VARCHAR(16) NOT NULL,
    latest_outcome VARCHAR(16) NOT NULL,
    last_window_start TIMESTAMP WITH TIME ZONE NOT NULL,
    healthy_streak INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_incidents PRIMARY KEY (id),
    CONSTRAINT fk_incident_open_scan FOREIGN KEY(opened_scan_id, merchant_id,
    detector_version) REFERENCES incident_scan_runs (id, merchant_id, detector_version) ON
    DELETE RESTRICT,
    CONSTRAINT uq_incident_subject UNIQUE (id, merchant_id, detector_version),
    CONSTRAINT ck_incidents_cohort_version CHECK (cohort_version = 2),
    CONSTRAINT ck_incidents_cohort_hex CHECK (cohort_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_incidents_dimensions_nonblank CHECK (btrim(merchant_id) <> '' AND
    btrim(method) <> '' AND btrim(detector_version) <> ''),
    CONSTRAINT ck_incidents_amount_band CHECK (amount_band IN
    ('band_0','band_1','band_2','band_3','band_4','band_5')),
    CONSTRAINT ck_incidents_issuer_availability CHECK ((issuer_availability = 'provided' AND
    issuer IS NOT NULL AND btrim(issuer) <> '') OR (issuer_availability IN
    ('missing','not_applicable','redacted') AND issuer IS NULL)),
    CONSTRAINT ck_incidents_provider_availability CHECK ((provider_availability = 'provided'
    AND provider IS NOT NULL AND btrim(provider) <> '') OR (provider_availability IN
    ('missing','not_applicable','redacted') AND provider IS NULL)),
    CONSTRAINT ck_incidents_severities CHECK (opening_severity IN
    ('medium','high','critical') AND peak_severity IN ('medium','high','critical')),
    CONSTRAINT ck_incidents_peak_at_least_opening CHECK ((opening_severity = 'medium') OR
    (opening_severity = 'high' AND peak_severity IN ('high','critical')) OR (opening_severity
    = 'critical' AND peak_severity = 'critical')),
    CONSTRAINT ck_incidents_outcome CHECK (latest_outcome IN
    ('degraded','healthy','watch','insufficient')),
    CONSTRAINT ck_incidents_streak CHECK (healthy_streak BETWEEN 0 AND 2 AND ((latest_outcome
    = 'healthy' AND healthy_streak > 0) OR (latest_outcome <> 'healthy' AND healthy_streak =
    0))),
    CONSTRAINT ck_incidents_status CHECK ((status = 'open' AND resolved_at IS NULL AND
    healthy_streak < 2) OR (status = 'resolved' AND resolved_at IS NOT NULL AND
    healthy_streak = 2)),
    CONSTRAINT ck_incidents_chronology CHECK (last_window_start >= opening_window_start AND
    (resolved_at IS NULL OR resolved_at >= opened_at))
    )
    """)
    op.execute("""
    CREATE TABLE incident_observations (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    incident_id BIGINT NOT NULL,
    scan_id UUID NOT NULL,
    merchant_id VARCHAR(100) NOT NULL,
    detector_version VARCHAR(64) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    severity VARCHAR(16),
    baseline_total BIGINT NOT NULL,
    baseline_success_count BIGINT NOT NULL,
    baseline_failed_count BIGINT NOT NULL,
    baseline_failed_amount_paise NUMERIC(38, 0) NOT NULL,
    baseline_success_rate NUMERIC(13, 12),
    observation_total BIGINT NOT NULL,
    observation_success_count BIGINT NOT NULL,
    observation_failed_count BIGINT NOT NULL,
    observation_failed_amount_paise NUMERIC(38, 0) NOT NULL,
    observation_success_rate NUMERIC(13, 12),
    absolute_success_rate_drop NUMERIC(13, 12),
    z_score NUMERIC(38, 12),
    recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_incident_observations PRIMARY KEY (id),
    CONSTRAINT uq_incident_observation_scan UNIQUE (incident_id, scan_id),
    CONSTRAINT fk_observation_incident FOREIGN KEY(incident_id, merchant_id,
    detector_version) REFERENCES incidents (id, merchant_id, detector_version) ON DELETE
    RESTRICT,
    CONSTRAINT fk_observation_scan FOREIGN KEY(scan_id, merchant_id, detector_version)
    REFERENCES incident_scan_runs (id, merchant_id, detector_version) ON DELETE RESTRICT,
    CONSTRAINT ck_incident_observations_outcome CHECK (outcome IN
    ('degraded','healthy','watch','insufficient')),
    CONSTRAINT ck_incident_observations_severity CHECK ((outcome = 'degraded' AND severity IS
    NOT NULL AND severity IN ('medium','high','critical')) OR (outcome <> 'degraded' AND
    severity IS NULL)),
    CONSTRAINT ck_incident_observations_baseline_evidence CHECK (baseline_total >= 0 AND
    baseline_success_count >= 0 AND baseline_failed_count >= 0 AND baseline_total =
    baseline_success_count + baseline_failed_count AND baseline_failed_amount_paise >= 0 AND
    baseline_failed_amount_paise < 'Infinity'::numeric AND ((baseline_total = 0 AND
    baseline_success_rate IS NULL) OR (baseline_total > 0 AND baseline_success_rate IS NOT
    NULL AND baseline_success_rate BETWEEN 0 AND 1))),
    CONSTRAINT ck_incident_observations_observation_evidence CHECK (observation_total >= 0
    AND observation_success_count >= 0 AND observation_failed_count >= 0 AND
    observation_total = observation_success_count + observation_failed_count AND
    observation_failed_amount_paise >= 0 AND observation_failed_amount_paise <
    'Infinity'::numeric AND ((observation_total = 0 AND observation_success_rate IS NULL) OR
    (observation_total > 0 AND observation_success_rate IS NOT NULL AND
    observation_success_rate BETWEEN 0 AND 1))),
    CONSTRAINT ck_incident_observations_drop_range CHECK (absolute_success_rate_drop IS NULL
    OR absolute_success_rate_drop BETWEEN -1 AND 1),
    CONSTRAINT ck_incident_observations_score_finite CHECK (z_score IS NULL OR (z_score >
    '-Infinity'::numeric AND z_score < 'Infinity'::numeric))
    )
    """)
    op.execute("""
    CREATE TABLE incident_error_counts (
    observation_id BIGINT NOT NULL,
    rank INTEGER NOT NULL,
    error_code VARCHAR(100) NOT NULL,
    failed_count BIGINT NOT NULL,
    CONSTRAINT pk_incident_error_counts PRIMARY KEY (observation_id, rank),
    CONSTRAINT uq_observation_error_code UNIQUE (observation_id, error_code),
    CONSTRAINT ck_incident_error_counts_rank_bound CHECK (rank BETWEEN 1 AND 20),
    CONSTRAINT ck_incident_error_counts_code_nonblank CHECK (btrim(error_code) <> ''),
    CONSTRAINT ck_incident_error_counts_count_positive CHECK (failed_count > 0),
    CONSTRAINT fk_incident_error_counts_observation_id_incident_observations FOREIGN
    KEY(observation_id) REFERENCES incident_observations (id) ON DELETE RESTRICT
    )
    """)
    op.execute("""
    CREATE INDEX ix_incident_merchant_history ON incidents (merchant_id, id)
    """)
    op.execute("""
    CREATE UNIQUE INDEX uq_incident_open_cohort ON incidents (merchant_id, cohort_version,
    cohort_hash, detector_version) WHERE status = 'open'
    """)
    op.execute("""
    CREATE INDEX ix_observation_history ON incident_observations (incident_id, id)
    """)
    op.execute("""
    ALTER TABLE audit_records ADD COLUMN incident_id BIGINT,
    ADD CONSTRAINT fk_audit_records_incident_id_incidents
    FOREIGN KEY (incident_id) REFERENCES incidents(id) ON DELETE RESTRICT,
    ADD CONSTRAINT ck_audit_records_incident_subject CHECK (
    (event_type IN ('incident.opened','incident.observation_updated','incident.resolved')
    AND incident_id IS NOT NULL AND payment_event_id IS NULL) OR
    (event_type NOT IN ('incident.opened','incident.observation_updated','incident.resolved')
    AND incident_id IS NULL)
    )
    """)
    op.create_index(
        "ix_audit_incident_history",
        "audit_records",
        ["incident_id", "id"],
        postgresql_where=sa.text("incident_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM audit_records WHERE incident_id IS NOT NULL) THEN
    RAISE EXCEPTION 'Cannot downgrade while incident audits exist';
    END IF;
    END $$
    """)
    op.drop_index("ix_audit_incident_history", table_name="audit_records")
    op.drop_constraint(op.f("ck_audit_records_incident_subject"), "audit_records", type_="check")
    op.drop_constraint(
        "fk_audit_records_incident_id_incidents", "audit_records", type_="foreignkey"
    )
    op.drop_column("audit_records", "incident_id")
    op.drop_table("incident_error_counts")
    op.drop_table("incident_observations")
    op.drop_table("incidents")
    op.drop_table("incident_scan_runs")
