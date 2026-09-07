"""Explicit local recommendations: python -m payrecover.planning_cli --help."""

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Never
from uuid import UUID

from pydantic import BaseModel

from payrecover.config import load_settings
from payrecover.domain.incidents import utc, validate_merchant
from payrecover.domain.planning import PlanningConflict, PlanningNotFound
from payrecover.domain.repositories import UnitOfWork
from payrecover.infrastructure.database.session import build_engine, build_session_factory
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork
from payrecover.services.planning import diagnose_incident, list_candidates, plan_recovery


class PlanningParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse's default error can echo a private identifier supplied as a malformed UUID.
        self.exit(2, "Invalid planning command arguments; use --help\n")


def positive_id(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 2**63 - 1:
        raise argparse.ArgumentTypeError("ID must be a positive BIGINT")
    return parsed


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    factory: Callable[[], AbstractContextManager[UnitOfWork]] | None = None,
) -> int:
    parser = PlanningParser(description="Historical advice; never execution authorization")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("diagnose", "candidates", "plan", "show"):
        child = sub.add_parser(command)
        child.add_argument("--merchant-id", required=True)
        if command == "show":
            child.add_argument("--plan-id", type=UUID, required=True)
        else:
            child.add_argument("--incident-id", type=positive_id, required=True)
            child.add_argument("--observation-id", type=positive_id, required=True)
        if command == "plan":
            child.add_argument("--payment-event-id", type=UUID, required=True)
        if command == "candidates":
            child.add_argument("--limit", type=int, default=20)
            child.add_argument("--cursor")
    args = parser.parse_args(argv)
    engine = None
    try:
        validate_merchant(args.merchant_id)
        now = utc(clock())
        if factory is None:
            engine = build_engine(load_settings().require_database_url())
            sessions = build_session_factory(engine=engine)

            def factory() -> SqlAlchemyUnitOfWork:
                return SqlAlchemyUnitOfWork(sessions)

        with factory() as uow:
            result: BaseModel
            if args.command == "diagnose":
                result = diagnose_incident(
                    uow, args.merchant_id, args.incident_id, args.observation_id, now=now
                )
            elif args.command == "plan":
                result = plan_recovery(
                    uow,
                    args.merchant_id,
                    args.incident_id,
                    args.observation_id,
                    args.payment_event_id,
                    now=now,
                )
            elif args.command == "candidates":
                result = list_candidates(
                    uow,
                    args.merchant_id,
                    args.incident_id,
                    args.observation_id,
                    args.limit,
                    args.cursor,
                    now=now,
                )
            else:
                result = uow.planning.get_plan(args.merchant_id, args.plan_id)
        print(result.model_dump_json())
        return 0
    except (PlanningConflict, PlanningNotFound, ValueError):
        print("Invalid, conflicting or unavailable planning subject/input", file=sys.stderr)
        return 2
    except Exception:
        print("Planning command could not be completed", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
