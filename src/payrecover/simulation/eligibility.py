from datetime import datetime, timedelta
from typing import Any

from payrecover.domain.planning import PlanEvidence, RecoveryRecommendation
from payrecover.services.policy import decide_recovery_plan


def eligibility(
    plan: RecoveryRecommendation,
    history: PlanEvidence,
    visible: dict[str, Any],
    states: tuple[str, ...],
    now: datetime,
    last_at: datetime | None,
) -> str | None:
    """Return a public blocker, or allow a synthetic reservation. No hidden truth input."""
    current = decide_recovery_plan(history)
    if history.stored_success:
        return "stored_success"
    if current.prerequisites.integrity_blocker:
        return "integrity"
    if plan.action not in ("retry_now", "wait_and_retry") or current.action not in (
        "retry_now",
        "wait_and_retry",
    ):
        return "terminal_or_unknown"
    if visible.get("consent") is not True:
        return "missing_consent"
    if visible.get("flow") is not True:
        return "unsupported_flow"
    if visible.get("status") != "failed":
        return "unresolved_status"
    if "succeeded" in states:
        return "simulated_success"
    if any(state in ("reserved", "uncertain") for state in states):
        return "outstanding_attempt"
    if len(states) >= min(2, plan.max_attempts, current.max_attempts):
        return "attempt_limit"
    delay = max(plan.retry_after_seconds or 0, current.retry_after_seconds or 0)
    due = (last_at or plan.created_at) + timedelta(seconds=delay * (2 if states else 1))
    if now < due:
        return "backoff"
    return None
