import os
from pathlib import Path

import httpx

from tools.session_collector import claude_v1, codex_v1
from tools.session_collector.collector import run_collection
from tools.session_collector.redact import PATTERNS
from tools.session_collector.render import render

FIXTURES = Path(__file__).parent / "fixtures"

PLANTED = (
    "AKIA1234567890ABCDEF",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ghp_abcdefghijklmnopqrstuvwxyz",
    "sk-openai_key_abcdefghijklmnopqrstu",
    "sk-ant-abcdefghijklmnopqrstuvwxyz",
    "xoxb-1234567890-abcdefghij",
    "eyJabcdefghijkl.abcdefghijkl.abcdefghijkl",
    "ZmFrZS1wcml2YXRlLWtleQ==",
    "-----BEGIN PRIVATE KEY-----",
    "-----END PRIVATE KEY-----",
    "Bearer abcdefghijklmnopqrstuvwxyz",
    "user:password123",
    "password123",
    "supersecretvalue",
    "CF_Authorization=abcdefghijklmnopqrstuvwxyz123456",
    "ops_abcdefghijklmnopqrstuvwxyz",
    "export BUILDBUDDY_API_KEY=abc123def456ghi789",
    "PGPASSWORD=hunter2secretvalue psql",
    "CLIENT_SECRET=0123456789abcdefghij",
    "MYSQL_ROOT_PASSWORD=letmeinplease123",
    "AWS_SESSION_TOKEN=FwoGZXIvYXdzEP//////////wEaDL0123456789abcdef",
    '{"token": "s3cr3tvaluehere123"}',
    "Authorization: token abcdef0123456789abcdef",
    "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
    "--token commandlinesecret",
    "password netrcsecretvalue",
    "tskey-abcdefghijklmnop",
)

PLANTED_VALUES = (
    "abc123def456ghi789",
    "hunter2secretvalue",
    "0123456789abcdefghij",
    "letmeinplease123",
    "FwoGZXIvYXdzEP//////////wEaDL0123456789abcdef",
    "s3cr3tvaluehere123",
    "abcdef0123456789abcdef",
    "dXNlcjpwYXNzd29yZDEyMw==",
    "commandlinesecret",
    "netrcsecretvalue",
    "tskey-abcdefghijklmnop",
)


def _session_with_text(text: str):
    from tools.session_collector.models import Block, Session, Turn

    return Session(
        "test",
        "id",
        "/tmp",
        None,
        None,
        "start",
        "end",
        "title",
        1,
        1,
        "test-v1",
        [Turn([Block("assistant", text)])],
    )


def test_json_escaped_kv_secret_is_removed_end_to_end(tmp_path):
    import json

    planted = "hunter2escaped"
    path = tmp_path / "escaped.jsonl"
    records = [
        {"type": "user", "message": {"content": "Run it"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {
                            "command": f'curl -d \'{{"password": "{planted}"}}\' example.test'
                        },
                    }
                ]
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    output = render(claude_v1.parse(path), None, "repo:unknown")
    assert planted not in output.markdown


def test_common_secret_shapes_are_absent_from_rendered_output():
    planted = (
        "basicheaderc2VjcmV0MTIzNDU2Nzg5MA==",
        "curluserpassword",
        "spaceflagpassword",
        "spaceflagtokensecret",
        "emptyurlpassword",
        "netrcpasswordvalue",
        "tskey-abcdefghijklmnop",
        "AIza12345678901234567890123456789012345",
        "npm_123456789012345678901234567890123456",
        "glpat-12345678901234567890",
        "discordwebhooksecret",
        "two words planted",
    )
    text = "\n".join(
        (
            f"Authorization: Basic {planted[0]}",
            f"curl -u user:{planted[1]} https://example.test",
            f"tool --password {planted[2]}",
            f"tool --token {planted[3]}",
            f"redis://:{planted[4]}@host/0",
            f"machine example.test login user password {planted[5]}",
            planted[6],
            planted[7],
            planted[8],
            planted[9],
            f"https://discordapp.com/api/webhooks/123/{planted[10]}",
            f'password = "{planted[11]}"',
        )
    )
    output = render(_session_with_text(text), None, "repo:unknown")
    for value in planted:
        assert value not in output.markdown


def test_every_pattern_is_replaced_and_counted_in_each_fixture():
    outputs = [
        render(
            claude_v1.parse(FIXTURES / "claude.jsonl"),
            "jomcgi-org/homelab",
            "repo:jomcgi-org/homelab",
        ),
        render(
            codex_v1.parse(FIXTURES / "codex.jsonl"),
            "jomcgi-org/homelab",
            "repo:jomcgi-org/homelab",
        ),
    ]
    for output in outputs:
        for name, _ in PATTERNS:
            assert output.redactions[name] >= 1
            assert f"[REDACTED:{name}]" in output.markdown
        assert output.redactions["env_dump"] >= 1
        for secret in PLANTED + PLANTED_VALUES:
            assert secret not in output.markdown


def test_env_dump_tool_result_is_replaced():
    output = render(
        codex_v1.parse(FIXTURES / "codex.jsonl"),
        "jomcgi-org/homelab",
        "repo:jomcgi-org/homelab",
    )
    assert "[REDACTED:env_dump]" in output.markdown
    assert "SECRET_VALUE" not in output.markdown
    assert output.redactions["env_dump"] == 1


def test_planted_values_never_reach_logs_or_state(tmp_path, capsys, monkeypatch):
    claude_dir = tmp_path / "claude"
    claude_project = claude_dir / "project"
    claude_project.mkdir(parents=True)
    claude_path = claude_project / "claude.jsonl"
    claude_path.write_bytes((FIXTURES / "claude.jsonl").read_bytes())
    codex_dir = tmp_path / "codex"
    codex_session = codex_dir / "2026" / "01" / "02"
    codex_session.mkdir(parents=True)
    codex_path = codex_session / "codex.jsonl"
    codex_path.write_bytes((FIXTURES / "codex.jsonl").read_bytes())
    for path in (claude_path, codex_path):
        os.utime(path, (100, 100))

    payloads = []

    def transport(request):
        payloads.append(request.content.decode())
        return httpx.Response(
            201, json={"raw_id": f"raw-{len(payloads)}", "created": True}
        )

    monkeypatch.setattr("tools.session_collector.collector.MIN_BODY_BYTES", 0)
    state_file = tmp_path / "state.json"
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        assert (
            run_collection(
                claude_dir=claude_dir,
                codex_dir=codex_dir,
                state_file=state_file,
                allowlist={"jomcgi-org/homelab": "repo:jomcgi-org/homelab"},
                path_allowlist={Path("/tmp/homelab"): "jomcgi-org/homelab"},
                quiet_minutes=0,
                client=client,
                token_reader=lambda hostname: "cached-token",
                now=1000,
            )
            == 0
        )

    diagnostics = capsys.readouterr()
    persisted = state_file.read_text()
    uploaded = "\n".join(payloads)
    for secret in PLANTED + PLANTED_VALUES:
        assert secret not in diagnostics.err
        assert secret not in persisted
        assert secret not in uploaded
