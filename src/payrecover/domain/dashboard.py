import base64
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DashboardNotFound(LookupError):
    pass


def wire(value: Any) -> Any:
    """Lossless JSON boundary: no BIGINT or paise passes through a JS Number."""
    if isinstance(value, BaseModel):
        return wire(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return wire(asdict(value))
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, Decimal, UUID)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    return value


def inr(paise: int | None) -> str:
    if paise is None:
        return "Indeterminate"
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"INR {sign}{whole:,}.{fraction:02d}"


class PageCursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    scope: str
    after: str = Field(min_length=1, max_length=1600)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def position(cursor: str | None, scope: str) -> str:
    if cursor is None:
        return ""
    try:
        if not 1 <= len(cursor) <= 2048:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        parsed = PageCursor.model_validate(json.loads(raw))
        if parsed.scope != scope or parsed.encode() != cursor or "\x00" in parsed.after:
            raise ValueError
        return parsed.after
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid scoped cursor") from exc
