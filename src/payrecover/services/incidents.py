from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from payrecover.domain.analytics import (
    OBSERVATION_DURATION,
    CohortAggregate,
    DegradationDetection,
    DegradationSeverity,
    DetectionOutcome,
    PaymentCohortV2,
    calculate_analytics_window,
)
from payrecover.domain.incidents import (
    Incident,
    IncidentOpenedAudit,
    IncidentResolvedAudit,
    IncidentUpdatedAudit,
    ScanResult,
    ScanSummary,
    utc,
    validate_merchant,
)
from payrecover.domain.records import NewAuditRecord
from payrecover.domain.repositories import UnitOfWork
from payrecover.services.analytics import DETECTOR_VERSION, detect_degradation_v2


class StaleScan(ValueError):
    pass


class ScanUnavailable(RuntimeError):
    pass


def absent_evidence(cohort: PaymentCohortV2) -> CohortAggregate:
    return CohortAggregate(cohort, 0, 0, 0, None, 0, 0, 0, 0, None, 0, None, (), None)


def advance_incident(
    incident: Incident,
    detection: DegradationDetection,
    scan: ScanSummary,
) -> Incident:
    if incident.status != "open":
        raise ValueError("Resolved incidents cannot be updated")
    if scan.window.observation_start <= incident.last_window_start:
        raise ValueError("Incident observations must advance chronologically")
    streak = 0
    if detection.outcome == DetectionOutcome.HEALTHY:
        adjacent = (
            incident.latest_outcome == DetectionOutcome.HEALTHY
            and incident.last_window_start + OBSERVATION_DURATION == scan.window.observation_start
        )
        streak = incident.healthy_streak + 1 if adjacent else 1
    severity_order = list(DegradationSeverity)
    peak = incident.peak_severity
    if detection.severity is not None:
        peak = max((peak, detection.severity), key=severity_order.index)
    return replace(
        incident,
        peak_severity=peak,
        latest_outcome=detection.outcome,
        last_window_start=scan.window.observation_start,
        healthy_streak=streak,
        status="resolved" if streak == 2 else "open",
        resolved_at=scan.recorded_at if streak == 2 else None,
    )


def scan_merchant(
    merchant_id: str,
    as_of: datetime,
    unit_of_work: UnitOfWork,
    *,
    now: datetime,
) -> ScanResult:
    validate_merchant(merchant_id)
    as_of, now = utc(as_of), utc(now)
    if as_of > now:
        raise ValueError("as-of must not be in the future")
    window = calculate_analytics_window(as_of)
    try:
        repository = unit_of_work.incidents
        repository.lock_merchant(merchant_id)
        previous = repository.find_scan(merchant_id, DETECTOR_VERSION, window.observation_start)
        if previous is not None:
            unit_of_work.commit()
            return ScanResult(previous, True)
        latest = repository.latest_scan(merchant_id, DETECTOR_VERSION)
        if latest is not None and window.observation_start < latest:
            raise StaleScan("Unseen scan window is older than the latest committed scan")
        scan = ScanSummary(uuid4(), merchant_id, DETECTOR_VERSION, window, 0, 0, 0, 0, now)
        repository.claim_scan(scan)
        open_incidents = {
            incident.cohort.cohort_hash: incident
            for incident in repository.open_incidents(merchant_id, DETECTOR_VERSION)
        }
        evidence = {
            item.cohort_hash: item
            for item in unit_of_work.payment_analytics.aggregate_for_merchant(merchant_id, window)
        }
        for key, existing_incident in open_incidents.items():
            evidence.setdefault(key, absent_evidence(existing_incident.cohort))
        opened = updated = resolved = 0
        for key in sorted(evidence):
            aggregate = evidence[key]
            detection = detect_degradation_v2(aggregate)
            incident = open_incidents.get(key)
            if incident is None and detection.outcome != DetectionOutcome.DEGRADED:
                continue
            is_new = incident is None
            if incident is None:
                incident = repository.create_incident(aggregate.cohort, detection, scan)
                opened += 1
            else:
                incident = advance_incident(incident, detection, scan)
                repository.update_incident(incident)
                updated += 1
            observation = repository.append_observation(incident, scan, aggregate, detection)
            common = dict(
                scan_id=scan.id,
                observation_id=observation.id,
                cohort_sha256=key,
                detector_version=DETECTOR_VERSION,
                observation_start=window.observation_start,
                observation_end=window.observation_end,
            )
            details = (
                IncidentOpenedAudit.model_validate(
                    {**common, "opening_severity": incident.opening_severity.value}
                )
                if is_new
                else IncidentUpdatedAudit.model_validate(
                    {
                        **common,
                        "outcome": detection.outcome.value,
                        "healthy_streak": incident.healthy_streak,
                    }
                )
            )
            unit_of_work.audit_records.append(
                NewAuditRecord(
                    correlation_id=scan.id,
                    payment_event_id=None,
                    incident_id=incident.id,
                    event_type="incident.opened" if is_new else "incident.observation_updated",
                    actor_type="incident_scanner",
                    actor_ref_digest=None,
                    details=details.model_dump(),
                )
            )
            if incident.status == "resolved":
                resolved += 1
                unit_of_work.audit_records.append(
                    NewAuditRecord(
                        correlation_id=scan.id,
                        payment_event_id=None,
                        incident_id=incident.id,
                        event_type="incident.resolved",
                        actor_type="incident_scanner",
                        actor_ref_digest=None,
                        details=IncidentResolvedAudit.model_validate(
                            {**common, "reason": "two_adjacent_healthy_windows"}
                        ).model_dump(),
                    )
                )
        scan = replace(
            scan, evaluated=len(evidence), opened=opened, updated=updated, resolved=resolved
        )
        repository.finish_scan(scan)
        unit_of_work.commit()
        return ScanResult(scan, False)
    except StaleScan:
        unit_of_work.rollback()
        raise
    except Exception as exc:
        unit_of_work.rollback()
        raise ScanUnavailable("Incident scan could not be committed") from exc
