from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_phase4_simulation import simulation_database  # noqa: F401

from payrecover.dashboard.app import create_app
from payrecover.domain.dashboard import DashboardNotFound, wire
from payrecover.infrastructure.database.dashboard_reads import DashboardReads, read_session
from payrecover.infrastructure.database.simulation_models import SimulationRun
from payrecover.simulation.runner import run

pytestmark = pytest.mark.integration


def test_dashboard_complete_journey_is_scoped_read_only_and_lossless(simulation_database):  # noqa: F811
    engine, _ = simulation_database
    report = run(engine, "dashboard", size=1000)
    identity = uuid5(NAMESPACE_URL, "payrecover-simulation:dashboard")
    other = uuid4()
    with Session(engine) as session, session.begin():
        original = session.get(SimulationRun, identity)
        session.add(
            SimulationRun(
                id=other,
                merchant_id=f"sim_{other.hex}",
                manifest=original.manifest,
                manifest_sha256=original.manifest_sha256,
                status="running",
                report=None,
            )
        )
    with engine.connect() as connection:
        before = {
            t: connection.scalar(text(f"SELECT count(*) FROM {t}"))
            for t in ("audit_records", "simulation_steps", "recovery_plans", "incident_diagnoses")
        }
        incident = connection.scalar(text("SELECT min(id) FROM incidents"))
        observation = connection.scalar(text("SELECT min(observation_id) FROM incident_diagnoses"))
        planned = connection.scalar(
            text("SELECT payment_id FROM simulation_payments WHERE plan_id IS NOT NULL LIMIT 1")
        )
        unplanned = connection.scalar(
            text("SELECT payment_id FROM simulation_payments WHERE plan_id IS NULL LIMIT 1")
        )
    app = create_app(engine.url.render_as_string(hide_password=False))
    statements = []
    event.listen(
        app.state.dashboard_engine,
        "before_cursor_execute",
        lambda c, cu, statement, p, ctx, many: statements.append(statement),
    )
    base = f"/demo/v1/runs/{identity}"
    other_base = f"/demo/v1/runs/{other}"
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/favicon.ico").status_code == 204
        for asset in ("dashboard.js", "dashboard.css"):
            response = client.get("/static/" + asset)
            assert response.status_code == 200
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert client.get(base).json()["report"] == wire(report)
        assert client.get(other_base).json()["report"] is None
        assert client.post(base).status_code == 405
        for suffix in (
            f"/incidents/{incident}",
            f"/observations/{observation}/diagnosis",
            f"/payments/{planned}",
            f"/payments/{planned}/history",
        ):
            assert client.get(base + suffix).status_code == 200
            assert client.get(other_base + suffix).status_code == 404
        assert client.get(other_base + "/payments").json()["items"] == []
        assert client.get(base + f"/payments/{unplanned}").json()["recommendation"] is None
        advice = client.get(base + f"/payments/{planned}").json()
        assert advice["recommendation"]["execution_authorized"] is False
        assert "Historical advice" in advice["advice_label"]
        all_payments = client.get(base + "/payments?limit=100").json()["items"]
        assert len({p["payment"] for p in all_payments}) == report["accounting"]["population_count"]
        excluded = next(p for p in all_payments if p["category"] == "excluded")
        excluded_detail = client.get(base + f"/payments/{excluded['payment']}").json()
        assert excluded_detail["amount_paise"] is None
        assert excluded_detail["amount_inr"] == "Indeterminate"
        recovered = next(p for p in all_payments if p["category"] == "simulated_recovered")
        page = client.get(base + "/payments?limit=1").json()
        assert len(page["items"]) == 1 and page["next_cursor"]
        second = client.get(
            base + "/payments", params={"limit": 1, "cursor": page["next_cursor"]}
        ).json()
        assert second["items"][0]["payment"] > page["items"][0]["payment"]
        assert (
            client.get(other_base + "/payments", params={"cursor": page["next_cursor"]}).status_code
            == 422
        )
        for suffix in (
            "/payments",
            "/evaluations",
            "/incidents",
            f"/incidents/{incident}",
            f"/payments/{planned}/history",
        ):
            for bad in ("!", "e30", "a" * 2049):
                assert client.get(base + suffix, params={"cursor": bad}).status_code == 422
            assert client.get(base + suffix, params={"limit": 101}).status_code == 422
        evaluations = client.get(base + "/evaluations?limit=1").json()
        next_eval = client.get(
            base + "/evaluations", params={"cursor": evaluations["next_cursor"], "limit": 1}
        ).json()
        assert next_eval["items"][0]["key"] != evaluations["items"][0]["key"]
        for suffix in (f"/payments/{planned}", f"/payments/{planned}/history", "/evaluations"):
            payload = client.get(base + suffix).text
            for forbidden in ('"truth"', '"fixture"', '"restored_at"', '"response_loss"'):
                assert forbidden not in payload
        with read_session(app.state.dashboard_engine) as session:
            assert session.scalar(text("SHOW transaction_read_only")) == "on"
            with pytest.raises(DBAPIError):
                session.execute(text("UPDATE simulation_runs SET status=status"))
        with read_session(app.state.dashboard_engine) as session:
            repo = DashboardReads(session)
            for call in (
                lambda: repo.payment_detail(other, planned),
                lambda: repo.attempts(other, planned),
                lambda: repo.steps(other, 1, None, planned),
                lambda: repo.incident(other, incident, 1, None),
                lambda: repo.diagnosis(other, observation),
            ):
                with pytest.raises(DashboardNotFound):
                    call()
        with engine.connect() as connection:
            assert before == {
                t: connection.scalar(text(f"SELECT count(*) FROM {t}")) for t in before
            }
        # A later evidence change does not rewrite the historical completed report.
        with engine.begin() as connection:
            connection.execute(
                text("""UPDATE payment_events SET amount_paise=:amount
                WHERE merchant_id=:merchant AND payment_id=:payment"""),
                {
                    "amount": 9223372036854775807,
                    "merchant": f"sim_{identity.hex}",
                    "payment": unplanned,
                },
            )
            connection.execute(
                text("""UPDATE simulation_payments SET amount_paise=:amount
                WHERE run_id=:run AND payment_id=:payment"""),
                {"amount": 9223372036854775807, "run": identity, "payment": unplanned},
            )
        changed = client.get(base + f"/payments/{unplanned}").json()
        assert changed["amount_paise"] == "9223372036854775807"
        assert changed["amount_inr"] == "INR 92,233,720,368,547,758.07"
        assert client.get(base).json()["report"] == wire(report)
        # New stored success changes CURRENT classification, never the historical report/advice.
        old_advice = client.get(base + f"/payments/{recovered['payment']}").json()["recommendation"]
        with engine.begin() as connection:
            connection.execute(
                text("""UPDATE payment_events SET status='success'
                WHERE merchant_id=:merchant AND payment_id=:payment"""),
                {"merchant": f"sim_{identity.hex}", "payment": recovered["payment"]},
            )
        refreshed = client.get(base + f"/payments/{recovered['payment']}").json()
        assert refreshed["category"] == "already_successful"
        assert refreshed["classification_label"] == "Current evidence classification"
        assert refreshed["recommendation"] == old_advice
        assert client.get(base).json()["report"] == wire(report)
    assert any("ORDER BY simulation_payments.payment_id" in s and "LIMIT" in s for s in statements)


def test_dashboard_missing_marker_refuses_startup_without_initializing(
    simulation_database,  # noqa: F811
):
    engine, _ = simulation_database
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM simulation_marker"))
    with pytest.raises(ValueError, match="marker"):
        with TestClient(create_app(engine.url.render_as_string(hide_password=False))):
            pass
    with Session(engine) as session:
        assert session.scalar(select(text("count(*)")).select_from(text("simulation_marker"))) == 0
