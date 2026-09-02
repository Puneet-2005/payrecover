from payrecover.domain.models import CohortSnapshot
from payrecover.services.detection import detect_degradation


def test_detects_large_payment_degradation():
    result = detect_degradation(CohortSnapshot(cohort_key="upi:bank_x:phonepe:band_3",
        baseline_success_rate=0.92, observed_success_rate=0.45, sample_size=200,
        failed_count=110, failed_amount_paise=38_000_000, dominant_error_code="issuer_unavailable"))
    assert result.degraded is True
    assert result.severity == "critical"


def test_rejects_tiny_sample():
    result = detect_degradation(CohortSnapshot(cohort_key="x", baseline_success_rate=.9,
        observed_success_rate=.1, sample_size=5, failed_count=4, failed_amount_paise=1000))
    assert result.degraded is False
    assert result.reason == "insufficient_sample_size"

