import pytest

from factory.orchestration.api import factory_session_allowed


@pytest.mark.parametrize("identity", [None, "local-session", "drainer:kg:1"])
def test_unrelated_sessions_are_outside_factory_stop(identity):
    assert factory_session_allowed(identity)


@pytest.mark.parametrize("identity", ["factory:", "factory:t:n", "factory:t:n:bad"])
def test_malformed_factory_identity_is_refused(identity):
    assert not factory_session_allowed(identity)


def test_pending_execution_rechecks_current_stop(monkeypatch):
    import factory.orchestration.factory_controls as controls

    state = {"ok": True}
    observed = []

    def can_start(task, *, start_key):
        observed.append((task, start_key))
        return state if task == "t-1" else {"ok": False}

    monkeypatch.setattr(controls, "can_start", can_start)
    assert factory_session_allowed("factory:t-1:work:1")
    state["ok"] = False
    assert not factory_session_allowed("factory:t-1:work:1")
    assert observed == [("t-1", "factory-node:t-1:work:1")] * 2
