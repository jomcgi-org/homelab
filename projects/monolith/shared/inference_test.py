from __future__ import annotations

import pytest

from shared.inference import (
    REASONING_EFFORTS,
    reasoning_effort,
    structured_output,
    thinking_off,
)


def test_thinking_off():
    assert thinking_off() == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("effort", sorted(REASONING_EFFORTS))
def test_reasoning_effort_accepts_legal_values(effort: str):
    assert reasoning_effort(effort) == {
        "chat_template_kwargs": {"reasoning_effort": effort}
    }


@pytest.mark.parametrize("effort", ["none", "off", "invalid"])
def test_reasoning_effort_rejects_illegal_values(effort: str):
    with pytest.raises(ValueError) as exc_info:
        reasoning_effort(effort)
    for legal in REASONING_EFFORTS:
        assert legal in str(exc_info.value)


def test_structured_output_carries_both_dialects_and_same_schema():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    result = structured_output(schema, name="answer")

    assert result["guided_json"] is schema
    assert result["response_format"]["json_schema"]["schema"] is schema
    assert result["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": schema,
        },
    }
