from datetime import UTC, datetime, timedelta, timezone

import pytest

from payrecover.domain.analytics import calculate_analytics_window


@pytest.mark.parametrize(
    ("as_of", "expected_end"),
    [
        (datetime(2026, 9, 4, 12, 20, tzinfo=UTC), datetime(2026, 9, 4, 12, 15, tzinfo=UTC)),
        (
            datetime(2026, 9, 4, 12, 19, 59, 999999, tzinfo=UTC),
            datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        ),
        (datetime(2026, 9, 4, 12, 35, tzinfo=UTC), datetime(2026, 9, 4, 12, 30, tzinfo=UTC)),
        (datetime(2026, 9, 4, 0, 3, tzinfo=UTC), datetime(2026, 9, 3, 23, 45, tzinfo=UTC)),
    ],
)
def test_completed_window_alignment(as_of: datetime, expected_end: datetime):
    window = calculate_analytics_window(as_of)
    assert window.observation_end == expected_end
    assert window.observation_start == expected_end - timedelta(minutes=15)
    assert window.baseline_end == window.observation_start
    assert window.baseline_start == window.baseline_end - timedelta(days=7)


def test_non_utc_aware_as_of_is_normalized_to_utc():
    india = timezone(timedelta(hours=5, minutes=30))
    window = calculate_analytics_window(datetime(2026, 9, 4, 17, 50, tzinfo=india))
    assert window.observation_end == datetime(2026, 9, 4, 12, 15, tzinfo=UTC)
    assert all(
        value.utcoffset() == timedelta(0)
        for value in (
            window.baseline_start,
            window.baseline_end,
            window.observation_start,
            window.observation_end,
        )
    )


def test_naive_as_of_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_analytics_window(datetime(2026, 9, 4, 12, 20))


def test_half_open_intervals_do_not_overlap():
    window = calculate_analytics_window(datetime(2026, 9, 4, 12, 23, tzinfo=UTC))

    def baseline_contains(value: datetime) -> bool:
        return window.baseline_start <= value < window.baseline_end

    def observation_contains(value: datetime) -> bool:
        return window.observation_start <= value < window.observation_end

    assert window.baseline_end == window.observation_start
    assert baseline_contains(window.baseline_end) is False
    assert observation_contains(window.observation_start) is True
    assert observation_contains(window.observation_end) is False
