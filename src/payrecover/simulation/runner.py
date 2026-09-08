from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Engine

from payrecover.infrastructure.database.session import build_session_factory
from payrecover.infrastructure.database.simulation import (
    SimulationStore,
    run_lock,
    validate_database,
)
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork
from payrecover.services.analytics import analyze_payment_cohorts
from payrecover.services.incidents import scan_merchant
from payrecover.services.ingestion import ingest_payment_event
from payrecover.services.planning import diagnose_incident, list_candidates, plan_recovery
from payrecover.simulation.reporting import report
from payrecover.simulation.scenario import EPOCH, fixtures, manifest, model, traffic


def run(
    engine: Engine,
    key: str,
    seed: int = 42,
    size: int = 50_000,
    *,
    fail: Callable[[str], None] = lambda boundary: None,
) -> dict[str, Any]:
    validate_database(engine)
    if not key or len(key) > 100:
        raise ValueError("Invalid run key")
    run_id = uuid5(NAMESPACE_URL, f"payrecover-simulation:{key}")
    merchant = f"sim_{run_id.hex}"
    store = SimulationStore(engine, run_id)
    configuration = manifest(seed, size)
    sessions = build_session_factory(engine=engine)
    with run_lock(engine, run_id):
        previous = store.start(merchant, configuration)
        if previous is not None:
            return previous
        rows = traffic(seed, size, merchant)
        done = store.steps()
        for stage in range(-1, 4):
            if f"stage:{stage}" in done:
                continue
            now = EPOCH + timedelta(minutes=(stage + 1) * 15 + 7)
            for delivery in (d for d in rows if d.stage == stage):
                checkpoint = f"delivery:{delivery.key}"
                if checkpoint in done:
                    continue
                with SqlAlchemyUnitOfWork(sessions) as uow:
                    ingest_payment_event(delivery.event, delivery.identity, uow)
                fail("after_ingestion_commit")
                if stage == 1 and delivery.event.status == "failed" and not delivery.duplicate:
                    with SqlAlchemyUnitOfWork(sessions) as uow:
                        stored = uow.payment_events.get_by_source_identity(
                            "normalized_api", merchant, delivery.identity
                        )
                        assert stored is not None
                        store.population(merchant, stored.id, fixtures(seed, delivery.payment))
                data = {"duplicate": delivery.duplicate, "payment": delivery.payment}
                store.append(checkpoint, "delivery", now, data)
                done[checkpoint] = data
            if stage >= 0:
                # Frozen stage: no ingestion resumes until analysis, scanning and all pages finish.
                if f"analysis:{stage}" not in done:
                    with SqlAlchemyUnitOfWork(sessions) as uow:
                        values = analyze_payment_cohorts(merchant, now, uow.payment_analytics)
                    for value in values.cohorts:
                        group = value.aggregate.cohort.issuer.value
                        store.append(
                            f"evaluation:{stage}:{group}",
                            "evaluation",
                            now,
                            {
                                "cohort": group,
                                "stage": stage,
                                "label": stage == 1 and group != "control",
                                "outcome": value.detection.outcome.value,
                                "delay_seconds": int(
                                    (now - (EPOCH + timedelta(minutes=15))).total_seconds()
                                ),
                            },
                        )
                    store.append(f"analysis:{stage}", "checkpoint", now, {})
                with SqlAlchemyUnitOfWork(sessions) as uow:
                    scan_merchant(merchant, now, uow, now=now)
                if f"planning:{stage}" not in done:
                    with SqlAlchemyUnitOfWork(sessions) as uow:
                        incidents, _ = uow.incidents.list_incidents(merchant, 0, None, 100)
                    population = {p.event_id: p for p in store.payments()}
                    for incident in incidents:
                        with SqlAlchemyUnitOfWork(sessions) as uow:
                            observations, _ = uow.incidents.observations(
                                merchant, incident.id, 0, None, 100
                            )
                        for observation in observations:
                            if observation.window.observation_start != EPOCH + timedelta(
                                minutes=stage * 15
                            ):
                                continue
                            with SqlAlchemyUnitOfWork(sessions) as uow:
                                diagnose_incident(
                                    uow, merchant, incident.id, observation.id, now=now
                                )
                            cursor = None
                            while True:
                                with SqlAlchemyUnitOfWork(sessions) as uow:
                                    page = list_candidates(
                                        uow,
                                        merchant,
                                        incident.id,
                                        observation.id,
                                        100,
                                        cursor,
                                        now=now,
                                    )
                                for item in page.items:
                                    p = population.get(item.payment_event_id)
                                    if p is None:
                                        continue
                                    with SqlAlchemyUnitOfWork(sessions) as uow:
                                        result = plan_recovery(
                                            uow,
                                            merchant,
                                            incident.id,
                                            observation.id,
                                            p.event_id,
                                            now=now,
                                        )
                                    fail("after_planning_commit")
                                    store.attach_plan(
                                        merchant, p.payment_id, result.recommendation.id
                                    )
                                cursor = page.next_cursor
                                if cursor is None:
                                    break
                    store.append(f"planning:{stage}", "checkpoint", now, {})
                if stage >= 1:
                    simulate(store, merchant, now + timedelta(minutes=15), fail)
            store.append(f"stage:{stage}", "checkpoint", now, {})
        completed = report(store, merchant)
        completed["manifest"] = configuration
        store.finish(completed)
        return completed


def simulate(
    store: SimulationStore, merchant: str, cutoff: datetime, fail: Callable[[str], None]
) -> None:
    # Public backoff and reconciliation interval, never hidden restoration times.
    sessions = build_session_factory(engine=store.engine)
    done = store.steps()
    for p in store.payments():
        with SqlAlchemyUnitOfWork(sessions) as uow:
            plan = uow.planning.get_plan(merchant, p.plan_id) if p.plan_id else None
        for ordinal in (1, 2):
            records = store.attempts(p.payment_id)
            if ordinal == 2 and not records:
                break
            delay = plan.retry_after_seconds or 0 if plan else 0
            at = (
                (plan.created_at if plan else EPOCH + timedelta(minutes=37))
                + timedelta(seconds=delay)
                if ordinal == 1
                else records[0].reserved_at + timedelta(seconds=max(60, delay * 2))
            )
            if at + timedelta(seconds=60) > cutoff:
                continue
            checkpoint = f"dispatch:{p.payment_id}:{ordinal}"
            if checkpoint in done:
                continue
            if not store.reserve(merchant, p.payment_id, ordinal, at):
                store.append(checkpoint, "checkpoint", at, {})
                break
            fail("after_reservation_commit")
            attempt = store.attempts(p.payment_id)[ordinal - 1]
            value = model(p.fixture["hidden"], attempt.reserved_at)
            fail("after_model_outcome")
            store.outcome(p.payment_id, ordinal, value, at)
            fail("after_outcome_commit")
            # Explicit injected duplicate, distinct from crash/resume reprocessing.
            assert store.reserve(merchant, p.payment_id, ordinal, at)
            store.append(
                f"suppressed:{p.payment_id}:{ordinal}",
                "suppression",
                at,
                {"payment": p.payment_id, "ordinal": ordinal},
            )
            store.reconcile(p.payment_id, ordinal, at + timedelta(seconds=60))
            store.append(checkpoint, "checkpoint", at + timedelta(seconds=60), {})
