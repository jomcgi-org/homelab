"""Verify that a configured target receives the right historical signals."""

from unittest.mock import Mock, patch

import numpy as np

from worker import HORIZONS, STEP, collect_profiles, render_metrics

END = 604800
PROFILE = {
    "namespace": "trial",
    "workload": "example",
    "target": "configured_memory_query",
    "past_covariates": ["configured_cpu_query", "configured_queue_query"],
    "min_samples": 6,
}


def samples(value):
    return [(END - delta * STEP, str(value)) for delta in range(5, -1, -1)]


def test_configured_target_joins_pod_and_shared_signals_on_the_time_grid():
    # The CPU signal has a gap and another pod's data. Both must stay separate
    # from the target and the workload-wide queue signal.
    cpu = samples(3)
    cpu.insert(0, (END - 6 * STEP, "3"))
    cpu.pop(-2)
    inputs = {
        PROFILE["target"]: {"pod-a": samples(650)},
        "configured_cpu_query": {"pod-a": cpu, "pod-b": samples(99)},
        "configured_queue_query": {"": samples(4)},
    }
    model = Mock()
    model.forecast.return_value.quantile.return_value = np.full(HORIZONS, 700)

    with patch(
        "worker.read_history", side_effect=lambda _, __, query: inputs[query]
    ) as read:
        points = collect_profiles(model, "http://prometheus", [PROFILE], END)

    assert [call.args[2] for call in read.call_args_list] == list(inputs)
    (observed,) = model.forecast.call_args.args
    np.testing.assert_array_equal(observed[-6:], [650] * 6)
    past = model.forecast.call_args.kwargs["past_covariates"]
    np.testing.assert_equal(past[0, :, -6:], [[3, 3, 3, 3, np.nan, 3], [4] * 6])
    assert model.forecast.call_args.kwargs["prediction_length"] == 6
    assert len(points) == 12
    assert (
        'namespace="trial",workload="example",pod="pod-a",policy="timestral"'
        in render_metrics(points, END)
    )


def test_unavailable_optional_signal_keeps_memory_forecast():
    model = Mock()
    model.forecast.return_value.quantile.return_value = np.full(HORIZONS, 700)

    def history(_url, _end, query):
        if query == PROFILE["target"]:
            return {"pod-a": samples(650)}
        raise TimeoutError("forecast signal unavailable")

    with patch("worker.read_history", side_effect=history):
        points = collect_profiles(model, "http://prometheus", [PROFILE], END)

    assert model.forecast.call_args.kwargs["past_covariates"] is None
    assert len(points) == 12


if __name__ == "__main__":
    test_configured_target_joins_pod_and_shared_signals_on_the_time_grid()
    test_unavailable_optional_signal_keeps_memory_forecast()
