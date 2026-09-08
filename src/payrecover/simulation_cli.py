"""Explicit dedicated-database setup and synthetic-only scenario command."""

import argparse
import json
import sys

from alembic.config import Config

from alembic import command
from payrecover.infrastructure.database.session import build_engine
from payrecover.infrastructure.database.simulation import (
    configured_url,
    initialize,
    validate_database,
)
from payrecover.simulation.runner import run


def main() -> int:
    parser = argparse.ArgumentParser(description="SIMULATION ONLY: no payment authorization")
    parser.add_argument("command", choices=("initialize", "run"))
    parser.add_argument("--run-key", default="demo-42")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--size", type=int, choices=(1000, 50000), default=50000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    engine = None
    try:
        url = configured_url()
        engine = build_engine(url)
        if args.command == "initialize":
            validate_database(engine, marker=False)
            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
            command.upgrade(config, "head")
            initialize(engine)
            print("Dedicated simulation database initialized")
        else:
            value = run(engine, args.run_key, args.seed, args.size)
            if not args.json:
                print("PAYRECOVER — SIMULATION ONLY. No real money recovered.")
            print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except Exception:
        print(
            "Simulation refused or interrupted; verify isolated setup and resume configuration",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
