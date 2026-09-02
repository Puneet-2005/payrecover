from payrecover.domain.models import RecoveryAction, RecoveryDecision


SAFE_POLICIES: dict[str, RecoveryDecision] = {
    "network_timeout": RecoveryDecision(action=RecoveryAction.RETRY_NOW, permitted=True,
        retry_after_seconds=30, max_attempts=2, reason="transient network failure"),
    "issuer_unavailable": RecoveryDecision(action=RecoveryAction.WAIT_AND_RETRY, permitted=True,
        retry_after_seconds=480, max_attempts=2, reason="wait for issuer health recovery"),
    "insufficient_funds": RecoveryDecision(action=RecoveryAction.NOTIFY_CUSTOMER, permitted=False,
        max_attempts=0, reason="customer action required; automatic retry blocked"),
    "incorrect_pin": RecoveryDecision(action=RecoveryAction.NO_ACTION, permitted=False,
        max_attempts=0, reason="credential failure must never be automatically retried"),
}


def decide_recovery(error_code: str, attempt: int = 0) -> RecoveryDecision:
    decision = SAFE_POLICIES.get(error_code)
    if decision is None:
        return RecoveryDecision(action=RecoveryAction.ESCALATE, permitted=False, max_attempts=0,
                                reason="unknown failure requires human review")
    if attempt >= decision.max_attempts:
        return RecoveryDecision(action=RecoveryAction.ESCALATE, permitted=False, max_attempts=decision.max_attempts,
                                reason="retry stopping rule reached")
    return decision

