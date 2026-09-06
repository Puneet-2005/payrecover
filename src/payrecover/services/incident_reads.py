import base64
import binascii
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from payrecover.domain.incidents import Incident, IncidentObservation, IncidentRepository


class InvalidCursor(ValueError):
    pass


class IncidentNotFound(LookupError):
    pass


class Cursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1] = 1
    merchant: str = Field(min_length=1, max_length=100)
    subject: int = Field(ge=0, le=2**63 - 1)
    after: int = Field(ge=0, le=2**63 - 1)
    upper: int = Field(ge=0, le=2**63 - 1)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def decode_cursor(value: str | None, merchant: str, subject: int) -> tuple[int, int | None]:
    if value is None:
        return 0, None
    try:
        if not 1 <= len(value) <= 1024:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = Cursor.model_validate(json.loads(raw))
        if (
            cursor.encode() != value
            or cursor.merchant != merchant
            or cursor.subject != subject
            or cursor.after > cursor.upper
        ):
            raise ValueError
    except (ValueError, ValidationError, binascii.Error, UnicodeError) as exc:
        raise InvalidCursor("Invalid pagination cursor") from exc
    return cursor.after, cursor.upper


@dataclass(frozen=True)
class IncidentPage:
    items: tuple[Incident, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class IncidentDetail:
    incident: Incident
    opening_observation: IncidentObservation
    observations: tuple[IncidentObservation, ...]
    next_cursor: str | None


def list_incidents(
    repository: IncidentRepository,
    merchant: str,
    limit: int,
    cursor: str | None,
) -> IncidentPage:
    after, upper = decode_cursor(cursor, merchant, 0)
    items, upper = repository.list_incidents(merchant, after, upper, limit + 1)
    next_cursor = (
        Cursor(merchant=merchant, subject=0, after=items[limit - 1].id, upper=upper).encode()
        if len(items) > limit
        else None
    )
    return IncidentPage(items[:limit], next_cursor)


def incident_detail(
    repository: IncidentRepository,
    merchant: str,
    incident_id: int,
    limit: int,
    cursor: str | None,
) -> IncidentDetail:
    after, upper = decode_cursor(cursor, merchant, incident_id)
    incident = repository.get_incident(merchant, incident_id)
    if incident is None:
        raise IncidentNotFound("Incident not found")
    observations, upper = repository.observations(merchant, incident_id, after, upper, limit + 1)
    opening, _ = repository.observations(merchant, incident_id, 0, None, 1)
    next_cursor = (
        Cursor(
            merchant=merchant, subject=incident_id, after=observations[limit - 1].id, upper=upper
        ).encode()
        if len(observations) > limit
        else None
    )
    return IncidentDetail(incident, opening[0], observations[:limit], next_cursor)
