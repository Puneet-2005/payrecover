"""Versioned deterministic inputs. Hidden truth is consumed only by the model."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from payrecover.domain.models import PaymentEvent, PaymentStatus

EPOCH = datetime(2026, 9, 8, 12, tzinfo=UTC)
VERSIONS = {
    "generator": "recovery-demo-v1",
    "fixture": "synthetic-world-v1",
    "eligibility": "simulation-eligibility-v1",
    "detector": "degradation-v2",
    "planning": "recovery-planning-v1",
    "diagnosis": "diagnosis-v1",
    "runner": "simulation-runner-v1",
    "report": "simulation-report-v1",
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def manifest(seed: int, size: int) -> dict[str, Any]:
    if size not in (1000, 50_000):
        raise ValueError("Size must be 1000 (tests) or 50000 (acceptance)")
    root = Path(__file__).resolve().parents[1]
    code = [
        (
            str(p.relative_to(root)).replace("\\", "/"),
            hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        )
        for p in sorted(root.rglob("*.py"))
    ]
    return {
        "versions": VERSIONS,
        "seed": seed,
        "size": size,
        "epoch": EPOCH.isoformat(),
        "code_sha256": fingerprint(code),
    }


@dataclass(frozen=True)
class Delivery:
    key: str
    identity: str
    payment: str
    stage: int
    duplicate: bool
    event: PaymentEvent


def fixtures(seed: int, payment: str) -> dict[str, Any]:
    number = int(payment.removeprefix("p"))
    choice = int(fingerprint([seed, payment])[:8], 16)
    return {
        "version": VERSIONS["fixture"],
        "visible": {
            "consent": number % 11 != 0,
            "flow": number % 13 != 0,
            "status": "unknown" if number % 17 == 0 else "failed",
        },
        "hidden": {
            "restored_at": (EPOCH + timedelta(seconds=2220 + choice % 120)).isoformat(),
            "persistent": choice % 7 == 0,
            "response_lost": choice % 3 == 0,
            "reconcilable": choice % 9 != 0,
            "reconcile_seconds": 60,
        },
    }


def traffic(seed: int, size: int, merchant: str) -> tuple[Delivery, ...]:
    """Exactly size unique events, plus explicit duplicate deliveries and history events.

    The final 600 events contain four 150-event windows (normal, outage, normal, normal).
    Baseline traffic is spread over seven days. Sparse cohort remains intentionally ineligible.
    """
    manifest(seed, size)
    rows: list[Delivery] = []
    baseline = size - 600
    for i in range(size):
        stage = -1 if i < baseline else (i - baseline) // 150
        local = i if stage == -1 else (i - baseline) % 150
        group = "sparse" if local % 50 == 49 else "control" if local % 3 == 0 else "affected"
        degraded = stage == 1 and group != "control"
        failed = (degraded and local % 5 != 0) or (local % 47 == 0)
        label = (
            "incorrect_pin"
            if local % 19 == 0
            else "insufficient_funds"
            if local % 17 == 0
            else "UNMAPPED"
            if local % 13 == 0
            else "issuer_unavailable"
            if local % 11 == 0
            else "network_timeout"
        )
        at = (
            EPOCH - timedelta(days=7) + timedelta(seconds=i * 604000 // baseline)
            if stage == -1
            else EPOCH + timedelta(minutes=stage * 15, seconds=local * 5)
        )
        payment = f"p{i:06d}"
        event = PaymentEvent(
            payment_id=payment,
            merchant_id=merchant,
            method="card",
            issuer=group,
            provider="synthetic",
            amount_paise=10_000 + int(fingerprint([seed, i])[:4], 16) % 1000,
            status=PaymentStatus.FAILED if failed else PaymentStatus.SUCCESS,
            error_code=label if failed else None,
            latency_ms=0,
            occurred_at=at,
        )
        rows.append(Delivery(f"event:{i}", f"event:{i}", payment, stage, False, event))
        if i % 101 == 0:
            rows.append(Delivery(f"duplicate:{i}", f"event:{i}", payment, stage, True, event))
        if stage == 1 and failed and local % 7 == 0:
            # Contradictory history is distinct ingestion, not simulator-generated success.
            change: dict[str, Any] = (
                {"amount_paise": event.amount_paise + 1}
                if local % 2
                else {"status": "success", "error_code": None}
            )
            extra = PaymentEvent.model_validate({**event.model_dump(), **change})
            rows.append(Delivery(f"history:{i}", f"history:{i}", payment, stage, False, extra))
        if stage == 1 and failed and local % 23 == 0:
            extra = PaymentEvent.model_validate(
                {**event.model_dump(), "status": "success", "error_code": None}
            )
            rows.append(Delivery(f"late:{i}", f"late:{i}", payment, 2, False, extra))
    return tuple(sorted(rows, key=lambda d: (d.stage, d.event.occurred_at, d.key)))


def model(hidden: dict[str, Any], reserved_at: datetime) -> dict[str, Any]:
    """Pure underlying truth; neither eligibility nor scheduling receives hidden fixtures."""
    success = not hidden["persistent"] and reserved_at >= datetime.fromisoformat(
        hidden["restored_at"]
    )
    return {
        "truth": "succeeded" if success else "failed",
        "observed": "uncertain"
        if hidden["response_lost"]
        else "succeeded"
        if success
        else "failed",
    }
