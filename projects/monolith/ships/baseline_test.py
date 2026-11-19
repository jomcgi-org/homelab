"""The HOME-483 baseline: log parsing, sampling, resume and the JSON shape."""

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from ships import baseline

START = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
M0 = int(START.timestamp()) // 60


def log_line(at: datetime, message: str) -> str:
    return f"{at.strftime('%Y-%m-%dT%H:%M:%S')}.123456789Z {message}\n"


def frames(minute: int, count: int) -> str:
    return f"INFO monolith.ships: ships AIS frames minute={minute} count={count}"


def test_frame_entries_reads_new_complete_frame_lines():
    text = (
        log_line(START, frames(M0 - 1, 50))
        + log_line(START + timedelta(seconds=1), "INFO uvicorn: GET /health 200")
        + log_line(START + timedelta(minutes=1), frames(M0, 600))
        + "2026-11-20T00:02:00.5Z INFO monolith.ships: ships AIS frames minute="
    )
    cursor = baseline.stamp_ns("2026-11-20T00:00:00.123456789Z")

    entries, newest = baseline.frame_entries(text, "backend-a", cursor)

    # The first line is at the cursor, so an earlier read counted it. The
    # cut-off last line waits for the next read.
    assert entries == [[M0, "backend-a", 600]]
    assert newest == baseline.stamp_ns("2026-11-20T00:01:00.123456789Z")


def test_usage_samples_reads_the_named_container():
    metrics = {
        "items": [
            {
                "metadata": {"name": "backend-a"},
                "timestamp": "2026-11-20T00:00:30Z",
                "containers": [
                    {
                        "name": "backend",
                        "usage": {"cpu": "250000000n", "memory": "512Mi"},
                    },
                    {"name": "sidecar", "usage": {"cpu": "1", "memory": "1Gi"}},
                ],
            }
        ]
    }

    assert baseline.usage_samples(metrics, "backend") == [
        [int(START.timestamp()) + 30, "backend-a", 0.25, 512 * 1024**2]
    ]


@pytest.mark.parametrize(
    ("quantity", "cores"), [("618m", 0.618), ("1500u", 0.0015), ("2", 2.0)]
)
def test_cpu_quantities(quantity, cores):
    assert baseline.cpu_cores(quantity) == pytest.approx(cores)


def positions():
    return {
        "rows": 10,
        "partition_bytes": 8192,
        "index_bytes": 4096,
        "partitions": [
            {
                "name": "ships.positions_20261120",
                "rows": 10,
                "partition_bytes": 8192,
                "index_bytes": 4096,
            }
        ],
        "source": baseline.POSITIONS_SOURCE,
        "observed_at": "2026-11-20T00:03:00Z",
    }


def test_summary_shape_and_values():
    state = baseline.new_state("ships-baseline-1", START, 3)
    state["container"] = "backend"
    state["series"]["frames"] = [
        [M0 - 1, "backend-a", 9000],  # before the window
        [M0, "backend-a", 600],
        [M0 + 1, "backend-a", 300],
        [M0 + 1, "backend-a", 300],  # a reconnect logs the minute twice
        [M0 + 2, "backend-b", 1200],  # the leader moved
        [M0 + 3, "backend-b", 9000],  # after the window
    ]
    t0 = int(START.timestamp())
    state["series"]["usage"] = [
        [t0 + 10, "backend-a", 0.2, 100],
        [t0 + 10, "backend-b", 5.0, 999],  # not the leader in this minute
        [t0 + 70, "backend-a", 0.4, 300],
        [t0 + 130, "backend-b", 0.6, 200],
    ]

    result = baseline.summarize(state, positions())

    assert list(result) == [
        "schema",
        "run",
        "complete",
        "window",
        "messages_per_second",
        "positions",
        "ingest_container",
        "cursors",
        "series",
    ]
    assert result["schema"] == "ships-baseline/v1"
    assert result["complete"] is True
    assert result["window"] == {
        "start": "2026-11-20T00:00:00Z",
        "end": "2026-11-20T00:03:00Z",
        "minutes": 3,
    }
    rate = result["messages_per_second"]
    assert (rate["median"], rate["p95"], rate["peak"]) == (10.0, 20.0, 20.0)
    assert rate["minutes_observed"] == 3
    assert rate["window"] == {
        "start": "2026-11-20T00:00:00Z",
        "end": "2026-11-20T00:03:00Z",
    }
    assert rate["source"] == baseline.FRAMES_SOURCE
    usage = result["ingest_container"]
    assert usage["container"] == "backend"
    assert usage["samples"] == 3
    assert usage["cpu_cores"] == {"p50": 0.4, "p95": 0.6, "peak": 0.6}
    assert usage["working_set_bytes"] == {"p50": 200, "p95": 300, "peak": 300}
    assert usage["source"] == baseline.USAGE_SOURCE
    assert result["positions"]["observed_at"] == "2026-11-20T00:03:00Z"
    assert json.loads(json.dumps(result)) == result


def test_summary_with_no_frames_reports_no_rates():
    state = baseline.new_state("ships-baseline-1", START, 3)

    result = baseline.summarize(state, positions())

    assert result["messages_per_second"]["median"] is None
    assert result["messages_per_second"]["minutes_observed"] == 0
    assert result["ingest_container"]["cpu_cores"] is None


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        self.at += timedelta(seconds=seconds)


class FakeKube:
    container = "backend"

    def __init__(self, clock):
        self.clock = clock
        self.metric_reads = 0

    def metrics(self):
        self.metric_reads += 1
        at = self.clock().replace(microsecond=0)
        return {
            "items": [
                {
                    "metadata": {"name": "backend-a"},
                    "timestamp": baseline.iso(at),
                    "containers": [
                        {"name": "backend", "usage": {"cpu": "300m", "memory": "400Mi"}}
                    ],
                }
            ]
        }

    def pods(self):
        return ["backend-a"]

    def logs(self, pod, since):
        # The leader logs each finished minute 5 seconds into the next one.
        # Like the kubelet, return every line at or after sinceTime.
        now = self.clock().timestamp()
        text = ""
        minute = int(since.timestamp()) // 60 - 1
        while (minute + 1) * 60 + 5 <= now:
            at = datetime.fromtimestamp((minute + 1) * 60 + 5, timezone.utc)
            if at >= since:
                text += log_line(at, frames(minute, 600))
            minute += 1
        return text


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            error = Exception("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        assert (Bucket, ContentType, CacheControl) == (
            "stars",
            "application/json",
            "no-store",
        )
        self.objects[Key] = Body
        self.puts.append(json.loads(Body))


@pytest.fixture
def measured(monkeypatch):
    calls = []

    def measure(engine, now):
        calls.append(now())
        return positions()

    monkeypatch.setattr(baseline, "measure_positions", measure)
    return calls


def test_run_samples_the_window_and_writes_one_object(measured):
    clock = Clock(START + timedelta(seconds=20))
    s3 = FakeS3()
    store = baseline.R2Object(s3, "stars", "ships-baseline/run-1.json")

    result = baseline.run(
        run_id="run-1",
        minutes=10,
        kube=FakeKube(clock),
        store=store,
        engine=object(),
        now=clock,
        sleep=clock.sleep,
    )

    assert list(s3.objects) == ["ships-baseline/run-1.json"]
    assert s3.puts[0]["complete"] is False  # the first save proves R2 access
    assert json.loads(s3.objects["ships-baseline/run-1.json"]) == result
    assert result["complete"] is True
    assert result["window"]["start"] == "2026-11-20T00:00:00Z"
    assert result["messages_per_second"]["minutes_observed"] == 10
    assert result["messages_per_second"]["median"] == 10.0
    assert result["ingest_container"]["samples"] == 10
    assert result["ingest_container"]["cpu_cores"]["peak"] == 0.3
    assert measured and measured[0] >= START + timedelta(minutes=10)


def test_retried_pod_resumes_the_same_window(measured):
    clock = Clock(START + timedelta(seconds=20))
    s3 = FakeS3()
    store = baseline.R2Object(s3, "stars", "ships-baseline/run-1.json")
    checkpoint = baseline.new_state("run-1", START, 10)
    checkpoint["series"]["frames"] = [[M0, "backend-a", 600]]
    # The cursor is the stamp of the last line read: minute M0, logged at 00:01:05.
    checkpoint["cursors"] = {
        "backend-a": baseline.stamp_ns("2026-11-20T00:01:05.123456789Z")
    }
    s3.objects[store.key] = json.dumps(checkpoint).encode()
    clock.at = START + timedelta(minutes=4)  # the pod restarted

    result = baseline.run(
        run_id="run-1",
        minutes=1440,  # ignored: the checkpoint fixes the window
        kube=FakeKube(clock),
        store=store,
        engine=object(),
        now=clock,
        sleep=clock.sleep,
    )

    assert result["window"]["minutes"] == 10
    minutes = [entry[0] for entry in result["series"]["frames"]]
    assert sorted(minutes) == minutes
    assert len(minutes) == len(set(minutes)), "a minute was read twice"
    assert result["messages_per_second"]["minutes_observed"] == 10


def test_complete_run_is_not_measured_again(measured):
    s3 = FakeS3()
    store = baseline.R2Object(s3, "stars", "ships-baseline/run-1.json")
    done = baseline.summarize(baseline.new_state("run-1", START, 10), positions())
    s3.objects[store.key] = json.dumps(done).encode()
    clock = Clock(START + timedelta(days=1))

    result = baseline.run(
        run_id="run-1",
        minutes=10,
        kube=FakeKube(clock),
        store=store,
        engine=object(),
        now=clock,
        sleep=clock.sleep,
    )

    assert result == done
    assert s3.puts == [] and measured == []


def test_load_raises_errors_other_than_a_missing_key():
    class Broken:
        def get_object(self, Bucket, Key):
            error = Exception("denied")
            error.response = {"Error": {"Code": "AccessDenied"}}
            raise error

    with pytest.raises(Exception, match="denied"):
        baseline.R2Object(Broken(), "stars", "k").load()


def test_run_from_env_refuses_a_plain_http_endpoint(monkeypatch):
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "http://r2.invalid")
    with pytest.raises(RuntimeError, match="https"):
        baseline.run_from_env()
