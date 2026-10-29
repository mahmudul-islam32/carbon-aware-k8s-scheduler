"""Unit tests for the simulation and backtest module.

These tests cover:
- Trace generation and loading
- Backtest determinism
- CSV output format
- Historical data handling
"""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from carbon_scheduler.planner.models import PolicyConfig
from carbon_scheduler.sim.backtest import Backtest, BacktestResult, JobResult, PolicyResult
from carbon_scheduler.sim.historical import HistoricalData, HistoricalDataPoint, HistoricalDataStore
from carbon_scheduler.sim.trace import (
    JobTrace,
    SyntheticTraceGenerator,
    TraceJob,
    parse_slack_range,
)


class TestTraceJob:
    """Tests for TraceJob model."""

    def test_create_trace_job(self) -> None:
        """Should create a trace job with all fields."""
        submit = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
        deadline = datetime(2026, 7, 1, 18, 0, 0, tzinfo=UTC)

        job = TraceJob(
            submit_time=submit,
            deadline=deadline,
            runtime=timedelta(hours=1),
            cpu_cores=4,
            gpu_units=0,
            name="test-job",
        )

        assert job.submit_time == submit
        assert job.deadline == deadline
        assert job.runtime == timedelta(hours=1)
        assert job.cpu_cores == 4
        assert job.gpu_units == 0
        assert job.name == "test-job"

    def test_parse_runtime_string(self) -> None:
        """Should parse runtime from string format."""
        submit = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
        deadline = datetime(2026, 7, 1, 18, 0, 0, tzinfo=UTC)

        job = TraceJob(
            submit_time=submit,
            deadline=deadline,
            runtime="1h30m",
            cpu_cores=4,
        )

        assert job.runtime == timedelta(hours=1, minutes=30)

    def test_slack_calculation(self) -> None:
        """Should calculate slack correctly."""
        submit = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
        deadline = datetime(2026, 7, 1, 14, 0, 0, tzinfo=UTC)

        job = TraceJob(
            submit_time=submit,
            deadline=deadline,
            runtime=timedelta(hours=2),
            cpu_cores=4,
        )

        assert job.slack == timedelta(hours=4)

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Should serialize and deserialize correctly."""
        submit = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
        deadline = datetime(2026, 7, 1, 18, 0, 0, tzinfo=UTC)

        original = TraceJob(
            submit_time=submit,
            deadline=deadline,
            runtime=timedelta(hours=1),
            cpu_cores=8,
            gpu_units=2,
            name="roundtrip-test",
        )

        data = original.to_dict()
        restored = TraceJob.from_dict(data)

        assert restored.submit_time == original.submit_time
        assert restored.deadline == original.deadline
        assert restored.runtime == original.runtime
        assert restored.cpu_cores == original.cpu_cores
        assert restored.gpu_units == original.gpu_units
        assert restored.name == original.name


class TestJobTrace:
    """Tests for JobTrace model."""

    def test_create_empty_trace(self) -> None:
        """Should create an empty trace."""
        trace = JobTrace()
        assert len(trace) == 0
        assert trace.start_time is None
        assert trace.end_time is None

    def test_create_trace_with_jobs(self) -> None:
        """Should create a trace with jobs."""
        submit1 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
        submit2 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=UTC)

        jobs = [
            TraceJob(
                submit_time=submit1,
                deadline=submit1 + timedelta(hours=6),
                runtime=timedelta(hours=1),
                cpu_cores=4,
            ),
            TraceJob(
                submit_time=submit2,
                deadline=submit2 + timedelta(hours=8),
                runtime=timedelta(hours=2),
                cpu_cores=8,
            ),
        ]

        trace = JobTrace(jobs=jobs)

        assert len(trace) == 2
        assert trace.start_time == submit1
        assert trace.end_time == submit2 + timedelta(hours=8)

    def test_save_and_load(self) -> None:
        """Should save to file and load correctly."""
        submit = datetime(2026, 7, 1, 8, 0, 0, tzinfo=UTC)
        jobs = [
            TraceJob(
                submit_time=submit,
                deadline=submit + timedelta(hours=6),
                runtime=timedelta(hours=1),
                cpu_cores=4,
                name="save-test",
            ),
        ]
        trace = JobTrace(jobs=jobs, metadata={"test": True})

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            trace_path = Path(f.name)

        try:
            trace.save(trace_path)

            loaded = JobTrace.load(trace_path)

            assert len(loaded) == 1
            assert loaded[0].name == "save-test"
            assert loaded[0].cpu_cores == 4
        finally:
            trace_path.unlink()

    def test_load_simple_job_list(self) -> None:
        """Should load a simple JSON list of jobs."""
        submit = datetime(2026, 7, 1, 8, 0, 0)

        job_list = [
            {
                "submit_time": submit.isoformat(),
                "deadline": (submit + timedelta(hours=10)).isoformat(),
                "runtime": "1h",
                "cpu_cores": 4,
            }
        ]

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(job_list, f)
            trace_path = Path(f.name)

        try:
            loaded = JobTrace.load(trace_path)
            assert len(loaded) == 1
            assert loaded[0].cpu_cores == 4
        finally:
            trace_path.unlink()


class TestSyntheticTraceGenerator:
    """Tests for SyntheticTraceGenerator."""

    def test_generate_basic(self) -> None:
        """Should generate the requested number of jobs."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC)

        generator = SyntheticTraceGenerator(seed=42)
        trace = generator.generate(
            num_jobs=50,
            start_time=start,
            end_time=end,
        )

        assert len(trace) == 50
        assert trace.start_time is not None
        assert trace.end_time is not None

    def test_determinism_with_seed(self) -> None:
        """Same seed should produce identical traces."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC)

        generator1 = SyntheticTraceGenerator(seed=12345)
        trace1 = generator1.generate(num_jobs=20, start_time=start, end_time=end)

        generator2 = SyntheticTraceGenerator(seed=12345)
        trace2 = generator2.generate(num_jobs=20, start_time=start, end_time=end)

        assert len(trace1) == len(trace2)
        for j1, j2 in zip(trace1.jobs, trace2.jobs, strict=True):
            assert j1.submit_time == j2.submit_time
            assert j1.deadline == j2.deadline
            assert j1.runtime == j2.runtime
            assert j1.cpu_cores == j2.cpu_cores
            assert j1.gpu_units == j2.gpu_units

    def test_different_seeds_produce_different_traces(self) -> None:
        """Different seeds should produce different traces."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC)

        generator1 = SyntheticTraceGenerator(seed=111)
        trace1 = generator1.generate(num_jobs=20, start_time=start, end_time=end)

        generator2 = SyntheticTraceGenerator(seed=222)
        trace2 = generator2.generate(num_jobs=20, start_time=start, end_time=end)

        different_count = sum(
            1
            for j1, j2 in zip(trace1.jobs, trace2.jobs, strict=True)
            if j1.submit_time != j2.submit_time
        )

        assert different_count > 0

    def test_jobs_respect_time_constraints(self) -> None:
        """All jobs should have submit times and deadlines within range."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC)

        generator = SyntheticTraceGenerator(seed=42)
        trace = generator.generate(
            num_jobs=100,
            start_time=start,
            end_time=end,
        )

        for job in trace.jobs:
            assert job.submit_time >= start
            assert job.deadline <= end + timedelta(hours=1)
            assert job.submit_time < job.deadline

    def test_jobs_sorted_by_submit_time(self) -> None:
        """Generated jobs should be sorted by submit time."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC)

        generator = SyntheticTraceGenerator(seed=42)
        trace = generator.generate(num_jobs=50, start_time=start, end_time=end)

        for i in range(len(trace.jobs) - 1):
            assert trace.jobs[i].submit_time <= trace.jobs[i + 1].submit_time

    def test_custom_slack_range(self) -> None:
        """Should respect custom slack range."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC)
        slack_min = timedelta(hours=2)
        slack_max = timedelta(hours=4)

        generator = SyntheticTraceGenerator(seed=42)
        trace = generator.generate(
            num_jobs=50,
            start_time=start,
            end_time=end,
            slack_min=slack_min,
            slack_max=slack_max,
        )

        for job in trace.jobs:
            calculated_slack = job.deadline - job.submit_time - job.runtime
            assert calculated_slack >= slack_min - timedelta(seconds=1)
            assert calculated_slack <= slack_max + timedelta(hours=1)

    def test_metadata_stored(self) -> None:
        """Should store generation metadata."""
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        end = datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC)

        generator = SyntheticTraceGenerator(seed=42)
        trace = generator.generate(num_jobs=10, start_time=start, end_time=end)

        assert trace.metadata.get("generated") is True
        assert trace.metadata.get("seed") == 42
        assert trace.metadata.get("num_jobs") == 10


class TestParseSlackRange:
    """Tests for parse_slack_range function."""

    def test_parse_hours(self) -> None:
        """Should parse hour ranges."""
        min_slack, max_slack = parse_slack_range("1h-6h")
        assert min_slack == timedelta(hours=1)
        assert max_slack == timedelta(hours=6)

    def test_parse_minutes(self) -> None:
        """Should parse minute ranges."""
        min_slack, max_slack = parse_slack_range("30m-90m")
        assert min_slack == timedelta(minutes=30)
        assert max_slack == timedelta(minutes=90)

    def test_parse_mixed(self) -> None:
        """Should parse mixed formats."""
        min_slack, max_slack = parse_slack_range("30m-2h")
        assert min_slack == timedelta(minutes=30)
        assert max_slack == timedelta(hours=2)

    def test_swap_if_reversed(self) -> None:
        """Should swap if min > max."""
        min_slack, max_slack = parse_slack_range("6h-1h")
        assert min_slack == timedelta(hours=1)
        assert max_slack == timedelta(hours=6)

    def test_invalid_format(self) -> None:
        """Should raise ValueError for invalid format."""
        with pytest.raises(ValueError):
            parse_slack_range("invalid")


class TestHistoricalData:
    """Tests for HistoricalData."""

    def test_create_historical_data(self) -> None:
        """Should create historical data from points."""
        points = [
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 0, 0, 0),
                renewable_share_pct=50.0,
            ),
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 1, 0, 0),
                renewable_share_pct=60.0,
            ),
        ]

        data = HistoricalData(points=points)

        assert len(data.points) == 2
        assert data.start_time == datetime(2026, 7, 1, 0, 0, 0)
        assert data.end_time == datetime(2026, 7, 1, 1, 0, 0)

    def test_get_renewable_share_at_exact_time(self) -> None:
        """Should return exact value at matching timestamp."""
        points = [
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 0, 0, 0),
                renewable_share_pct=50.0,
            ),
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 1, 0, 0),
                renewable_share_pct=70.0,
            ),
        ]
        data = HistoricalData(points=points)

        share = data.get_renewable_share_at(datetime(2026, 7, 1, 0, 0, 0))
        assert share == 50.0

    def test_get_renewable_share_interpolation(self) -> None:
        """Should interpolate between points."""
        points = [
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 0, 0, 0),
                renewable_share_pct=50.0,
            ),
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 1, 0, 0),
                renewable_share_pct=70.0,
            ),
        ]
        data = HistoricalData(points=points)

        share = data.get_renewable_share_at(datetime(2026, 7, 1, 0, 30, 0))
        assert share == pytest.approx(60.0, rel=0.01)

    def test_get_average_renewable_share(self) -> None:
        """Should calculate average over a window.

        The average is calculated by sampling at the interval.
        With sample_interval=1h, it samples at 0h and 1h (not 2h, which is the end).
        So the average is (50 + 60) / 2 = 55.
        """
        points = [
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 0, 0, 0),
                renewable_share_pct=50.0,
            ),
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 1, 0, 0),
                renewable_share_pct=60.0,
            ),
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 2, 0, 0),
                renewable_share_pct=70.0,
            ),
        ]
        data = HistoricalData(points=points)

        avg = data.get_average_renewable_share(
            datetime(2026, 7, 1, 0, 0, 0),
            datetime(2026, 7, 1, 2, 0, 0),
            sample_interval=timedelta(hours=1),
        )

        assert avg == pytest.approx(55.0, rel=0.01)

    def test_serialization_roundtrip(self) -> None:
        """Should serialize and deserialize correctly."""
        points = [
            HistoricalDataPoint(
                timestamp=datetime(2026, 7, 1, 0, 0, 0),
                renewable_share_pct=50.0,
            ),
        ]
        original = HistoricalData(points=points)

        data_dict = original.to_dict()
        restored = HistoricalData.from_dict(data_dict)

        assert len(restored.points) == 1
        assert restored.points[0].renewable_share_pct == 50.0


class TestHistoricalDataStore:
    """Tests for HistoricalDataStore."""

    def test_save_and_load(self) -> None:
        """Should save and load data correctly."""
        from datetime import date

        with tempfile.TemporaryDirectory() as tmpdir:
            store = HistoricalDataStore(Path(tmpdir))

            points = [
                HistoricalDataPoint(
                    timestamp=datetime(2026, 7, 1, 0, 0, 0),
                    renewable_share_pct=50.0,
                ),
            ]
            data = HistoricalData(points=points)

            store.save(data, date(2026, 7, 1), date(2026, 7, 1))

            loaded = store.load()

            assert loaded is not None
            assert len(loaded.points) == 1

    def test_has_data(self) -> None:
        """Should correctly report if data exists."""
        from datetime import date

        with tempfile.TemporaryDirectory() as tmpdir:
            store = HistoricalDataStore(Path(tmpdir))

            assert store.has_data() is False

            points = [
                HistoricalDataPoint(
                    timestamp=datetime(2026, 7, 1, 0, 0, 0),
                    renewable_share_pct=50.0,
                ),
            ]
            store.save(HistoricalData(points=points), date(2026, 7, 1), date(2026, 7, 1))

            assert store.has_data() is True

    def test_covers_range(self) -> None:
        """Should correctly check if stored data covers a range."""
        from datetime import date

        with tempfile.TemporaryDirectory() as tmpdir:
            store = HistoricalDataStore(Path(tmpdir))

            points = [
                HistoricalDataPoint(
                    timestamp=datetime(2026, 7, 1, 0, 0, 0),
                    renewable_share_pct=50.0,
                ),
            ]
            store.save(
                HistoricalData(points=points),
                date(2026, 7, 1),
                date(2026, 7, 31),
            )

            assert store.covers_range(date(2026, 7, 1), date(2026, 7, 31)) is True
            assert store.covers_range(date(2026, 7, 1), date(2026, 7, 15)) is True
            assert store.covers_range(date(2026, 6, 1), date(2026, 7, 31)) is False
            assert store.covers_range(date(2026, 7, 1), date(2026, 8, 15)) is False


class TestBacktest:
    """Tests for Backtest engine."""

    @pytest.fixture
    def sample_historical_data(self) -> HistoricalData:
        """Create sample historical data for testing."""
        points: list[HistoricalDataPoint] = []
        start = datetime(2026, 7, 1, 0, 0, 0)

        for hour in range(168):
            renewable = 40.0 + 30.0 * ((hour % 24) / 24.0)
            if 10 <= (hour % 24) <= 16:
                renewable += 20.0

            points.append(
                HistoricalDataPoint(
                    timestamp=start + timedelta(hours=hour),
                    renewable_share_pct=min(renewable, 100.0),
                )
            )

        return HistoricalData(points=points)

    @pytest.fixture
    def sample_trace(self) -> JobTrace:
        """Create sample job trace for testing."""
        start = datetime(2026, 7, 1, 8, 0, 0)
        jobs = [
            TraceJob(
                submit_time=start + timedelta(hours=i * 2),
                deadline=start + timedelta(hours=i * 2 + 8),
                runtime=timedelta(hours=1),
                cpu_cores=4,
                name=f"job-{i:03d}",
            )
            for i in range(10)
        ]
        return JobTrace(jobs=jobs)

    def test_run_single_policy(
        self,
        sample_historical_data: HistoricalData,
        sample_trace: JobTrace,
    ) -> None:
        """Should run a single policy and return results."""
        backtest = Backtest(sample_historical_data)
        result = backtest.run_policy(sample_trace, "immediate")

        assert result.total_jobs == 10
        assert len(result.job_results) == 10
        assert result.policy_name == "immediate"

    def test_run_multiple_policies(
        self,
        sample_historical_data: HistoricalData,
        sample_trace: JobTrace,
    ) -> None:
        """Should run multiple policies and compare."""
        backtest = Backtest(sample_historical_data)
        result = backtest.run(
            sample_trace,
            ["immediate", "threshold", "greenest-window"],
        )

        assert len(result.policy_results) == 3
        assert "immediate" in result.policy_results
        assert "threshold" in result.policy_results
        assert "greenest-window" in result.policy_results

    def test_backtest_determinism(
        self,
        sample_historical_data: HistoricalData,
    ) -> None:
        """Same inputs should produce identical results."""
        generator = SyntheticTraceGenerator(seed=42)
        start = datetime(2026, 7, 1, 0, 0, 0)
        end = datetime(2026, 7, 7, 23, 59, 59)
        trace = generator.generate(num_jobs=20, start_time=start, end_time=end)

        config = PolicyConfig(renewable_threshold_pct=60)

        backtest1 = Backtest(sample_historical_data, config=config)
        result1 = backtest1.run(trace, ["immediate", "threshold"])

        backtest2 = Backtest(sample_historical_data, config=config)
        result2 = backtest2.run(trace, ["immediate", "threshold"])

        for policy in ["immediate", "threshold"]:
            r1 = result1.policy_results[policy]
            r2 = result2.policy_results[policy]

            assert r1.total_co2_grams == r2.total_co2_grams
            assert r1.mean_delay_seconds == r2.mean_delay_seconds
            assert r1.deadline_misses == r2.deadline_misses

    def test_deadline_guarantee(
        self,
        sample_historical_data: HistoricalData,
        sample_trace: JobTrace,
    ) -> None:
        """All policies should meet deadlines (target: 0 misses)."""
        backtest = Backtest(sample_historical_data)
        result = backtest.run(
            sample_trace,
            ["immediate", "threshold", "greenest-window"],
        )

        for policy_name, policy_result in result.policy_results.items():
            assert policy_result.deadline_misses == 0, (
                f"Policy {policy_name} had {policy_result.deadline_misses} deadline misses"
            )

    def test_avoided_co2_calculation(
        self,
        sample_historical_data: HistoricalData,
        sample_trace: JobTrace,
    ) -> None:
        """Should calculate avoided CO2 correctly."""
        backtest = Backtest(sample_historical_data)
        result = backtest.run(sample_trace, ["immediate", "threshold"])

        avoided = result.get_avoided_co2("threshold")

        immediate_co2 = result.policy_results["immediate"].total_co2_grams
        threshold_co2 = result.policy_results["threshold"].total_co2_grams

        assert avoided == immediate_co2 - threshold_co2

        baseline_avoided = result.get_avoided_co2("immediate")
        assert baseline_avoided == 0.0


class TestBacktestResult:
    """Tests for BacktestResult."""

    def test_csv_roundtrip(self) -> None:
        """Should save and load CSV correctly."""
        result = BacktestResult()
        result.policy_results["immediate"] = PolicyResult(
            policy_name="immediate",
            job_results=[
                JobResult(
                    job_name="test-job",
                    submit_time=datetime(2026, 7, 1, 8, 0, 0),
                    deadline=datetime(2026, 7, 1, 16, 0, 0),
                    runtime=timedelta(hours=1),
                    scheduled_start=datetime(2026, 7, 1, 8, 0, 0),
                    scheduled_end=datetime(2026, 7, 1, 9, 0, 0),
                    delay=timedelta(0),
                    renewable_share_pct=50.0,
                    estimated_energy_kwh=0.04,
                    estimated_co2_grams=8.0,
                    deadline_met=True,
                    is_fallback=False,
                    policy="immediate",
                    cpu_cores=4,
                    gpu_units=0,
                ),
            ],
        )

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            csv_path = Path(f.name)

        try:
            result.save_csv(csv_path)
            loaded = BacktestResult.load_csv(csv_path)

            assert "immediate" in loaded.policy_results
            assert loaded.policy_results["immediate"].total_jobs == 1
            assert loaded.policy_results["immediate"].job_results[0].job_name == "test-job"
        finally:
            csv_path.unlink()

    def test_csv_has_correct_columns(self) -> None:
        """CSV should have all required columns."""
        result = BacktestResult()
        result.policy_results["immediate"] = PolicyResult(
            policy_name="immediate",
            job_results=[
                JobResult(
                    job_name="test",
                    submit_time=datetime(2026, 7, 1, 8, 0, 0),
                    deadline=datetime(2026, 7, 1, 16, 0, 0),
                    runtime=timedelta(hours=1),
                    scheduled_start=datetime(2026, 7, 1, 8, 0, 0),
                    scheduled_end=datetime(2026, 7, 1, 9, 0, 0),
                    delay=timedelta(0),
                    renewable_share_pct=50.0,
                    estimated_energy_kwh=0.04,
                    estimated_co2_grams=8.0,
                    deadline_met=True,
                    is_fallback=False,
                    policy="immediate",
                    cpu_cores=4,
                    gpu_units=0,
                ),
            ],
        )

        rows = result.to_csv_rows()

        required_columns = [
            "policy",
            "job_name",
            "submit_time",
            "deadline",
            "runtime_seconds",
            "scheduled_start",
            "scheduled_end",
            "delay_seconds",
            "renewable_share_pct",
            "estimated_energy_kwh",
            "estimated_co2_grams",
            "deadline_met",
            "is_fallback",
            "cpu_cores",
            "gpu_units",
        ]

        for col in required_columns:
            assert col in rows[0], f"Missing column: {col}"


class TestPolicyResult:
    """Tests for PolicyResult."""

    def test_aggregate_metrics(self) -> None:
        """Should calculate aggregate metrics correctly."""
        result = PolicyResult(
            policy_name="test",
            job_results=[
                JobResult(
                    job_name="job1",
                    submit_time=datetime(2026, 7, 1, 8, 0, 0),
                    deadline=datetime(2026, 7, 1, 16, 0, 0),
                    runtime=timedelta(hours=1),
                    scheduled_start=datetime(2026, 7, 1, 10, 0, 0),
                    scheduled_end=datetime(2026, 7, 1, 11, 0, 0),
                    delay=timedelta(hours=2),
                    renewable_share_pct=60.0,
                    estimated_energy_kwh=0.04,
                    estimated_co2_grams=10.0,
                    deadline_met=True,
                    is_fallback=False,
                    policy="test",
                    cpu_cores=4,
                    gpu_units=0,
                ),
                JobResult(
                    job_name="job2",
                    submit_time=datetime(2026, 7, 1, 9, 0, 0),
                    deadline=datetime(2026, 7, 1, 17, 0, 0),
                    runtime=timedelta(hours=1),
                    scheduled_start=datetime(2026, 7, 1, 9, 0, 0),
                    scheduled_end=datetime(2026, 7, 1, 10, 0, 0),
                    delay=timedelta(0),
                    renewable_share_pct=40.0,
                    estimated_energy_kwh=0.04,
                    estimated_co2_grams=20.0,
                    deadline_met=True,
                    is_fallback=True,
                    policy="test",
                    cpu_cores=4,
                    gpu_units=0,
                ),
            ],
        )

        assert result.total_jobs == 2
        assert result.deadline_misses == 0
        assert result.deadline_miss_rate == 0.0
        assert result.total_co2_grams == 30.0
        assert result.total_energy_kwh == 0.08
        assert result.mean_delay_seconds == 3600.0
        assert result.mean_renewable_share == 50.0
        assert result.fallback_count == 1

    def test_deadline_miss_rate(self) -> None:
        """Should calculate deadline miss rate correctly."""
        result = PolicyResult(
            policy_name="test",
            job_results=[
                JobResult(
                    job_name="job1",
                    submit_time=datetime(2026, 7, 1, 8, 0, 0),
                    deadline=datetime(2026, 7, 1, 16, 0, 0),
                    runtime=timedelta(hours=1),
                    scheduled_start=datetime(2026, 7, 1, 8, 0, 0),
                    scheduled_end=datetime(2026, 7, 1, 9, 0, 0),
                    delay=timedelta(0),
                    renewable_share_pct=50.0,
                    estimated_energy_kwh=0.04,
                    estimated_co2_grams=10.0,
                    deadline_met=True,
                    is_fallback=False,
                    policy="test",
                    cpu_cores=4,
                    gpu_units=0,
                ),
                JobResult(
                    job_name="job2",
                    submit_time=datetime(2026, 7, 1, 9, 0, 0),
                    deadline=datetime(2026, 7, 1, 10, 0, 0),
                    runtime=timedelta(hours=2),
                    scheduled_start=datetime(2026, 7, 1, 9, 0, 0),
                    scheduled_end=datetime(2026, 7, 1, 11, 0, 0),
                    delay=timedelta(0),
                    renewable_share_pct=50.0,
                    estimated_energy_kwh=0.08,
                    estimated_co2_grams=20.0,
                    deadline_met=False,
                    is_fallback=False,
                    policy="test",
                    cpu_cores=4,
                    gpu_units=0,
                ),
            ],
        )

        assert result.deadline_misses == 1
        assert result.deadline_miss_rate == 0.5
