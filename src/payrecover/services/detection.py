from math import sqrt

from payrecover.domain.models import CohortSnapshot, DetectionResult


def detect_degradation(snapshot: CohortSnapshot, min_samples: int = 30) -> DetectionResult:
    """One-proportion z-test with guardrails against tiny/noisy cohorts."""
    drop = snapshot.baseline_success_rate - snapshot.observed_success_rate
    if snapshot.sample_size < min_samples:
        return DetectionResult(degraded=False, severity="none", absolute_drop=drop, z_score=0,
                               reason="insufficient_sample_size")

    variance = snapshot.baseline_success_rate * (1 - snapshot.baseline_success_rate)
    standard_error = sqrt(max(variance / snapshot.sample_size, 1e-9))
    z_score = drop / standard_error
    degraded = drop >= 0.10 and z_score >= 3.0
    severity = (
        "critical"
        if drop >= 0.40
        else "high"
        if drop >= 0.25
        else "medium"
        if degraded
        else "none"
    )
    return DetectionResult(
        degraded=degraded,
        severity=severity,
        absolute_drop=drop,
        z_score=z_score,
        reason="statistically_significant_drop" if degraded else "within_guardrails",
    )
