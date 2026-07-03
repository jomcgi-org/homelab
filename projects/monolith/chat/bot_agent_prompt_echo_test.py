"""Tests for the /agent prompt echo (_format_agent_prompt_echo in chat.bot).

The prompt otherwise survives only in the ~90-char thread title, so the echo
posts it in full. Covers attribution, code-fence escaping (the prompt is
user-controlled and must not break out of the block), and the 2000-char cap.
"""

from types import SimpleNamespace

from chat.bot import _PROMPT_ECHO_MAX, _format_agent_prompt_echo

_USER = SimpleNamespace(mention="<@123>")


def test_attributes_and_fences_the_prompt():
    out = _format_agent_prompt_echo(_USER, "add a health check endpoint")
    assert out.startswith("Prompt from <@123>:\n")
    assert "```\nadd a health check endpoint\n```" in out


def test_neutralizes_inner_code_fence():
    # A triple-backtick in the prompt must not close the echo's own fence. After
    # escaping, the only raw ``` left are the echo's own opening and closing pair.
    out = _format_agent_prompt_echo(_USER, "```rm -rf /```")
    assert out.count("```") == 2
    zwsp = chr(0x200B)
    assert f"`{zwsp}`{zwsp}`" in out  # backticks woven with a zero-width space


def test_caps_to_one_discord_message():
    out = _format_agent_prompt_echo(_USER, "x" * 5000)
    assert len(out) <= _PROMPT_ECHO_MAX
    assert out.endswith("```")
    assert "…" in out  # truncation marker


def test_short_prompt_not_truncated():
    out = _format_agent_prompt_echo(_USER, "tiny")
    assert "…" not in out
    assert out.count("tiny") == 1
