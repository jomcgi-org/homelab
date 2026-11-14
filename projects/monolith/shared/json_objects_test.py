"""Schema-independent selection and decoder failure contracts."""

import json
import sys

import pytest

from shared.json_objects import select_json_object


@pytest.mark.parametrize("fallback", ["forward", "reverse"])
@pytest.mark.parametrize(
    "text, expected",
    [
        ('prefix {"a":{"b":1}} suffix', {"a": {"b": 1}}),
        (
            json.dumps({"a": 'quoted " and escaped \\ { }'}),
            {"a": 'quoted " and escaped \\ { }'},
        ),
        ('{"a":1} {broken} {"a":2} {invalid}', {"a": 2}),
        ('{"a":1} {}', {}),
        ('[{"a":1}, {"a":2}]', {"a": 2}),
        ('{"a":1} {"truncated":', {"a": 1}),
        ('{"truncated":{"a":1}', None),
        ("no object", None),
        ('"unmatched quote {"a":1}', None),
        ('{"a":1} "unmatched {"a":2}', {"a": 1}),
    ],
)
def test_balanced_candidates(text, expected, fallback):
    assert select_json_object(text, [], fallback=fallback) == expected


@pytest.mark.parametrize("fallback", ["forward", "reverse"])
@pytest.mark.parametrize(
    "blocks, expected",
    [
        (['{"a":1}', '{"a":2}'], {"a": 2}),
        (['{"a":1}', "invalid", '[{"a":3}]'], {"a": 1}),
        (["{}", "invalid"], {}),
        (["invalid", '[{"a":3}]'], {"fallback": True}),
    ],
)
def test_fences_precede_fallback(blocks, expected, fallback):
    assert (
        select_json_object('{"fallback":true}', iter(blocks), fallback=fallback)
        == expected
    )


def test_integer_limit_preserves_fallback_decoding_order():
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        text = '{"a":' + "1" * 641 + '} {"a":2}'
        with pytest.raises(ValueError, match="Exceeds the limit"):
            select_json_object(text, [], fallback="forward")
        assert select_json_object(text, [], fallback="reverse") == {"a": 2}
        for fallback in ("forward", "reverse"):
            with pytest.raises(ValueError, match="Exceeds the limit"):
                select_json_object(text, ["{}", text.split(" ")[0]], fallback=fallback)
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize("fallback", ["forward", "reverse"])
def test_other_decoder_exceptions_propagate(monkeypatch, fallback):
    def fail(_):
        raise RuntimeError("synthetic decoder failure")

    monkeypatch.setattr(json, "loads", fail)
    for blocks in ([], ["{}"]):
        with pytest.raises(RuntimeError, match="^synthetic decoder failure$"):
            select_json_object("{}", blocks, fallback=fallback)
