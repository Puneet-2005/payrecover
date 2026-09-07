from datetime import datetime
from typing import Literal, cast
from uuid import uuid4

from payrecover.domain.incidents import IncidentObservation
from payrecover.domain.planning import (
    DiagnosisEvidence,
    DiagnosisOutcome,
    IncidentDiagnosis,
    Signal,
    digest,
)

CAUSES: dict[Signal, DiagnosisOutcome] = {
    "network_timeout": "probable_transient_network_failure",
    "issuer_unavailable": "probable_issuer_unavailability",
    "insufficient_funds": "probable_customer_funding_issue",
    "incorrect_pin": "probable_authentication_failure",
}


def diagnose(observation: IncidentObservation, now: datetime) -> IncidentDiagnosis:
    aggregate = observation.evidence
    total = aggregate.observation_failed_count
    retained = sum(e.failed_count for e in aggregate.observation_error_code_distribution)
    if retained > total:
        raise ValueError("Error evidence exceeds failed count")
    signal: Signal = "unknown"
    count = 0
    outcome: DiagnosisOutcome = "unknown"
    reason: Literal["strict_majority", "mixed_or_unmapped_failures", "insufficient_failures"] = (
        "mixed_or_unmapped_failures"
    )
    if observation.detection.outcome == "insufficient" or total == 0:
        outcome, reason = "insufficient_evidence", "insufficient_failures"
    else:
        for error in aggregate.observation_error_code_distribution:
            if error.error_code in CAUSES and 2 * error.failed_count > total:
                signal = cast(Signal, error.error_code)
                count, outcome, reason = error.failed_count, CAUSES[signal], "strict_majority"
    evidence = DiagnosisEvidence(
        detector_outcome=observation.detection.outcome.value,
        supporting_signal=signal,
        retained_failed_count=retained,
        unrepresented_failed_count=total - retained,
    )
    return IncidentDiagnosis(
        id=uuid4(),
        merchant_id=aggregate.cohort.merchant_id,
        incident_id=observation.incident_id,
        observation_id=observation.id,
        outcome=outcome,
        reason_code=reason,
        supporting_failed_count=count,
        total_failed_count=total,
        evidence=evidence,
        evidence_sha256=digest(evidence),
        created_at=now,
    )
