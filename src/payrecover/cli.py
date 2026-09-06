"""Local scan command: python -m payrecover.cli --merchant-id ID [--as-of RFC3339]."""

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from pydantic import TypeAdapter

from payrecover.config import load_settings
from payrecover.domain.incidents import ScanResult, utc, validate_merchant
from payrecover.domain.repositories import UnitOfWork
from payrecover.infrastructure.database.session import build_engine, build_session_factory
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork
from payrecover.services.incidents import StaleScan, scan_merchant


def parse_as_of(value: str) -> datetime:
    if (
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
            r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
            value,
        )
        is None
    ):
        raise argparse.ArgumentTypeError("as-of must be an aware RFC3339 timestamp")
    try:
        return utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("as-of must be an aware RFC3339 timestamp") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    factory: Callable[[], AbstractContextManager[UnitOfWork]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Scan one merchant's completed payment window")
    parser.add_argument("--merchant-id", required=True)
    parser.add_argument("--as-of", type=parse_as_of)
    args = parser.parse_args(argv)
    engine = None
    try:
        now = utc(clock())
        validate_merchant(args.merchant_id)
        as_of = args.as_of or now
        if as_of > now:
            print("as-of must not be in the future", file=sys.stderr)
            return 2
        if factory is None:
            engine = build_engine(load_settings().require_database_url())
            session_factory = build_session_factory(engine=engine)

            def factory() -> SqlAlchemyUnitOfWork:
                return SqlAlchemyUnitOfWork(session_factory)

        with factory() as uow:
            result = scan_merchant(args.merchant_id, as_of, uow, now=now)
        print(TypeAdapter(ScanResult).dump_json(result).decode())
        return 0
    except StaleScan:
        print("Unseen scan window is older than the latest committed scan", file=sys.stderr)
        return 2
    except Exception:
        print("Incident scan could not be completed", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
