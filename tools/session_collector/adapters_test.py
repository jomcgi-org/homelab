import json
from pathlib import Path

from tools.session_collector import claude_v1, codex_v1
from tools.session_collector.models import Block, Session, Turn
from tools.session_collector.render import render

FIXTURES = Path(__file__).parent / "fixtures"


def test_claude_fixture_turns_and_drops_records():
    session = claude_v1.parse(FIXTURES / "claude.jsonl")
    output = render(session, "jomcgi-org/homelab", "repo:jomcgi-org/homelab")
    assert session.title == "Sanitized fixture"
    assert "## Turn 1" in output.markdown
    assert "## Turn 2" in output.markdown
    assert "`tool: Bash` input" in output.markdown
    assert "`result:`" in output.markdown
    assert "private chain of thought" not in output.markdown
    assert "must disappear" not in output.markdown
    assert "DROP_" not in output.markdown
    assert output.markdown.count("## Turn ") == 2
    assert "<command-name>" not in output.markdown
    assert "<local-command-stdout>" not in output.markdown
    assert "[Request interrupted" not in output.markdown


def test_claude_command_boilerplate_is_not_a_title_or_turn(tmp_path):
    path = tmp_path / "commands.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"type":"user","message":{"content":"<command-name>/compact</command-name>"}}',
                '{"type":"user","message":{"content":"<local-command-stdout>done</local-command-stdout>"}}',
                '{"type":"user","message":{"content":"[Request interrupted by user]"}}',
                '{"type":"user","message":{"content":"Real request"}}',
            ]
        )
        + "\n"
    )
    session = claude_v1.parse(path)
    assert session.title == "Real request"
    assert len(session.turns) == 1
    assert session.turns[0].blocks == [Block("user", "Real request")]


def test_codex_fixture_turns_and_drops_records():
    session = codex_v1.parse(FIXTURES / "codex.jsonl")
    output = render(session, "jomcgi-org/homelab", "repo:jomcgi-org/homelab")
    assert session.session_id == "codex-fixture"
    assert "## Turn 1" in output.markdown
    assert "`tool: exec_command` input" in output.markdown
    assert "`result:`" in output.markdown
    assert "base_instructions" not in output.markdown
    assert "must disappear" not in output.markdown
    assert "DROP_" not in output.markdown


def test_codex_uses_transcript_git_and_event_title(tmp_path):
    planted = "injected-title-must-not-appear"
    path = tmp_path / "codex.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "cwd": "/deleted/worktree",
                "git": {
                    "repository_url": "git@github.com:owner/transcript.git",
                    "branch": "feat/transcript",
                },
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"<recommended_plugins>{planted}</recommended_plugins>",
                    }
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Actual request"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Visible request"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = codex_v1.parse(path)
    output = render(session, "owner/transcript", "repo:owner/transcript")
    assert session.git_branch == "feat/transcript"
    assert session.git_origin == "git@github.com:owner/transcript.git"
    assert session.title == "Actual request"
    assert planted not in output.markdown


def test_codex_whitespace_user_text_does_not_crash_or_leak(tmp_path):
    planted = "whitespace-fallback-secret"
    path = tmp_path / "whitespace.jsonl"
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "   \n  "}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "token_count", "secret": planted},
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    output = render(codex_v1.parse(path), None, "repo:unknown")
    assert planted not in output.markdown


def test_titles_are_redacted_before_truncation_in_both_adapters(tmp_path):
    planted = "ghp_abcdefghijklmnopqrstuvwxyz"
    title = "x" * 75 + planted
    claude_path = tmp_path / "claude-title.jsonl"
    claude_path.write_text(
        json.dumps({"type": "user", "message": {"content": title}}) + "\n"
    )
    codex_path = tmp_path / "codex-title.jsonl"
    codex_path.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": title},
            }
        )
        + "\n"
    )
    for session in (claude_v1.parse(claude_path), codex_v1.parse(codex_path)):
        output = render(session, None, "repo:unknown")
        assert planted not in output.markdown
        assert "ghp_a" not in output.markdown


def test_claude_task_notification_and_bash_stdout_attach_as_results(tmp_path):
    planted = "bashstdoutsecretvalue"
    path = tmp_path / "notifications.jsonl"
    records = [
        {"type": "user", "message": {"content": "Real request"}},
        {
            "type": "user",
            "message": {
                "content": "<task-notification>background work finished</task-notification>"
            },
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "<bash-stdout>PATH=/bin\nHOME=/tmp\nSHELL=/bin/zsh\n"
                    f"SECRET={planted}</bash-stdout>"
                )
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    session = claude_v1.parse(path)
    output = render(session, None, "repo:unknown")
    assert len(session.turns) == 1
    assert "[task notification]" in output.markdown
    assert planted not in output.markdown
    assert "[REDACTED:env_dump]" in output.markdown


def test_sensitive_command_replaces_following_result_value():
    planted = "c2Vuc2l0aXZlLWt1YmUtZGF0YQ=="
    commands = (
        "kubectl get secret -o yaml",
        "kubectl get secrets/example -o json",
        "cat ~/.kube/config",
        "cat ~/.netrc",
        "cat ~/.npmrc",
        "cat ~/.pypirc",
        "cat ~/.pgpass",
        "cat ~/.cloudflared/cert.pem",
        "cat ~/.ssh/id_rsa",
        "cat ~/.ssh/id_ed25519",
        "cat .env",
        "op read op://vault/item/password",
        "op item get example",
        "base64 -d token.txt",
        "security find-generic-password -s example -w",
        "gh auth token",
        "aws configure get aws_secret_access_key",
        "gcloud auth print-access-token",
        "vault read secret/example",
        "cloudflared access token -app example.test",
    )
    for command in commands:
        session = Session(
            "claude",
            "id",
            "/tmp",
            None,
            None,
            "start",
            "end",
            "title",
            2,
            2,
            "test-v1",
            [
                Turn(
                    [
                        Block("tool", json.dumps({"command": command}), "Bash"),
                        Block("result", f"data:\n  planted: {planted}"),
                    ]
                )
            ],
        )
        output = render(session, None, "repo:unknown")
        assert planted not in output.markdown
        assert "[REDACTED:sensitive_output]" in output.markdown


def test_fragment_caps_apply():
    session = Session(
        "claude",
        "id",
        "/tmp/homelab",
        None,
        None,
        "start",
        "end",
        "title",
        1,
        1,
        "test-v1",
        [
            Turn(
                [
                    Block("user", "u" * 9000),
                    Block("assistant", "a" * 9000),
                    Block("tool", "i" * 2000, "Bash"),
                    Block("result", "r" * 3000),
                ]
            )
        ],
    )
    output = render(session, "jomcgi-org/homelab", "repo:jomcgi-org/homelab")
    assert output.markdown.count("[... capped ...]") == 4
    assert output.truncated is True


def test_middle_elision_keeps_first_three_and_last_five():
    turns = [
        Turn([Block("user", f"marker-{index}")] + [Block("assistant", "x" * 9000)] * 6)
        for index in range(12)
    ]
    session = Session(
        "codex",
        "id",
        "/tmp/homelab",
        None,
        None,
        "start",
        "end",
        "title",
        12,
        12,
        "test-v1",
        turns,
    )
    output = render(session, "jomcgi-org/homelab", "repo:jomcgi-org/homelab")
    for index in (0, 1, 2, 7, 8, 9, 10, 11):
        assert f"marker-{index}" in output.markdown
    for index in (3, 4, 5, 6):
        assert f"marker-{index}" not in output.markdown
    assert "[... elided 4 turns ...]" in output.markdown
    assert len(output.markdown.encode()) <= 400 * 1024


def test_document_cap_applies_when_eight_kept_turns_are_oversized():
    turns = [
        Turn([Block("user", f"marker-{index}")] + [Block("assistant", "x" * 9000)] * 8)
        for index in range(8)
    ]
    session = Session(
        "codex",
        "id",
        "/tmp/homelab",
        None,
        None,
        "start",
        "end",
        "title",
        8,
        8,
        "test-v1",
        turns,
    )
    output = render(session, "jomcgi-org/homelab", "repo:jomcgi-org/homelab")
    assert all(f"marker-{index}" in output.markdown for index in range(8))
    assert len(output.markdown.encode()) <= 400 * 1024
    assert output.truncated is True
