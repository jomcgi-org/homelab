from core.jsonio import loads_or


def test_loads_or_returns_default_for_empty_or_invalid_text():
    assert loads_or(None, {}) == {}
    assert loads_or("", []) == []
    assert loads_or("{not json", 0) == 0
    assert loads_or('{"a": 1}', {}) == {"a": 1}
