import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from payrecover.api.dependencies import UnitOfWorkFactory, get_unit_of_work_factory
from payrecover.services.incident_reads import (
    IncidentDetail,
    IncidentNotFound,
    IncidentPage,
    InvalidCursor,
    decode_cursor,
    incident_detail,
    list_incidents,
)

router = APIRouter(prefix="/v1/incidents", tags=["local incident reads"])
logger = logging.getLogger(__name__)
Merchant = Annotated[str, Query(min_length=1, max_length=100, pattern=r".*\S.*")]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(max_length=1024)]
Factory = Annotated[UnitOfWorkFactory, Depends(get_unit_of_work_factory)]


@router.get("", response_model=IncidentPage)
def get_incidents(
    merchant_id: Merchant,
    factory: Factory,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
) -> IncidentPage:
    try:
        decode_cursor(cursor, merchant_id, 0)
        with factory() as uow:
            return list_incidents(uow.incidents, merchant_id, limit, cursor)
    except InvalidCursor as exc:
        raise HTTPException(422, "Invalid pagination cursor") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Incident read failed")
        raise HTTPException(503, "Incident data is unavailable") from exc


@router.get("/{incident_id}", response_model=IncidentDetail)
def get_incident(
    incident_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
    merchant_id: Merchant,
    factory: Factory,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
) -> IncidentDetail:
    try:
        decode_cursor(cursor, merchant_id, incident_id)
        with factory() as uow:
            return incident_detail(uow.incidents, merchant_id, incident_id, limit, cursor)
    except InvalidCursor as exc:
        raise HTTPException(422, "Invalid pagination cursor") from exc
    except IncidentNotFound as exc:
        raise HTTPException(404, "Incident not found") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Incident read failed")
        raise HTTPException(503, "Incident data is unavailable") from exc
