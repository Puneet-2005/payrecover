from decimal import Decimal

import pytest

from payrecover.domain.dashboard import PageCursor, inr, position, wire
from payrecover.infrastructure.database.dashboard_reads import dashboard_engine


def test_lossless_dashboard_wire_and_money():
    assert wire({"id": 2**63 - 1, "decimal": Decimal("0.1234567890123456789"), "flag": False}) == {
        "id": "9223372036854775807",
        "decimal": "0.1234567890123456789",
        "flag": False,
    }
    assert inr(9223372036854775807) == "INR 92,233,720,368,547,758.07"
    assert inr(1) == "INR 0.01"
    assert inr(None) == "Indeterminate"


@pytest.mark.parametrize("value", ["", "!", "e30", "a" * 2049])
def test_malformed_dashboard_cursor(value):
    with pytest.raises(ValueError):
        position(value, "a")


def test_dashboard_cursor_scope():
    cursor = PageCursor(scope="a:payments", after="p1").encode()
    assert position(cursor, "a:payments") == "p1"
    with pytest.raises(ValueError):
        position(cursor, "b:payments")
    with pytest.raises(ValueError):
        position(PageCursor(scope="a:payments", after="\x00").encode(), "a:payments")


def test_dashboard_configuration_has_no_normal_database_fallback(monkeypatch):
    monkeypatch.delenv("PAYRECOVER_SIMULATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/production")
    with pytest.raises(ValueError, match="Dedicated"):
        dashboard_engine()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "postgresql+psycopg://localhost:5433/production",
        "postgresql+psycopg://localhost:5432/payrecover_sim_demo",
        "postgresql+psycopg://localhost:5433/payrecover_sim_demo?options=x",
    ],
)
def test_dashboard_rejects_unsafe_targets(url):
    with pytest.raises(ValueError):
        dashboard_engine(url)
