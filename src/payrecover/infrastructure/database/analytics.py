from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Numeric, Select, and_, case, cast, collate, func, select
from sqlalchemy.orm import Session

from payrecover.domain.analytics import (
    AMOUNT_BAND_UPPER_BOUNDS_PAISE,
    AmountBand,
    AnalyticsWindow,
    CohortAggregate,
    CohortDimension,
    ErrorCodeCount,
    PaymentCohortV2,
    quantized_drop,
    quantized_rate,
)
from payrecover.domain.models import FieldAvailability
from payrecover.infrastructure.database.models import PaymentEventRow

MAX_ERROR_CODES_PER_COHORT = 20


def _amount_band_expression() -> Any:
    return cast(
        case(
            *(
                (PaymentEventRow.amount_paise < upper_bound, band_index)
                for band_index, upper_bound in enumerate(
                    AMOUNT_BAND_UPPER_BOUNDS_PAISE
                )
            ),
            else_=len(AMOUNT_BAND_UPPER_BOUNDS_PAISE),
        ),
        BigInteger,
    ).label("amount_band")


def build_cohort_aggregation_statement(
    merchant_id: str, window: AnalyticsWindow
) -> Select[Any]:
    """Build one PostgreSQL statement that aggregates, rather than returns, events."""
    analytics_events = (
        select(
            PaymentEventRow.method,
            PaymentEventRow.issuer,
            PaymentEventRow.issuer_availability,
            PaymentEventRow.provider,
            PaymentEventRow.provider_availability,
            PaymentEventRow.amount_paise,
            PaymentEventRow.status,
            PaymentEventRow.error_code,
            PaymentEventRow.occurred_at,
            _amount_band_expression(),
        )
        .where(
            PaymentEventRow.merchant_id == merchant_id,
            PaymentEventRow.occurred_at >= window.baseline_start,
            PaymentEventRow.occurred_at < window.observation_end,
        )
        .cte("analytics_events")
    )
    dimensions = (
        analytics_events.c.method,
        analytics_events.c.issuer,
        analytics_events.c.issuer_availability,
        analytics_events.c.provider,
        analytics_events.c.provider_availability,
        analytics_events.c.amount_band,
    )
    baseline = analytics_events.c.occurred_at < window.baseline_end
    observation = analytics_events.c.occurred_at >= window.observation_start
    baseline_failed = and_(baseline, analytics_events.c.status == "failed")
    observation_failed = and_(observation, analytics_events.c.status == "failed")

    cohort_counts = (
        select(
            *dimensions,
            func.count().filter(baseline).label("baseline_total"),
            func.count()
            .filter(and_(baseline, analytics_events.c.status == "success"))
            .label("baseline_success_count"),
            func.count().filter(baseline_failed).label("baseline_failed_count"),
            func.coalesce(
                func.sum(cast(analytics_events.c.amount_paise, Numeric(38, 0))).filter(
                    baseline_failed
                ),
                0,
            ).label("baseline_failed_amount_paise"),
            func.count().filter(observation).label("observation_total"),
            func.count()
            .filter(and_(observation, analytics_events.c.status == "success"))
            .label("observation_success_count"),
            func.count().filter(observation_failed).label("observation_failed_count"),
            func.coalesce(
                func.sum(cast(analytics_events.c.amount_paise, Numeric(38, 0))).filter(
                    observation_failed
                ),
                0,
            ).label("observation_failed_amount_paise"),
        )
        .group_by(*dimensions)
        .cte("cohort_counts")
    )

    error_counts = (
        select(
            *dimensions,
            analytics_events.c.error_code,
            func.count().label("error_count"),
        )
        .where(
            observation_failed,
            analytics_events.c.error_code.is_not(None),
        )
        .group_by(*dimensions, analytics_events.c.error_code)
        .cte("error_counts")
    )
    error_dimensions = (
        error_counts.c.method,
        error_counts.c.issuer,
        error_counts.c.issuer_availability,
        error_counts.c.provider,
        error_counts.c.provider_availability,
        error_counts.c.amount_band,
    )
    ranked_errors = (
        select(
            *error_counts.c,
            func.row_number()
            .over(
                partition_by=error_dimensions,
                order_by=(
                    error_counts.c.error_count.desc(),
                    collate(error_counts.c.error_code, "C").asc(),
                ),
            )
            .label("error_rank"),
        )
        .cte("ranked_errors")
    )
    top_errors = (
        select(ranked_errors).where(
            ranked_errors.c.error_rank <= MAX_ERROR_CODES_PER_COHORT
        )
    ).cte("top_errors")

    join_conditions = (
        cohort_counts.c.method == top_errors.c.method,
        cohort_counts.c.issuer.is_not_distinct_from(top_errors.c.issuer),
        cohort_counts.c.issuer_availability == top_errors.c.issuer_availability,
        cohort_counts.c.provider.is_not_distinct_from(top_errors.c.provider),
        cohort_counts.c.provider_availability == top_errors.c.provider_availability,
        cohort_counts.c.amount_band == top_errors.c.amount_band,
    )
    return (
        select(
            *cohort_counts.c,
            top_errors.c.error_code,
            top_errors.c.error_count,
            top_errors.c.error_rank,
        )
        .outerjoin(top_errors, and_(*join_conditions))
        .order_by(
            cohort_counts.c.method,
            cohort_counts.c.issuer_availability,
            cohort_counts.c.issuer,
            cohort_counts.c.provider_availability,
            cohort_counts.c.provider,
            cohort_counts.c.amount_band,
            top_errors.c.error_rank,
        )
    )


@dataclass(slots=True)
class _AggregateBuilder:
    cohort: PaymentCohortV2
    baseline_total: int
    baseline_success_count: int
    baseline_failed_count: int
    baseline_failed_amount_paise: int
    observation_total: int
    observation_success_count: int
    observation_failed_count: int
    observation_failed_amount_paise: int
    errors: list[ErrorCodeCount] = field(default_factory=list)

    def build(self) -> CohortAggregate:
        distribution = tuple(self.errors)
        return CohortAggregate(
            cohort=self.cohort,
            baseline_total=self.baseline_total,
            baseline_success_count=self.baseline_success_count,
            baseline_failed_count=self.baseline_failed_count,
            baseline_success_rate=quantized_rate(
                self.baseline_success_count, self.baseline_total
            ),
            baseline_failed_amount_paise=self.baseline_failed_amount_paise,
            observation_total=self.observation_total,
            observation_success_count=self.observation_success_count,
            observation_failed_count=self.observation_failed_count,
            observation_success_rate=quantized_rate(
                self.observation_success_count, self.observation_total
            ),
            observation_failed_amount_paise=self.observation_failed_amount_paise,
            absolute_success_rate_drop=quantized_drop(
                self.baseline_success_count,
                self.baseline_total,
                self.observation_success_count,
                self.observation_total,
            ),
            observation_error_code_distribution=distribution,
            dominant_observation_error_code=(
                distribution[0].error_code if distribution else None
            ),
        )


class SqlAlchemyPaymentAnalyticsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def aggregate_for_merchant(
        self, merchant_id: str, window: AnalyticsWindow
    ) -> tuple[CohortAggregate, ...]:
        rows = self._session.execute(
            build_cohort_aggregation_statement(merchant_id, window)
        ).mappings()
        builders: dict[
            tuple[str, str | None, str, str | None, str, int], _AggregateBuilder
        ] = {}
        for row in rows:
            key = (
                str(row["method"]),
                row["issuer"],
                str(row["issuer_availability"]),
                row["provider"],
                str(row["provider_availability"]),
                int(row["amount_band"]),
            )
            builder = builders.get(key)
            if builder is None:
                builder = _AggregateBuilder(
                    cohort=PaymentCohortV2(
                        merchant_id=merchant_id,
                        method=key[0],
                        issuer=CohortDimension(
                            value=key[1],
                            availability=FieldAvailability(key[2]),
                        ),
                        provider=CohortDimension(
                            value=key[3],
                            availability=FieldAvailability(key[4]),
                        ),
                        amount_band=AmountBand(f"band_{key[5]}"),
                    ),
                    baseline_total=int(row["baseline_total"]),
                    baseline_success_count=int(row["baseline_success_count"]),
                    baseline_failed_count=int(row["baseline_failed_count"]),
                    baseline_failed_amount_paise=int(
                        Decimal(row["baseline_failed_amount_paise"])
                    ),
                    observation_total=int(row["observation_total"]),
                    observation_success_count=int(row["observation_success_count"]),
                    observation_failed_count=int(row["observation_failed_count"]),
                    observation_failed_amount_paise=int(
                        Decimal(row["observation_failed_amount_paise"])
                    ),
                )
                builders[key] = builder
            if row["error_code"] is not None:
                builder.errors.append(
                    ErrorCodeCount(
                        error_code=str(row["error_code"]),
                        failed_count=int(row["error_count"]),
                    )
                )

        aggregates = tuple(builder.build() for builder in builders.values())
        return tuple(sorted(aggregates, key=lambda value: value.cohort.cohort_hash))
