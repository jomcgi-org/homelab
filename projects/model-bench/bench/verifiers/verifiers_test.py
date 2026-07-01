from bench.verifiers import get_verifier, VerifyResult


def test_dispatch_unknown_kind_raises():
    import pytest

    with pytest.raises(KeyError):
        get_verifier("nope")


def test_command_verifier_passes_on_zero_exit(tmp_path):
    v = get_verifier("command")
    r = v(tmp_path, {"cmd": ["sh", "-c", "exit 0"]})
    assert r.passed


def test_command_verifier_reports_stderr_on_fail(tmp_path):
    v = get_verifier("command")
    r = v(tmp_path, {"cmd": ["sh", "-c", "echo boom 1>&2; exit 1"]})
    assert not r.passed and "boom" in r.feedback


def test_compile_python_detects_syntax_error(tmp_path):
    (tmp_path / "m.py").write_text("def f(:\n")
    v = get_verifier("py-compile")
    r = v(tmp_path, {"file": "m.py"})
    assert not r.passed and "SyntaxError" in r.feedback


def test_command_write_files_drops_hidden_test(tmp_path):
    # write_files drops a hidden grading file into the workdir before the command runs.
    v = get_verifier("command")
    r = v(
        tmp_path,
        {
            "write_files": {"check.py": "open('marker','w').write('x')\n"},
            "cmd": ["python3", "check.py"],
        },
    )
    assert r.passed and (tmp_path / "marker").exists()


def test_pytest_verifier_registers():
    # The import root differs between a bare `python3 -m bench` run (module is
    # "bench.verifiers.pytest") and bazel's imports=["../.."] ("verifiers.pytest"),
    # so match the stable suffix rather than the full dotted path.
    assert get_verifier("pytest").__module__.endswith("verifiers.pytest")


def test_pytest_verifier_resolves_venv_precedence(tmp_path, monkeypatch):
    from bench.verifiers.pytest import _venv_python

    # Explicit args["python"] wins over the env var.
    assert _venv_python({"python": "/x/py"}) == __import__("pathlib").Path("/x/py")
    # Else $MODEL_BENCH_VENV/bin/python.
    monkeypatch.setenv("MODEL_BENCH_VENV", str(tmp_path))
    assert _venv_python({}) == tmp_path / "bin" / "python"


def test_pytest_verifier_reports_setup_error_when_venv_missing(tmp_path, monkeypatch):
    # No real venv on CI: the verifier must fail cleanly with a setup message rather
    # than crash, and it must not run any gold test. (The full drop-test-and-run path
    # is validated locally against the monolith venv, which CI does not provision.)
    monkeypatch.setenv("MODEL_BENCH_VENV", str(tmp_path / "nonexistent"))
    v = get_verifier("pytest")
    r = v(tmp_path, {"tests": {"t_test.py": "def test_x():\n    assert True\n"}})
    assert not r.passed and "venv python not found" in r.feedback
