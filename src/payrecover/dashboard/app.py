from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from payrecover.domain.dashboard import DashboardNotFound, wire
from payrecover.infrastructure.database.dashboard_reads import (
    DashboardReads,
    dashboard_engine,
    read_session,
    validate,
)

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=2048)]
ASSETS = Path(__file__).parent / "static"


def create_app(database_url: str | None = None) -> FastAPI:
    engine = dashboard_engine(database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            with read_session(engine) as session:
                validate(session)
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="PayRecover simulation explorer",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.dashboard_engine = engine

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def read(method: str, *args):
        try:
            with read_session(engine) as session:
                validate(session)
                return JSONResponse(wire(getattr(DashboardReads(session), method)(*args)))
        except DashboardNotFound as exc:
            raise HTTPException(404, "Simulation evidence not found") from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid dashboard input or cursor") from exc
        except Exception as exc:
            raise HTTPException(503, "Simulation evidence is unavailable") from exc

    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/demo/v1/runs")
    def runs(limit: Limit = 20, cursor: Cursor = None):
        return read("runs", limit, cursor)

    @app.get("/demo/v1/runs/{run_id}")
    def overview(run_id: UUID):
        return read("overview", run_id)

    @app.get("/demo/v1/runs/{run_id}/evaluations")
    def evaluations(run_id: UUID, limit: Limit = 20, cursor: Cursor = None):
        return read("steps", run_id, limit, cursor)

    @app.get("/demo/v1/runs/{run_id}/incidents")
    def incidents(run_id: UUID, limit: Limit = 20, cursor: Cursor = None):
        return read("incidents", run_id, limit, cursor)

    @app.get("/demo/v1/runs/{run_id}/incidents/{incident_id}")
    def incident(run_id: UUID, incident_id: int, limit: Limit = 20, cursor: Cursor = None):
        if not 1 <= incident_id <= 2**63 - 1:
            raise HTTPException(422, "Invalid incident ID")
        return read("incident", run_id, incident_id, limit, cursor)

    @app.get("/demo/v1/runs/{run_id}/observations/{observation_id}/diagnosis")
    def diagnosis(run_id: UUID, observation_id: int):
        if not 1 <= observation_id <= 2**63 - 1:
            raise HTTPException(422, "Invalid observation ID")
        return read("diagnosis", run_id, observation_id)

    @app.get("/demo/v1/runs/{run_id}/payments")
    def payments(run_id: UUID, limit: Limit = 20, cursor: Cursor = None):
        return read("payments", run_id, limit, cursor)

    @app.get("/demo/v1/runs/{run_id}/payments/{payment}")
    def payment_detail(run_id: UUID, payment: str):
        if not 1 <= len(payment) <= 100:
            raise HTTPException(422, "Invalid payment identity")
        return read("payment_detail", run_id, payment)

    @app.get("/demo/v1/runs/{run_id}/payments/{payment}/history")
    def history(run_id: UUID, payment: str, limit: Limit = 20, cursor: Cursor = None):
        if not 1 <= len(payment) <= 100:
            raise HTTPException(422, "Invalid payment identity")
        return read("steps", run_id, limit, cursor, payment)

    app.mount("/static", StaticFiles(directory=ASSETS), name="static")
    return app
