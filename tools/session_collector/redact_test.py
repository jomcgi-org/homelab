from pathlib import Path

from tools.session_collector import claude_v1, codex_v1
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
)


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
        for secret in PLANTED:
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
