import pytest
from genai_prices import Usage, calc_price

from shared.pricing import price_usage


def test_luna_input_price():
    priced = price_usage("luna", {"input_tokens": 1_000_000, "output_tokens": 0})

    assert priced is not None
    assert priced.cost_usd == pytest.approx(0.40)
    assert priced.source == "list"
    assert priced.model_ref == "gpt-5.6-luna"


def test_luna_cached_input_uses_inclusive_input_semantics():
    priced = price_usage(
        "luna", {"input_tokens": 1_000_000, "cache_read_tokens": 500_000}
    )

    assert priced is not None
    assert priced.cost_usd == pytest.approx(0.22)


def test_codex_transcript_cache_is_part_of_reported_input():
    priced = price_usage(
        "luna",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cached_input_tokens": 500_000,
            "reasoning_output_tokens": 100_000,
        },
    )

    assert priced is not None
    assert priced.cost_usd == pytest.approx(0.22)


@pytest.mark.parametrize("model", ["sol", "terra"])
def test_codex_aliases_resolve_to_provider_models(model):
    priced = price_usage(model, {"input_tokens": 1_000})

    assert priced is not None
    assert priced.cost_usd > 0
    assert priced.model_ref == f"gpt-5.6-{model}"


def test_opus_adds_exclusive_claude_cache_tokens_to_input():
    blended = price_usage(
        "opus",
        {"input_tokens": 1_000, "cache_read_tokens": 9_000, "output_tokens": 100},
    )
    assert blended is not None
    assert blended.model_ref == "claude-opus-5"
    model_price = calc_price(
        Usage(
            input_tokens=10_000,
            cache_read_tokens=9_000,
            output_tokens=100,
        ),
        "claude-opus-5",
        provider_id="anthropic",
    ).model_price
    expected = float(
        (
            1_000 * model_price.input_mtok
            + 9_000 * model_price.cache_read_mtok
            + 100 * model_price.output_mtok
        )
        / 1_000_000
    )
    assert blended.cost_usd == pytest.approx(expected)


@pytest.mark.parametrize("model", ["spark", "qwen", "pi-spark"])
def test_muse_contributor_price(model):
    priced = price_usage(model, {"input_tokens": 1_000_000, "output_tokens": 1_000_000})

    assert priced is not None
    assert priced.cost_usd == pytest.approx(0.30)
    assert priced.model_ref == "muse-spark-1.3-contributor"


def test_muse_cache_reads_add_no_separate_cost():
    with_cache = price_usage(
        "pi-spark",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cache_read_tokens": 500_000,
        },
    )
    without_cache = price_usage(
        "pi-spark", {"input_tokens": 1_000_000, "output_tokens": 100_000}
    )

    assert with_cache is not None
    assert with_cache == without_cache


def test_unknown_model_returns_none():
    assert price_usage("gpt-6-astra", {"input_tokens": 1_000}) is None


def test_empty_usage_returns_none():
    assert price_usage("luna", {}) is None


def test_none_model_returns_none():
    assert price_usage(None, {"input_tokens": 1_000}) is None


def test_reasoning_output_tokens_are_not_added_to_output():
    with_reasoning = price_usage(
        "luna",
        {
            "input_tokens": 1_000,
            "output_tokens": 500,
            "reasoning_output_tokens": 400,
        },
    )
    without_reasoning = price_usage(
        "luna", {"input_tokens": 1_000, "output_tokens": 500}
    )

    assert with_reasoning == without_reasoning


def test_claude_transcript_shape_uses_exclusive_input_semantics():
    shaped = price_usage(
        "claude-opus-5",
        {
            "input_tokens": 1_000,
            "output_tokens": 100,
            "cache_read_input_tokens": 9_000,
            "cache_creation_input_tokens": 0,
        },
    )
    canonical = price_usage(
        "opus",
        {"input_tokens": 1_000, "output_tokens": 100, "cache_read_tokens": 9_000},
    )

    assert shaped == canonical


def test_collector_shapes_apply_provider_cache_semantics():
    claude = price_usage(
        "claude-opus-5",
        {
            "shape": "claude",
            "input_tokens": 1_000,
            "output_tokens": 100,
            "cache_read_tokens": 9_000,
            "cache_write_tokens": 0,
        },
    )
    codex = price_usage(
        "claude-opus-5",
        {
            "shape": "codex",
            "input_tokens": 10_000,
            "output_tokens": 100,
            "cache_read_tokens": 9_000,
            "cache_write_tokens": 0,
        },
    )

    assert claude is not None
    assert codex is not None
    assert claude.cost_usd == pytest.approx(codex.cost_usd)


def test_dated_claude_model_retries_without_date_suffix():
    priced = price_usage(
        "claude-haiku-4-5-20251001", {"input_tokens": 1_000, "output_tokens": 100}
    )

    assert priced is not None
    assert priced.model_ref == "claude-haiku-4-5"
