import json
from collections import Counter
from pathlib import Path
from typing import Any

from bench.verifiers import register, VerifyResult


def _deep_equal(expected: Any, got: Any, *, unordered: bool) -> bool:
    """Recursively compare two JSON-compatible values.

    When unordered=True, lists are compared as multisets (order-insensitive)
    at every level, recursively.
    """
    if type(expected) is not type(got):
        return False
    if isinstance(expected, dict):
        if set(expected.keys()) != set(got.keys()):
            return False
        return all(
            _deep_equal(expected[k], got[k], unordered=unordered) for k in expected
        )
    if isinstance(expected, list) and unordered:
        if len(expected) != len(got):
            return False
        # For lists of hashable scalars, use Counter. For complex elements, use
        # a simple O(n^2) matching (task pack lists are small).
        try:
            return Counter(expected) == Counter(got)
        except TypeError:
            # Unhashable elements: match each expected item to a got item.
            remaining = list(got)
            for item in expected:
                for i, candidate in enumerate(remaining):
                    if _deep_equal(item, candidate, unordered=unordered):
                        remaining.pop(i)
                        break
                else:
                    return False
            return True
    return expected == got


@register("json-match")
def verify(workdir: Path, args: dict) -> VerifyResult:
    """Load a JSON file and deep-compare it to args['expect'].

    args:
        file: path relative to workdir
        expect: the expected JSON-compatible value
        unordered: (optional bool) if True, compare lists order-insensitively
    """
    file_path = workdir / args["file"]
    with open(file_path) as f:
        got = json.load(f)

    expect = args["expect"]
    unordered = args.get("unordered", False)

    if _deep_equal(expect, got, unordered=unordered):
        return VerifyResult(True, "")

    return VerifyResult(
        False,
        f"json-match failed.\nexpected: {json.dumps(expect, indent=2)}\ngot:      {json.dumps(got, indent=2)}",
    )
