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
