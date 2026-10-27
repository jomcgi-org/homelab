import math

import pytest

from core.jsonio import dumps, loads_or


def test_loads_or_returns_default_for_empty_or_invalid_text():
    assert loads_or(None, {}) == {}
    assert loads_or("", []) == []
    assert loads_or("{not json", 0) == 0
    assert loads_or('{"a": 1}', {}) == {"a": 1}


def test_dumps_is_compact_sorted_and_rejects_nan():
    assert dumps({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    with pytest.raises(ValueError):
        dumps(math.nan)
