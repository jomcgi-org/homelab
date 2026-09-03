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
