from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from payrecover.api.dependencies import UnitOfWorkFactory, get_settings, get_unit_of_work_factory
from payrecover.config import Settings
from payrecover.services.ingestion import (
    IdempotencyConflict,
    IngestionUnavailable,
    append_idempotency_conflict_audit,
    ingest_operational_payment_event,
)
from payrecover.webhooks.razorpay import (
    MAX_WEBHOOK_BODY_BYTES,
    MalformedSupportedWebhook,
    MalformedWebhookJson,
    UnsupportedWebhook,
    normalize_webhook,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_VISIBLE_ASCII = re.compile(r"^[\x21-\x7e]{1,128}$")
_CONTENT_LENGTH = re.compile(r"^[0-9]+$")


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) != 1:
        return None
    return values[0]


def _validate_request_headers(request: Request) -> tuple[str, str]:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Webhook requires application/json",
        )
    media_type = content_types[0].split(";", maxsplit=1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Webhook requires application/json",
        )

    signature = _single_header(request, "x-razorpay-signature")
    if signature is None or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    source_event_id = _single_header(request, "x-razorpay-event-id")
    if source_event_id is None or _VISIBLE_ASCII.fullmatch(source_event_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook request",
        )

    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Content-Length",
        )
    if content_lengths:
        declared_length = content_lengths[0]
        if _CONTENT_LENGTH.fullmatch(declared_length) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Content-Length",
            )
        if int(declared_length) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook body is too large",
            )
    return signature, source_event_id


async def _read_limited_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook body is too large",
            )
    return bytes(body)


@router.post("/v1/webhooks/razorpay")
async def ingest_razorpay_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    unit_of_work_factory: Annotated[UnitOfWorkFactory, Depends(get_unit_of_work_factory)],
) -> Response:
    signature, source_event_id = _validate_request_headers(request)
    try:
        webhook_secret = settings.require_razorpay_webhook_secret()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook ingestion is unavailable",
        ) from exc

    raw_body = await _read_limited_body(request)
    if not verify_webhook_signature(raw_body, signature, webhook_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        event = normalize_webhook(raw_body, source_event_id)
    except MalformedWebhookJson as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed webhook JSON",
        ) from exc
    except MalformedSupportedWebhook as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Malformed supported webhook",
        ) from exc
    except UnsupportedWebhook:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        with unit_of_work_factory() as unit_of_work:
            result = ingest_operational_payment_event(event, unit_of_work)
    except IdempotencyConflict as conflict:
        try:
            with unit_of_work_factory() as conflict_unit_of_work:
                conflict_audit = append_idempotency_conflict_audit(
                    conflict, conflict_unit_of_work
                )
        except IngestionUnavailable as exc:
            logger.error("Razorpay webhook conflict audit transaction failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Webhook event could not be persisted",
            ) from exc
        logger.warning(
            "Razorpay webhook idempotency conflict acknowledged",
            extra={
                "correlation_id": str(conflict_audit.correlation_id),
                "provider_event_id_sha256": conflict_audit.provider_event_id_sha256,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "conflict_acknowledged"},
        )
    except IngestionUnavailable as exc:
        logger.error("Razorpay webhook payment-event transaction failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook event could not be persisted",
        ) from exc

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "accepted" if result.created else "duplicate"},
    )
