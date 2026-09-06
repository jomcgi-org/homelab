from knowledge.tools.prettier_json import dumps_prettier_json


def test_short_scalar_array_collapses_onto_one_line():
    assert dumps_prettier_json({"tags": ["inference", "moe"]}) == (
        '{\n  "tags": ["inference", "moe"]\n}'
    )


def test_long_scalar_array_breaks_one_element_per_line():
    # The array text is 72 columns, but the key and indentation make the full
    # line 82 columns, so Prettier breaks it.
    value = {"tags": ["a" * 32, "b" * 32]}

    assert dumps_prettier_json(value) == (
        f'{{\n  "tags": [\n    "{"a" * 32}",\n    "{"b" * 32}"\n  ]\n}}'
    )
