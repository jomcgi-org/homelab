import json

import pytest

from factory.orchestration import factory_conductor as conductor


def test_phase_diagnostics_bound_reads_to_one_tick_and_fixed_phases():
    from factory.orchestration.github_read_cooldown import FactoryPhaseDiagnostics

    clock = [0.0]
    samples, reads = [], []

    def memory():
        reads.append(clock[0])
        return {"rss_bytes": 123, "cgroup_oom_kills": 0}

    diagnostics = FactoryPhaseDiagnostics(
        monotonic=lambda: clock[0], memory=memory, emit=samples.append
    )
    checkpoint = diagnostics.begin()
    clock[0] = 2
    checkpoint("landing")
    checkpoint("landing")
    checkpoint("private task text must never become a label")
    clock[0] = 7
    checkpoint("end")
    assert [s["phase"] for s in samples] == ["snapshot", "landing", "end"]
    assert [s["previous_elapsed_seconds"] for s in samples] == [0, 2, 5]
    assert samples[-1]["tick_elapsed_seconds"] == 7
    for _ in range(100):
        diagnostics.begin()("end")
    assert reads == [0, 2, 7]
    clock[0] = 60
    next_tick = diagnostics.begin()
    next_tick("end")
    assert len(reads) == 5
    assert "private" not in json.dumps(samples)
    assert len(samples) <= 2 * len(diagnostics.PHASES)


@pytest.mark.parametrize("failure", ["clock", "memory", "emit"])
def test_phase_diagnostic_failure_does_not_change_tick_execution(monkeypatch, failure):
    from factory.orchestration.github_read_cooldown import FactoryPhaseDiagnostics

    def fail(*_args):
        raise RuntimeError("diagnostics unavailable")

    arguments = {
        "monotonic": lambda: 0.0,
        "memory": dict,
        "emit": lambda _s: None,
    }
    arguments[{"clock": "monotonic", "memory": "memory", "emit": "emit"}[failure]] = (
        fail
    )
    monkeypatch.setattr(
        conductor, "_phase_diagnostics", FactoryPhaseDiagnostics(**arguments)
    )
    ran = []
    monkeypatch.setattr(
        conductor, "_tick", lambda checkpoint: (ran.append(True), checkpoint("landing"))
    )
    conductor.tick()
    assert ran == [True]


def test_phase_diagnostics_record_exception_boundary_without_swallowing(monkeypatch):
    from factory.orchestration.github_read_cooldown import FactoryPhaseDiagnostics

    samples = []
    monkeypatch.setattr(
        conductor,
        "_phase_diagnostics",
        FactoryPhaseDiagnostics(
            monotonic=lambda: 0.0,
            memory=dict,
            emit=samples.append,
        ),
    )
    failure = ValueError("the original control error")

    def broken(checkpoint):
        checkpoint("landing")
        raise failure

    monkeypatch.setattr(conductor, "_tick", broken)
    with pytest.raises(ValueError) as raised:
        conductor.tick()
    assert raised.value is failure
    assert [s["phase"] for s in samples] == ["snapshot", "landing", "end"]
    assert "control error" not in json.dumps(samples)
