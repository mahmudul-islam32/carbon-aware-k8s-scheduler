"""Unit tests for window selection policies.

These tests cover:
- Each policy with various forecast scenarios
- Deadline guarantee is never violated
- Edge cases (no forecast data, all low renewable, etc.)
- Hand-computed examples for verification
"""

from datetime import UTC, datetime, timedelta

import pytest

from carbon_scheduler.planner.models import (
    Forecast,
    ForecastPoint,
    JobInfo,
    PolicyConfig,
    ScheduleWindow,
)
from carbon_scheduler.planner.policies import (
    GreenestWindowPolicy,
    ImmediatePolicy,
    ThresholdPolicy,
    get_policy,
)
from carbon_scheduler.planner.window import select_window


def make_forecast(
    start_time: datetime,
    shares: list[float],
    interval_minutes: int = 15,
    degraded: bool = False,
) -> Forecast:
    """Helper to create a Forecast from a list of share values.

    Args:
        start_time: Timestamp for the first point.
        shares: List of renewable share percentages.
        interval_minutes: Time between forecast points.
        degraded: Whether to mark the forecast as degraded.

    Returns:
        A Forecast object with points at regular intervals.
    """
    points = []
    for i, share in enumerate(shares):
        ts = start_time + timedelta(minutes=i * interval_minutes)
        points.append(ForecastPoint(timestamp=ts, renewable_share_pct=share))
    return Forecast(points=points, degraded=degraded)


def make_job(
    submit_time: datetime,
    deadline: datetime,
    runtime_minutes: int = 30,
    name: str = "test-job",
) -> JobInfo:
    """Helper to create a JobInfo."""
    return JobInfo(
        name=name,
        submit_time=submit_time,
        deadline=deadline,
        estimated_runtime=timedelta(minutes=runtime_minutes),
    )


class TestImmediatePolicy:
    """Tests for the immediate policy."""

    def test_immediate_starts_now(self) -> None:
        """Immediate policy should return window starting at current_time."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now - timedelta(minutes=5),
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        forecast = make_forecast(now, [50, 60, 70, 80])

        policy = ImmediatePolicy()
        window = policy.select(job, forecast, now)

        assert window.start == now
        assert window.end == now + timedelta(minutes=30)
        assert window.policy_used == "immediate"
        assert window.is_fallback is False

    def test_immediate_uses_submit_time_if_later(self) -> None:
        """If submit_time > current_time, window starts at submit_time."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        submit_time = now + timedelta(minutes=10)
        job = make_job(
            submit_time=submit_time,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )

        policy = ImmediatePolicy()
        window = policy.select(job, None, now)

        assert window.start == submit_time
        assert window.end == submit_time + timedelta(minutes=30)

    def test_immediate_works_without_forecast(self) -> None:
        """Immediate policy should work even with no forecast data."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )

        policy = ImmediatePolicy()
        window = policy.select(job, None, now)

        assert window.start == now
        assert window.policy_used == "immediate"
        assert window.mean_renewable_share_pct is None


class TestThresholdPolicy:
    """Tests for the threshold policy."""

    def test_threshold_finds_first_qualifying_slot(self) -> None:
        """Should find first slot where renewable >= threshold."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        shares = [40, 45, 50, 65, 70, 75, 60]
        forecast = make_forecast(now, shares, interval_minutes=15)

        config = PolicyConfig(renewable_threshold_pct=60)
        policy = ThresholdPolicy(config=config)
        window = policy.select(job, forecast, now)

        expected_start = now + timedelta(minutes=45)
        assert window.start == expected_start
        assert window.policy_used == "threshold"
        assert window.is_fallback is False

    def test_threshold_respects_deadline(self) -> None:
        """Should not select window that would end after deadline - safety margin."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=1)
        job = make_job(
            submit_time=now,
            deadline=deadline,
            runtime_minutes=30,
        )
        shares = [40, 45, 50, 55, 70]
        forecast = make_forecast(now, shares, interval_minutes=15)

        config = PolicyConfig(
            renewable_threshold_pct=60,
            safety_margin=timedelta(minutes=5),
        )
        policy = ThresholdPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.end <= deadline - timedelta(minutes=5)

    def test_threshold_fallback_when_no_qualifying_slot(self) -> None:
        """Should fall back when no slot meets threshold before deadline."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        shares = [30, 35, 40, 45, 50]
        forecast = make_forecast(now, shares, interval_minutes=15)

        config = PolicyConfig(renewable_threshold_pct=60)
        policy = ThresholdPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.is_fallback is True
        assert window.fallback_reason is not None

    def test_threshold_fallback_on_no_forecast(self) -> None:
        """Should fall back immediately when no forecast data."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )

        policy = ThresholdPolicy()
        window = policy.select(job, None, now)

        assert window.is_fallback is True
        assert window.fallback_reason == "no_forecast_data"
        assert window.start == now

    def test_threshold_fallback_on_degraded_forecast(self) -> None:
        """Should fall back when forecast is marked as degraded."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        forecast = make_forecast(now, [80, 85, 90], degraded=True)

        policy = ThresholdPolicy()
        window = policy.select(job, forecast, now)

        assert window.is_fallback is True
        assert window.fallback_reason == "forecast_data_degraded"


class TestGreenestWindowPolicy:
    """Tests for the greenest-window policy."""

    def test_greenest_finds_highest_mean_share(self) -> None:
        """Should find window with highest mean renewable share.

        Hand-computed example:
        - Forecast: [40, 50, 80, 90, 60, 50] at 15-min intervals
        - Job runtime: 30 minutes
        - A 30-min window starting at T covers points at T, T+15, T+30
        - Possible windows and means:
          - T+0 to T+30: points 40, 50, 80 -> mean = 56.67%
          - T+15 to T+45: points 50, 80, 90 -> mean = 73.33%
          - T+30 to T+60: points 80, 90, 60 -> mean = 76.67%
          - T+45 to T+75: points 90, 60, 50 -> mean = 66.67%

        Expected: Window starting at T+30 with mean 76.67%
        """
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        shares = [40, 50, 80, 90, 60, 50]
        forecast = make_forecast(now, shares, interval_minutes=15)

        policy = GreenestWindowPolicy()
        window = policy.select(job, forecast, now)

        expected_start = now + timedelta(minutes=30)
        assert window.start == expected_start
        assert window.policy_used == "greenest-window"
        assert window.is_fallback is False

        assert window.mean_renewable_share_pct is not None
        assert window.mean_renewable_share_pct == pytest.approx(76.67, rel=0.01)

    def test_greenest_respects_deadline_with_safety_margin(self) -> None:
        """Should not select window ending after deadline - safety margin."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=1, minutes=30)
        safety_margin = timedelta(minutes=5)
        job = make_job(
            submit_time=now,
            deadline=deadline,
            runtime_minutes=30,
        )
        shares = [40, 50, 60, 70, 95, 95]
        forecast = make_forecast(now, shares, interval_minutes=15)

        config = PolicyConfig(safety_margin=safety_margin)
        policy = GreenestWindowPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.end <= deadline - safety_margin

    def test_greenest_fallback_on_no_forecast(self) -> None:
        """Should fall back when no forecast data available."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )

        policy = GreenestWindowPolicy()
        window = policy.select(job, None, now)

        assert window.is_fallback is True
        assert window.fallback_reason == "no_forecast_data"

    def test_greenest_fallback_on_empty_forecast(self) -> None:
        """Should fall back when forecast has no points."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        forecast = Forecast(points=[])

        policy = GreenestWindowPolicy()
        window = policy.select(job, forecast, now)

        assert window.is_fallback is True
        assert window.fallback_reason == "no_forecast_data"

    def test_greenest_with_all_equal_shares(self) -> None:
        """When all shares are equal, should return a valid window."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        shares = [60, 60, 60, 60, 60]
        forecast = make_forecast(now, shares, interval_minutes=15)

        policy = GreenestWindowPolicy()
        window = policy.select(job, forecast, now)

        assert window.is_fallback is False
        assert window.mean_renewable_share_pct == pytest.approx(60.0)


class TestDeadlineGuarantee:
    """Tests ensuring deadline is never violated."""

    @pytest.mark.parametrize("policy_name", ["immediate", "threshold", "greenest-window"])
    def test_deadline_never_exceeded(self, policy_name: str) -> None:
        """No policy should return window ending after deadline."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=2)
        safety_margin = timedelta(minutes=5)

        job = make_job(
            submit_time=now,
            deadline=deadline,
            runtime_minutes=60,
        )
        forecast = make_forecast(now, [50, 60, 70, 80, 90, 95, 98, 99])

        config = PolicyConfig(
            renewable_threshold_pct=60,
            safety_margin=safety_margin,
        )
        policy = get_policy(policy_name, config=config)
        window = policy.select(job, forecast, now)

        if policy_name == "immediate":
            assert window.end <= deadline
        else:
            assert window.end <= deadline - safety_margin

    def test_latest_feasible_start_calculation(self) -> None:
        """Latest feasible start should be deadline - runtime - safety margin."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        deadline = now + timedelta(hours=2)
        runtime = timedelta(minutes=30)
        safety_margin = timedelta(minutes=5)

        job = JobInfo(
            name="test",
            submit_time=now,
            deadline=deadline,
            estimated_runtime=runtime,
        )

        expected = deadline - runtime - safety_margin
        assert job.latest_feasible_start(safety_margin) == expected

    def test_fallback_when_current_time_past_latest_feasible(self) -> None:
        """Should fall back when current time exceeds latest feasible start."""
        deadline = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        submit_time = deadline - timedelta(hours=2)
        now = deadline - timedelta(minutes=20)

        job = make_job(
            submit_time=submit_time,
            deadline=deadline,
            runtime_minutes=30,
        )
        forecast = make_forecast(submit_time, [90] * 10)

        config = PolicyConfig(safety_margin=timedelta(minutes=5))
        policy = GreenestWindowPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.is_fallback is True
        assert "past_latest_feasible_start" in (window.fallback_reason or "")


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_forecast_points(self) -> None:
        """Should handle empty forecast gracefully."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        forecast = Forecast(points=[])

        for policy_name in ["threshold", "greenest-window"]:
            policy = get_policy(policy_name)
            window = policy.select(job, forecast, now)

            assert window.is_fallback is True
            assert window.start == now

    def test_all_low_renewable_shares(self) -> None:
        """Should fall back when all shares are below threshold."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        shares = [10, 15, 20, 25, 30]
        forecast = make_forecast(now, shares)

        config = PolicyConfig(renewable_threshold_pct=60)
        policy = ThresholdPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.is_fallback is True

    def test_very_short_deadline(self) -> None:
        """Should handle deadline very close to submit time."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(minutes=35),
            runtime_minutes=30,
        )
        shares = [90, 95, 98]
        forecast = make_forecast(now, shares)

        config = PolicyConfig(safety_margin=timedelta(minutes=5))
        policy = GreenestWindowPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.end <= job.deadline - timedelta(minutes=5)

    def test_forecast_starts_after_current_time(self) -> None:
        """Should handle forecast that starts in the future."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        forecast_start = now + timedelta(hours=1)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        shares = [70, 80, 90]
        forecast = make_forecast(forecast_start, shares)

        policy = ThresholdPolicy(config=PolicyConfig(renewable_threshold_pct=60))
        window = policy.select(job, forecast, now)

        assert window.start >= now
        assert window.is_fallback is False

    def test_forecast_ends_before_deadline(self) -> None:
        """Should handle forecast that ends before job deadline.

        When forecast data ends early, the policy should still find the best
        window within the available forecast period. Here:
        - Points at T+0 (80), T+15 (85), T+30 (90)
        - 30-min job starting at T+0 covers T+0 to T+30, mean = (80+85+90)/3 = 85%
        - Starting at T+15 covers T+15 to T+45, but T+45 has no data (only T+30)
          so mean = (85+90)/2 = 87.5% (but end point missing...)
        - Actually the best window is starting at T+30 (covers T+30=90 only)
          but that gives mean=90%
        The algorithm finds the window with highest mean among those with data.
        """
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=4),
            runtime_minutes=30,
        )
        shares = [80, 85, 90]
        forecast = make_forecast(now, shares, interval_minutes=15)

        policy = GreenestWindowPolicy()
        window = policy.select(job, forecast, now)

        # The greenest window is at T+30 since it only has the 90% point
        assert window.start == now + timedelta(minutes=30)
        assert window.mean_renewable_share_pct == pytest.approx(90.0)

    def test_single_forecast_point(self) -> None:
        """Should handle forecast with single point."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        forecast = Forecast(
            points=[ForecastPoint(timestamp=now, renewable_share_pct=75)]
        )

        policy = ThresholdPolicy(config=PolicyConfig(renewable_threshold_pct=60))
        window = policy.select(job, forecast, now)

        assert window.start == now
        assert window.is_fallback is False

    def test_zero_runtime(self) -> None:
        """Should handle job with zero estimated runtime."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=0,
        )
        forecast = make_forecast(now, [70, 80, 90])

        policy = GreenestWindowPolicy()
        window = policy.select(job, forecast, now)

        assert window.start == window.end


class TestPolicyFactory:
    """Tests for the get_policy factory function."""

    def test_get_immediate_policy(self) -> None:
        """Should return ImmediatePolicy for 'immediate'."""
        policy = get_policy("immediate")
        assert isinstance(policy, ImmediatePolicy)

    def test_get_threshold_policy(self) -> None:
        """Should return ThresholdPolicy for 'threshold'."""
        policy = get_policy("threshold")
        assert isinstance(policy, ThresholdPolicy)

    def test_get_greenest_window_policy(self) -> None:
        """Should return GreenestWindowPolicy for 'greenest-window'."""
        policy = get_policy("greenest-window")
        assert isinstance(policy, GreenestWindowPolicy)

    def test_get_threshold_with_custom_threshold(self) -> None:
        """Should allow custom threshold for threshold policy."""
        policy = get_policy("threshold", threshold_pct=75)
        assert isinstance(policy, ThresholdPolicy)
        assert policy.config.renewable_threshold_pct == 75

    def test_get_policy_with_config(self) -> None:
        """Should accept config for all policies."""
        config = PolicyConfig(
            renewable_threshold_pct=80,
            safety_margin=timedelta(minutes=10),
        )

        for name in ["immediate", "threshold", "greenest-window"]:
            policy = get_policy(name, config=config)
            assert policy.config.safety_margin == timedelta(minutes=10)

    def test_get_unknown_policy_raises(self) -> None:
        """Should raise ValueError for unknown policy name."""
        with pytest.raises(ValueError, match="Unknown policy"):
            get_policy("nonexistent")  # type: ignore


class TestSelectWindowFunction:
    """Tests for the select_window convenience function."""

    def test_select_window_immediate(self) -> None:
        """select_window should work with immediate policy."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        config = PolicyConfig()

        window = select_window(job, None, now, "immediate", config)

        assert window.start == now
        assert window.policy_used == "immediate"

    def test_select_window_threshold(self) -> None:
        """select_window should work with threshold policy."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        forecast = make_forecast(now, [50, 55, 70, 75])
        config = PolicyConfig(renewable_threshold_pct=60)

        window = select_window(job, forecast, now, "threshold", config)

        assert window.start == now + timedelta(minutes=30)
        assert window.policy_used == "threshold"

    def test_select_window_greenest(self) -> None:
        """select_window should work with greenest-window policy."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=2),
            runtime_minutes=30,
        )
        forecast = make_forecast(now, [50, 55, 90, 95, 70])
        config = PolicyConfig()

        window = select_window(job, forecast, now, "greenest-window", config)

        expected_start = now + timedelta(minutes=30)
        assert window.start == expected_start
        assert window.policy_used == "greenest-window"


class TestModels:
    """Tests for Pydantic models."""

    def test_job_info_duration_parsing(self) -> None:
        """Should parse various duration formats."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)

        job = JobInfo(
            name="test",
            submit_time=now,
            deadline=now + timedelta(hours=2),
            estimated_runtime="1h30m",
        )
        assert job.estimated_runtime == timedelta(hours=1, minutes=30)

        job = JobInfo(
            name="test",
            submit_time=now,
            deadline=now + timedelta(hours=2),
            estimated_runtime="90m",
        )
        assert job.estimated_runtime == timedelta(minutes=90)

        job = JobInfo(
            name="test",
            submit_time=now,
            deadline=now + timedelta(hours=2),
            estimated_runtime=3600,
        )
        assert job.estimated_runtime == timedelta(hours=1)

    def test_forecast_sorting(self) -> None:
        """Forecast points should be sorted by timestamp."""
        t1 = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        t2 = datetime(2024, 6, 15, 11, 0, 0, tzinfo=UTC)
        t3 = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

        forecast = Forecast(
            points=[
                ForecastPoint(timestamp=t3, renewable_share_pct=70),
                ForecastPoint(timestamp=t1, renewable_share_pct=50),
                ForecastPoint(timestamp=t2, renewable_share_pct=60),
            ]
        )

        assert forecast.points[0].timestamp == t1
        assert forecast.points[1].timestamp == t2
        assert forecast.points[2].timestamp == t3

    def test_schedule_window_duration(self) -> None:
        """ScheduleWindow should calculate duration correctly."""
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        window = ScheduleWindow(
            start=now,
            end=now + timedelta(hours=1, minutes=30),
            policy_used="test",
        )

        assert window.duration == timedelta(hours=1, minutes=30)


class TestHandComputedExamples:
    """Hand-computed examples for verification."""

    def test_greenest_window_hand_computed(self) -> None:
        """Verify greenest window selection with hand-computed values.

        Scenario:
        - Forecast at 15-min intervals: [40, 50, 80, 85, 70, 55]
          Times: T+0, T+15, T+30, T+45, T+60, T+75
        - Job runtime: 30 minutes
        - A 30-min window starting at T covers points from T to T+30 inclusive:
          - Start at T+0:  covers T+0 to T+30, points: 40, 50, 80
            Mean = (40 + 50 + 80) / 3 = 56.67%
          - Start at T+15: covers T+15 to T+45, points: 50, 80, 85
            Mean = (50 + 80 + 85) / 3 = 71.67%
          - Start at T+30: covers T+30 to T+60, points: 80, 85, 70
            Mean = (80 + 85 + 70) / 3 = 78.33%
          - Start at T+45: covers T+45 to T+75, points: 85, 70, 55
            Mean = (85 + 70 + 55) / 3 = 70%

        Expected: Window starting at T+30 with mean 78.33%
        """
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=3),
            runtime_minutes=30,
        )
        shares = [40, 50, 80, 85, 70, 55]
        forecast = make_forecast(now, shares, interval_minutes=15)

        policy = GreenestWindowPolicy()
        window = policy.select(job, forecast, now)

        assert window.start == now + timedelta(minutes=30)
        assert window.mean_renewable_share_pct == pytest.approx(78.33, rel=0.01)

    def test_threshold_hand_computed(self) -> None:
        """Verify threshold policy with hand-computed values.

        Scenario:
        - Threshold: 70%
        - Forecast at 15-min intervals: [50, 55, 65, 72, 80]
        - First point >= 70% is at T+45 (72%)

        Expected: Window starting at T+45
        """
        now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
        job = make_job(
            submit_time=now,
            deadline=now + timedelta(hours=3),
            runtime_minutes=30,
        )
        shares = [50, 55, 65, 72, 80]
        forecast = make_forecast(now, shares, interval_minutes=15)

        config = PolicyConfig(renewable_threshold_pct=70)
        policy = ThresholdPolicy(config=config)
        window = policy.select(job, forecast, now)

        assert window.start == now + timedelta(minutes=45)
        assert window.is_fallback is False

    def test_latest_feasible_start_hand_computed(self) -> None:
        """Verify latest feasible start calculation.

        Scenario:
        - Deadline: 14:00
        - Runtime: 30 minutes
        - Safety margin: 5 minutes

        Latest feasible start = 14:00 - 30min - 5min = 13:25
        """
        deadline = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
        submit_time = deadline - timedelta(hours=4)
        job = JobInfo(
            name="test",
            submit_time=submit_time,
            deadline=deadline,
            estimated_runtime=timedelta(minutes=30),
        )
        safety_margin = timedelta(minutes=5)

        expected = datetime(2024, 6, 15, 13, 25, 0, tzinfo=UTC)
        assert job.latest_feasible_start(safety_margin) == expected
