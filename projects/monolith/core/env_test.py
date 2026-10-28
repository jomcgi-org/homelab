from core.env import flag


def test_flag_accepts_only_true_in_any_case(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert flag("X_FLAG") is False
    for value in ("true", "TRUE", "True"):
        monkeypatch.setenv("X_FLAG", value)
        assert flag("X_FLAG") is True
    for value in ("1", "yes", " true", "false", ""):
        monkeypatch.setenv("X_FLAG", value)
        assert flag("X_FLAG") is False
