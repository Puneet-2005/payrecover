from payrecover.domain.models import RecoveryAction, RecoveryDecision
from payrecover.domain.planning import PlanEvidence, PolicyRecommendation, Prerequisites

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
        return RecoveryDecision(
            action=RecoveryAction.ESCALATE,
            permitted=False,
            max_attempts=decision.max_attempts,
            reason="retry stopping rule reached",
        )
    return decision


def decide_recovery_plan(evidence: PlanEvidence) -> PolicyRecommendation:
    """Planning v1: terminal rules precede backoff rules; never dispatch or authorize."""
    integrity = (evidence.mixed_statuses or evidence.inconsistent_dimensions_or_amount
                 or evidence.conflicting_reviewed_signals)
    prerequisites = Prerequisites(integrity_blocker=integrity)
    if evidence.stored_success:
        return PolicyRecommendation(action=RecoveryAction.NO_ACTION, decision="no_action",
                                    reason_code="stored_success",
                                    prerequisites=prerequisites, max_attempts=0)
    if integrity:
        return PolicyRecommendation(action=RecoveryAction.ESCALATE, decision="manual_review",
                                    reason_code="contradictory_history",
                                    prerequisites=prerequisites, max_attempts=0)
    if evidence.individual_signal == "incorrect_pin":
        return PolicyRecommendation(action=RecoveryAction.NO_ACTION, decision="no_action",
                                    reason_code="authentication_failure",
                                    prerequisites=prerequisites, max_attempts=0)
    if evidence.individual_signal == "insufficient_funds":
        return PolicyRecommendation(action=RecoveryAction.NOTIFY_CUSTOMER, decision="manual_review",
                                    reason_code="customer_funding_action",
                                    prerequisites=prerequisites, max_attempts=0)
    if evidence.individual_signal in {"network_timeout", "issuer_unavailable"}:
        network = evidence.individual_signal == "network_timeout"
        return PolicyRecommendation(
            action=RecoveryAction.RETRY_NOW if network else RecoveryAction.WAIT_AND_RETRY,
            decision="blocked",
            reason_code="transient_network" if network else "issuer_health_wait",
            retry_after_seconds=30 if network else 480, max_attempts=2,
            prerequisites=prerequisites,
        )
    return PolicyRecommendation(action=RecoveryAction.ESCALATE, decision="manual_review",
                                reason_code="unknown_signal",
                                prerequisites=prerequisites, max_attempts=0)
