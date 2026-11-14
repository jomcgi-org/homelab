import pytest

from core.lazy import lazy_exports


def test_lazy_exports_resolves_mapped_names_and_rejects_others():
    getter = lazy_exports("fake.api", {"dumps": "json"})
    import json

    assert getter("dumps") is json.dumps
    with pytest.raises(AttributeError) as excinfo:
        getter("missing")
    assert str(excinfo.value) == "module 'fake.api' has no attribute 'missing'"
