from shared.env import float_env, int_env


def test_int_env_accepts_only_positive_integers(monkeypatch):
    monkeypatch.delenv("KNOB", raising=False)
    assert int_env("KNOB", 3) == 3
    monkeypatch.setenv("KNOB", "5")
    assert int_env("KNOB", 3) == 5
    monkeypatch.setenv("KNOB", "0")
    assert int_env("KNOB", 3) == 3
    monkeypatch.setenv("KNOB", "many")
    assert int_env("KNOB", 3) == 3


def test_float_env_falls_back_on_missing_or_garbage(monkeypatch):
    monkeypatch.delenv("KNOB", raising=False)
    assert float_env("KNOB", 0.5) == 0.5
    monkeypatch.setenv("KNOB", "0.25")
    assert float_env("KNOB", 0.5) == 0.25
    monkeypatch.setenv("KNOB", "-1")
    assert float_env("KNOB", 0.5) == -1.0
    monkeypatch.setenv("KNOB", "nope")
    assert float_env("KNOB", 0.5) == 0.5
