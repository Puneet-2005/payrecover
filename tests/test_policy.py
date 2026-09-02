from payrecover.domain.models import RecoveryAction, recovery_idempotency_key
from payrecover.services.policy import decide_recovery


def test_transient_failure_can_retry():
    decision = decide_recovery("network_timeout")
    assert decision.permitted is True
    assert decision.action == RecoveryAction.RETRY_NOW


def test_pin_failure_never_retries():
    assert decide_recovery("incorrect_pin").permitted is False


def test_unknown_failure_escalates():
    assert decide_recovery("new_error").action == RecoveryAction.ESCALATE


def test_idempotency_key_is_stable_and_attempt_scoped():
    one = recovery_idempotency_key("pay_1", RecoveryAction.RETRY_NOW, 1)
    assert one == recovery_idempotency_key("pay_1", RecoveryAction.RETRY_NOW, 1)
    assert one != recovery_idempotency_key("pay_1", RecoveryAction.RETRY_NOW, 2)

