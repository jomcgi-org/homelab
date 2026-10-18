"""Invoke the real shell uploader with synthetic credentials and a fake rclone.

PATH contains only instrumented local tools. No network or real secret reads.
"""

import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("upload-book.sh")
BASH = shutil.which("bash")
ACCESS = "synthetic-access-never-a-real-key"
SECRET = "synthetic-secret-never-a-real-key"
REQUIRED = (
    "SEAWEEDFS_S3_ENDPOINT",
    "AWS_REGION",
    "GRIMOIRE_S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
)

RCLONE_STUB = r"""
import fnmatch
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
assert args[:3] == ["--config", "/dev/null", "--s3-no-check-bucket"]
assert not any(value in args for value in (os.environ["S3_ACCESS_KEY_ID"], os.environ["S3_SECRET_ACCESS_KEY"]))
keys = ("TYPE", "PROVIDER", "ENDPOINT", "REGION", "FORCE_PATH_STYLE")
record = {"args": args, "config": {key: os.environ["RCLONE_CONFIG_GRIM_" + key] for key in keys}}
record["credentials_match"] = all(os.environ["RCLONE_CONFIG_GRIM_" + key] == os.environ["S3_" + key] for key in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY"))
log = Path(os.environ["RCLONE_CALLS"])
with log.open("a") as stream:
    stream.write(json.dumps(record) + "\n")
if len(log.read_text().splitlines()) == int(os.environ.get("FAIL_CALL", "0")):
    sys.exit(37)
operation, source, destination = args[3:6]
assert operation in ("copy", "copyto"), "no bucket creation or other remote operation"
dest = Path(os.environ["OBJECTS"]) / destination.removeprefix("GRIM:")
if operation == "copyto":
    assert len(args) == 6
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
else:
    assert args[6::2] == ["--include"] * 5
    patterns = args[7::2]
    assert patterns == ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp"]
    for item in Path(source).rglob("*"):
        if item.is_file() and any(fnmatch.fnmatchcase(item.name, p) for p in patterns):
            target = dest / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
"""


@pytest.fixture
def upload(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    source = tmp_path / "book with spaces"
    source.mkdir()
    (source / "output.json").write_bytes(b'{"text": "verbatim raw JSON"}\n')
    temp = tmp_path / "scratch"
    temp.mkdir()
    (temp / "unrelated-file").write_text("preserve")
    # Deliberately do not inherit user credentials, rclone options or config.
    env = {
        "PATH": str(tools),
        "TMPDIR": str(temp),
        "SEAWEEDFS_S3_ENDPOINT": "https://fixture.r2.invalid",
        "AWS_REGION": "auto",
        "GRIMOIRE_S3_BUCKET": "fixture-books",
        "S3_ACCESS_KEY_ID": ACCESS,
        "S3_SECRET_ACCESS_KEY": SECRET,
        "RCLONE_CALLS": str(tmp_path / "rclone.jsonl"),
        "EXTERNAL_CALLS": str(tmp_path / "external.jsonl"),
        "OBJECTS": str(tmp_path / "objects"),
    }
    for name in ("rclone", "gzip", "mktemp", "rm", "kubectl", "nc"):
        target = tools / name
        body = (
            "import os\n"
            "with open(os.environ['EXTERNAL_CALLS'], 'a') as stream:\n"
            f"    stream.write({name!r} + '\\n')\n"
        )
        if name == "rclone":
            body += RCLONE_STUB
        elif name in ("kubectl", "nc"):
            body += "raise AssertionError('forbidden external command')\n"
        else:
            executable = shutil.which(name)
            assert executable, f"test requires local {name}"
            body += f"import sys\nos.execv({executable!r}, [{executable!r}] + sys.argv[1:])\n"
        target.write_text(f"#!{sys.executable}\n" + body)
        target.chmod(0o755)

    def invoke(args=None, overrides=None, trace=False):
        configured = env | (overrides or {})
        command = [BASH, *(["-x"] if trace else []), str(SCRIPT)]
        command += ["book id", str(source)] if args is None else args
        result = subprocess.run(
            command,
            env=configured,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        assert ACCESS not in result.stdout + result.stderr
        assert SECRET not in result.stdout + result.stderr
        assert sorted(p.name for p in temp.iterdir()) == ["unrelated-file"]
        assert (temp / "unrelated-file").read_text() == "preserve"
        return result

    return source, env, invoke


@pytest.mark.parametrize("missing", REQUIRED)
@pytest.mark.parametrize("value", [None, ""])
def test_missing_configuration_runs_no_external_command(upload, missing, value):
    _, env, invoke = upload
    if value is None:
        del env[missing]
    else:
        env[missing] = value
    result = invoke()
    assert result.returncode != 0
    assert f"{missing} is required" in result.stderr
    assert not Path(env["EXTERNAL_CALLS"]).exists()


@pytest.mark.parametrize(
    "case", ["args", "directory", "json", "metadata", "chunks", "book"]
)
def test_invalid_input_runs_no_external_command(upload, case):
    source, env, invoke = upload
    args = None
    if case == "args":
        args = []
    elif case == "directory":
        args = ["book", str(source / "missing")]
    elif case == "json":
        (source / "output.json").unlink()
    elif case == "book":
        args = ["../book", str(source)]
    else:
        (
            source / ("output.metadata.json" if case == "metadata" else "chunks.ndjson")
        ).mkdir()
    assert invoke(args=args).returncode != 0
    assert not Path(env["EXTERNAL_CALLS"]).exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8333",
        "fixture.invalid",
        "https://",
        "https://key:secret@fixture.invalid",
        "https://fixture.invalid/path",
    ],
)
def test_requires_https_endpoint(upload, endpoint):
    _, env, invoke = upload
    result = invoke(overrides={"SEAWEEDFS_S3_ENDPOINT": endpoint})
    assert result.returncode != 0
    assert "must be an HTTPS endpoint" in result.stderr
    assert not Path(env["EXTERNAL_CALLS"]).exists()


@pytest.mark.parametrize("optional", [False, True])
def test_upload_layout_and_configuration(upload, optional):
    source, env, invoke = upload
    images = [
        f"original name.{extension}"
        for extension in ("jpg", "jpeg", "png", "gif", "webp")
    ]
    for name in images + ["not-an-image.txt", "not-an-image.svg", "ignored.JPG"]:
        (source / name).write_bytes(name.encode())
    if optional:
        (source / "output.metadata.json").write_text('{"source": "fixture"}')
        (source / "chunks.ndjson").write_text('{"chunk": 1}\n')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    result = invoke(trace=True)
    assert result.returncode == 0, result.stderr
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    root = Path(env["OBJECTS"]) / "fixture-books/books/book id"
    assert (
        gzip.decompress((root / "raw/output.json.gz").read_bytes())
        == before["output.json"]
    )
    expected = {"raw/output.json.gz", *(f"raw/img/{name}" for name in images)}
    if optional:
        expected |= {"raw/output.metadata.json", "chunks/chunks.ndjson"}
        assert (root / "raw/output.metadata.json").read_bytes() == before[
            "output.metadata.json"
        ]
        assert (root / "chunks/chunks.ndjson").read_bytes() == before["chunks.ndjson"]
    else:
        assert "no chunks.ndjson" in result.stderr
    assert {
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    } == expected
    for name in images:
        assert (root / "raw/img" / name).read_bytes() == before[name]
    calls = [
        json.loads(line) for line in Path(env["RCLONE_CALLS"]).read_text().splitlines()
    ]
    assert len(calls) == (4 if optional else 2)
    for call in calls:
        assert call["credentials_match"]
        assert call["config"] == {
            "TYPE": "s3",
            "PROVIDER": "Cloudflare",
            "ENDPOINT": env["SEAWEEDFS_S3_ENDPOINT"],
            "REGION": "auto",
            "FORCE_PATH_STYLE": "true",
        }
    external = Path(env["EXTERNAL_CALLS"]).read_text().splitlines()
    assert "kubectl" not in external and "nc" not in external


@pytest.mark.parametrize("failed_call", [1, 2, 3, 4])
def test_rclone_failure_propagates_and_cleans_only_own_temp(upload, failed_call):
    source, env, invoke = upload
    (source / "output.metadata.json").write_text("{}")
    (source / "chunks.ndjson").write_text("{}\n")
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    result = invoke(overrides={"FAIL_CALL": str(failed_call)})
    assert result.returncode == 37
    assert "done." not in result.stderr
    assert len(Path(env["RCLONE_CALLS"]).read_text().splitlines()) == failed_call
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
