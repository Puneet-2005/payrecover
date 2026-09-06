# Phase 3A2 incident scans

This is a local-development incident history interface, not an authenticated multi-tenant service. There is no scheduler, Redis processing, AI diagnosis, recovery execution or payment action. Run the CLI explicitly and bind the API to loopback.

## Transaction and identity

One call scans one merchant and the completed Phase 3A1 observation window. The service validates aware `as_of` and injected `now`, rejects future input, and reuses the existing window calculator. At PostgreSQL's default READ COMMITTED isolation it takes `pg_advisory_xact_lock` using the signed first 64 bits of SHA-256 over a namespaced merchant ID. A hash collision can only serialize unrelated merchants; all queries still explicitly filter merchant identity.

The transaction then checks a unique `(merchant_id, detector_version, observation_start)` scan. A committed duplicate returns the exact stored summary without aggregation, lifecycle changes or new audits, even if newer windows have since committed. An unseen window earlier than the latest committed window is rejected. The scan claim, one PostgreSQL aggregation query, incident updates, observations, ranked error evidence and audits all commit together. A failure rolls everything back, including the claim, so retry is safe. Repositories never commit.

The first commit freezes the window. Late arrivals cannot rewrite it, but can appear in later seven-day baselines. Each aggregate query sees a PostgreSQL statement snapshot; a payment ingestion that commits after this snapshot belongs to this late-arrival policy. Merchant scans serialize; independent merchant scans may overlap. This is not a throughput or performance claim.

## Tables

All timestamps below are `TIMESTAMPTZ`, supplied as aware UTC values through domain/service boundaries and UTC database sessions. IDs of scans are UUIDs; incident and observation IDs use BIGINT generated-always identities. Every primary key, foreign key, check and unique constraint is named in revision `20260905_0004`.

| Table | Stored fields | Identity and access |
| --- | --- | --- |
| `incident_scan_runs` | UUID `id`; merchant VARCHAR(100), detector VARCHAR(64); baseline and observation start/end timestamps; BIGINT `evaluated`, `opened`, `updated`, `resolved`; `recorded_at` | Unique merchant/detector/window; composite subject uniqueness for restrictive child FKs. Windows are seven-day baseline plus adjacent, aligned 15-minute observation. Summary counts are nonnegative and consistent. |
| `incidents` | BIGINT `id`; merchant, cohort version, VARCHAR(64) lowercase-hex cohort SHA-256; method, nullable issuer/provider and explicit availability states, amount-band label; detector version; opening scan, opening timestamp/window/severity; peak severity, latest outcome/window, healthy streak, status, nullable resolution timestamp | One open row per merchant/cohort-version/hash/detector via partial unique index. `(merchant_id,id)` supports ascending history pagination. Opening scan uses a merchant/detector composite FK with ON DELETE RESTRICT. |
| `incident_observations` | BIGINT `id`, incident ID, scan UUID, merchant/detector; outcome and nullable severity; baseline/observation totals, success and failure counts; exact failed paise amounts; rates, absolute drop, z-score; recorded timestamp | Unique incident/scan. Composite restrictive incident/scan FKs prevent cross-merchant or cross-detector evidence. `(incident_id,id)` history index. Windows come from the immutable referenced scan; cohort dimensions come from the immutable incident. |
| `incident_error_counts` | Observation ID, integer rank, allowlisted error code VARCHAR(100), BIGINT failed count | Composite primary key `(observation_id,rank)`, rank 1–20; unique observation/code; positive counts; restrictive observation FK. No raw payment IDs or arbitrary metadata. |

Counts use BIGINT, matching PostgreSQL COUNT. Failed amounts use `NUMERIC(38,0)`: even the product of maximum positive BIGINT event count and maximum BIGINT paise fits 38 digits. Rates and absolute drop use `NUMERIC(13,12)`, and scores use `NUMERIC(38,12)`. These accommodate PostgreSQL aggregate counts, while the pure Phase 3A1 detector still accepts larger synthetic inputs. Checks reject non-finite persisted values and inconsistent counts/value availability. Monetary values are never converted to float.

The detector makes decisions before its output rounding. The scanner persists that decision, including nullable observation severity, without reconstructing thresholds from stored rates. Database checks enforce valid categories and ranges but do not infer exact detector classifications from rounded evidence. JSON responses serialize Decimal statistics as strings and paise as integers.

## Lifecycle and immutable evidence

A degraded cohort opens an incident if none is open. Its first observation, referenced by its opening scan, contains immutable opening evidence. Opening severity and cohort dimensions are never updated by the repository and are protected against ordinary ORM mutation. Peak severity only increases on degraded observations. Every subsequent scanned window adds one observation for every existing open incident, including completely absent cohorts. Absence produces zero baseline/observation counts, null rates and score, empty errors and an insufficient outcome.

Healthy increments the streak only after an immediately adjacent healthy window. Watch, insufficient and degraded reset it; a gap restarts a healthy streak at one. Two adjacent healthy windows resolve once. Resolved rows remain historical, and a subsequent degradation creates a new incident. Resolution establishes the detector's relative health criterion, not proof that the original outage has ended. No refund, retry or recovery action follows resolution.

Repository interfaces return typed domain records. Observation/error evidence is append-only through the ORM. Committed scan summaries and incident opening fields are immutable through application paths. These are not protections against privileged direct SQL.

Peak severity decreases are rejected both by `update_incident()` and the ordinary ORM flush guard. Each checks authoritative persisted state under a PostgreSQL row lock held through transaction completion, including when session attributes are expired, unloaded or stale. Increases and unchanged peaks remain valid. This is application enforcement: bulk/direct SQL can bypass it; the database check only requires peak severity to be at least opening severity.

Observation repositories accept an explicit merchant ID and incident ID. Upper-bound, page and ranked-error queries join through the persisted incident and enforce that pair; returned cohort dimensions also come from the persisted incident. SQL limits continue to bound each observation page without deleting history.

## Audits and migration compatibility

`audit_records.incident_id` is a nullable BIGINT FK with ON DELETE RESTRICT and a partial `(incident_id,id)` history index. The three new event types require an incident and prohibit a payment-event subject. Older event types retain the existing nullable payment subject contract and cannot attach an incident. The migration neither fabricates subjects nor deletes old records.

Event-specific strict Pydantic schemas allow only scan/observation IDs, cohort digest, detector version, window timestamps, and the relevant opening severity, outcome/streak, or fixed resolution reason. Opening writes `incident.opened`; subsequent observations write `incident.observation_updated`; resolution additionally writes `incident.resolved`. These writes share the state transaction. Existing payment audit behavior is preserved. Application append-only auditing is not tamper-proof.

The migration is self-contained PostgreSQL DDL run by Alembic and never imports mutable application model definitions. Its downgrade drops the four new tables and audit extension only when no incident audits exist. With incident audits present it fails atomically, preserving their subjects; an operator must first make an explicit export/retention decision outside this implementation. Upgrade/downgrade/re-upgrade is tested on disposable PostgreSQL 17 with existing Phase 3A1 payment data and audits. Never point test migrations at an unrelated developer database.

## CLI and pagination

`python -m payrecover.cli --merchant-id ID [--as-of RFC3339]` reads the injected clock once. The optional timestamp must contain seconds and `Z` or a numeric timezone offset; fractional seconds accept up to six digits. Output contains the stored scan summary and a replay indicator. Errors use nonzero exit codes and generic database/configuration messages without credentials. There is no automatic timer or background scanning.

Both `GET /v1/incidents` and `GET /v1/incidents/{incident_id}` require `merchant_id`; the detail response returns generic 404 for both absent and out-of-scope IDs. Both use `limit` (default 20, maximum 100) and optional `cursor`. Detail limits observation history, with the opening observation always included separately.

Cursors are canonical URL-safe base64 JSON with an explicit version, merchant, list/detail subject, last ID and fixed upper ID. Validation rejects noncanonical encoding, extra fields, wrong types, invalid ranges and mismatched scopes. Ascending immutable IDs plus the captured upper bound make traversal complete without duplicates when new rows arrive. Existing incident status may change during traversal; pagination freezes membership, not a historical snapshot of mutable lifecycle state. Cursors are not signed authorization tokens: merchant filtering is not authentication, and real access control is required before public exposure.

No production performance, detector accuracy, recovered revenue or tamper-proof audit claims are made.
