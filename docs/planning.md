# Phase 3B: evidence-based recommendations

This local-development feature records deterministic diagnoses and recovery advice. It does not
execute payments, notify customers, schedule work, use Redis or call a provider or AI service.
Every recommendation has `execution_authorized=false`, enforced by a PostgreSQL check as well as
the domain contract. Existing HTTP routes and `decide_recovery()` behavior are unchanged.

## Evidence and opening incidents

The actual Phase 3A2 schema uses BIGINT incident, observation and audit IDs; payment-event and scan
IDs are UUIDs. The opening scan already inserts an `incident_observations` row and its ranked error
counts. Its scan ID equals the incident's `opened_scan_id`. Diagnosis therefore works immediately
with that observation ID: no second scan, invented observation or alternative evidence ID is needed.
A composite foreign key binds each diagnosis to the observation, incident and merchant together.

`diagnosis-v1` reads the immutable observation and retained top-20 error counts. A recognized label
must have a strict majority of **all** failed events (`2 * supporting_count > failed_count`).
Missing codes and failures outside the top 20 remain in the denominator. The retained and
unrepresented counts are recorded separately. Insufficient observations and zero failures yield
`insufficient_evidence`; ties, mixed and unmapped signals yield `unknown`.

| Reported label | Probable interpretation |
| --- | --- |
| `network_timeout` | transient network failure |
| `issuer_unavailable` | issuer unavailability |
| `insufficient_funds` | customer funding issue |
| `incorrect_pin` | authentication failure |

Observed labels/counts are facts about stored evidence; the category is an inference, not proof of
causation. The majority threshold is a deterministic rule, not a calibrated probability. There is
no confidence score. Aggregate evidence does not retain source provenance or error reasons, so this
diagnosis interprets only the four literal internal labels. It does not translate provider codes,
infer causes from descriptions or establish a provider outage. Generic Razorpay codes remain unknown.
The original detector outcome is retained: classifying failure labels does not declare a new outage.

## Individual association and policy

Candidates must be failed events matching the persisted incident's merchant, method, issuer/provider
values and availability states, and exact cohort-v2 amount band. Nullable values use null-safe SQL
equality. The window is half-open: `observation_start <= occurred_at < observation_end`. Legacy
`payment_events.cohort_key` is ignored. SQL applies all predicates and the page limit.

`association_evaluated_at` is explicit on candidate output and stored plans. It uses the command's
single injected UTC evaluation time, sampled before transaction work. Membership means cohort/window
membership evaluated during planning, **not membership in the original scanner's database snapshot**.
Late-arriving events may qualify. No comparison of ingestion timestamps can reconstruct that old
snapshot; this feature does not attempt one.

`recovery-planning-v1` uses the individual event's reviewed signal, never the cohort diagnosis to
authorize or choose its retry. Only exact internal labels from `normalized_api` with provided error
availability are reviewed. All provider signals, including identical strings from
`razorpay_webhook`, are unmapped for individual planning and escalate. No provider mappings or
network calls were added.

| Individual rule | Action | Minimum delay | Attempt ceiling |
| --- | --- | --- | --- |
| Any stored success for this identity | `no_action` | none | 0 |
| Contradictory history | `escalate` | none | 0 |
| `incorrect_pin` | `no_action` | none | 0 |
| `insufficient_funds` | `notify_customer` (advice only) | none | 0 |
| `network_timeout` | `retry_now` | 30 seconds | 2 |
| `issuer_unavailable` | `wait_and_retry` | 480 seconds | 2 |
| Unmapped/missing signal | `escalate` | none | 0 |

`RETRY_NOW` retains the existing enum name but means a recommendation subject to minimum backoff,
not immediate dispatch. Delays and ceilings are advisory bounds, not schedules or verified attempt
counts. Retry advice has decision `blocked`; terminal decisions are `no_action` or `manual_review`.
The legacy policy still escalates PIN/funding signals at attempt zero because its stopping rule
precedes terminal handling. Regression tests preserve that HTTP behavior; the new planning policy
handles terminal rules explicitly.

History is scoped to `(merchant_id, source, payment_id)` across all stored timestamps. Integrity
checks first resolve the supplied local event ID under the authoritative merchant. Missing or
foreign events and mismatched source/payment identities are rejected. The individual signal and
history identity come from persisted fields, never a caller-supplied signal. Integrity
blockers are: both success and failure present; differing amount, method, issuer/provider value or
availability; or more than one distinct reviewed failure signal. Repeated identical failures alone
are not contradictory. Different source namespaces and merchants are not merged. Any stored success
prevents a new retry recommendation regardless of occurrence or ingestion order. None of these rules
establishes authoritative current provider status.

Every plan retains missing consent, unverified provider flow, unreconciled payment status and
unverified attempt history as prerequisites. They cannot be supplied as satisfied through the CLI.
The history aggregate observes a database statement snapshot; concurrent ingestion may add new
facts afterward. Future execution must independently revalidate all prerequisites and status.

## Transactions, immutable replay and history

Standalone `diagnose` validates merchant-scoped evidence, acquires the existing merchant transaction
lock, checks `(merchant, observation, diagnosis_version)`, and inserts a diagnosis and
`incident.diagnosed` audit in one commit. Exact replay returns the original diagnosis and adds
nothing. Failure rolls back both records.

`plan` takes the same merchant lock, validates the association and checks the unique identity
`(merchant, source, payment_id, policy_version)` before inspecting changing payment history.
Exact replay returns the original recommendation, timestamps, evidence and decision with
`replayed=true`, without new records/audits. A different event or observation association for the
same payment/policy identity is rejected. Duplicate events cannot create another same-version plan.
Historical advice is not refreshed eligibility, even when a later success would now prevent retry.

A new plan creates or reuses its diagnosis, evaluates history and policy, then commits the plan and
`recovery_plan.created` audit together. Audit or commit failure rolls back all newly created records.
Repositories flush but never commit. Merchant locks serialize cooperating planners and scanners;
database unique constraints reject duplicate inserts even outside that orchestration. An unexpected
database race/failure is reported unavailable and rolls back; rerunning the command starts a fresh
unit of work and resolves the committed identity. There is no background retry loop.

Plans and diagnoses have one state: recorded immutable history. There is no approval, dispatch,
execution or completion transition, automatic supersession or same-version reassessment. Repositories
expose no update/delete operations and ordinary ORM mutation/deletion is rejected. Bulk or privileged
direct SQL is outside application append-only protection. Digests identify canonical evidence;
they are not signatures or a tamper-proof audit chain.

## Schema and migration

Revision `20260907_0005` follows `20260905_0004` and fits Alembic's existing version column. Its static
DDL does not import application model definitions and never calls `metadata.create_all()`.

`incident_diagnoses` has a UUID PK, BIGINT incident/observation references, merchant and version,
outcome/reason, BIGINT supporting/total counts, strict JSONB evidence, SHA-256 and UTC timestamp.
`recovery_plans` has a UUID PK, matching incident/observation/diagnosis/payment references, private
source/payment identity, policy version, action/decision/reason, optional integer delay, SMALLINT
attempt ceiling, false execution flag, strict JSONB prerequisites/evidence, SHA-256 and UTC creation
and association timestamps. No monetary value is converted to float or duplicated into a plan;
candidate amounts use the existing exact BIGINT integer-paise value.

Composite restrictive FKs bind diagnosis to observation/incident/merchant and plan to both diagnosis
and payment-event identity. Added parent unique keys enable those FKs. Checks enforce supported
categories, versions, count/delay/attempt bounds, terminal action bounds, JSON object shapes, digest
format and `execution_authorized=false`. Strict application schemas validate nested evidence and
prerequisites. Cross-table cohort/time membership is validated transactionally in the repository,
not claimed as a PostgreSQL CHECK invariant.

History indexes are `(merchant_id, incident_id, created_at, id)` on diagnoses and plans, plus plan
diagnosis/payment-event reference indexes. Audits add nullable UUID diagnosis/plan subjects, composite
restrictive FKs and partial history indexes. The updated subject check preserves all existing payment,
incident and permitted subjectless audits. New audit details contain only a schema version, rule
version and evidence digest; immutable linked records hold the decision and evidence.

Downgrade refuses before DDL if either planning table or a planning audit reference contains history.
It does not delete audit rows or remove subjects. With no planning history, downgrade restores the
Phase 3A2 audit check and drops only the Phase 3B extension. Tests use disposable PostgreSQL 17 for
upgrade/downgrade/re-upgrade, existing-data preservation and refusal/schema preservation.

## Local CLI

After migration of your intended development database:

```text
python -m payrecover.planning_cli diagnose --merchant-id MERCHANT --incident-id 1 --observation-id 1
python -m payrecover.planning_cli candidates --merchant-id MERCHANT --incident-id 1 --observation-id 1 --limit 20
python -m payrecover.planning_cli plan --merchant-id MERCHANT --incident-id 1 --observation-id 1 --payment-event-id LOCAL_UUID
python -m payrecover.planning_cli show --merchant-id MERCHANT --plan-id PLAN_UUID
```

Use the existing incident detail interface to obtain observation IDs. Candidate output supplies local
payment-event UUIDs and `next_cursor`; pass that token as `--cursor` with the same scope. Pages use
ascending UUID keyset ordering, SQL `LIMIT limit+1`, a 1-100 limit and canonical scope-validated cursors.
This is a live traversal, not a frozen membership snapshot: arrivals behind the cursor require a new
traversal. Read-only `candidates`/`show` commands do not append audits and close without writes.

JSON output displays `retry_after_seconds` and `execution_authorized=false`. It excludes raw provider
payment/event identifiers, raw webhook bodies, descriptions, signatures and customer information.
Errors are generic and use nonzero exit codes. The CLI requires trusted local database access; merchant
filters are not authentication, and existing development APIs must not be publicly exposed.

## Deferred provider requirements

No generic Razorpay failed-payment retry API is assumed. Before any later execution phase, verify
official documentation for the specific product, method, mandate/consent, endpoint, current status,
attempt limits, idempotency and provider-managed retry behavior. Checkout, recurring payments and
Subscriptions are distinct flows. Also review method-specific error semantics before adding mappings.
No execution, notifications, new HTTP routes, AI, scheduling, Redis integration, production
authentication or calibrated confidence is implemented here.
