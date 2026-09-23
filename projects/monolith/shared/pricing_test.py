import pytest
from genai_prices import Usage, calc_price

from shared.pricing import price_usage


def test_luna_input_price():
    priced = price_usage("luna", {"input_tokens": 1_000_000, "output_tokens": 0})

    assert priced is not None
    assert priced.cost_usd == pytest.approx(0.10)
    assert priced.source == "list"
    assert priced.model_ref == "gpt-6-luna"


def test_luna_cached_input_uses_inclusive_input_semantics():
    priced = price_usage(
        "luna", {"input_tokens": 1_000_000, "cache_read_tokens": 500_000}
    )

    assert priced is not None
    assert priced.cost_usd == pytest.approx(0.055)


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
    assert priced.cost_usd == pytest.approx(0.055)


@pytest.mark.parametrize(
    ("model", "model_ref"), [("sol", "gpt-6-sol"), ("terra", "gpt-5.6-terra")]
)
def test_codex_aliases_resolve_to_provider_models(model, model_ref):
    priced = price_usage(model, {"input_tokens": 1_000})

    assert priced is not None
    assert priced.cost_usd > 0
    assert priced.model_ref == model_ref


def test_opus_adds_exclusive_claude_cache_tokens_to_input():
    blended = price_usage(
        "opus",
        {"input_tokens": 1_000, "cache_read_tokens": 9_000, "output_tokens": 100},
    )
    assert blended is not None
    assert blended.model_ref == "claude-opus-5-5"
    # 1,000 uncached input at $4, 9,000 cache reads at $0.20, 100 output at $20.
    assert blended.cost_usd == pytest.approx(0.0078)


def test_opus_5_5_prices_cache_writes_at_the_write_rate():
    priced = price_usage(
        "claude-opus-5-5",
        {
            "shape": "claude",
            "input_tokens": 1_000,
            "cache_write_tokens": 2_000,
            "output_tokens": 0,
        },
    )

    assert priced is not None
    assert priced.model_ref == "claude-opus-5-5"
    assert priced.cost_usd == pytest.approx(0.014)


def test_opus_5_5_model_id_is_not_priced_as_opus_5():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}

    assert price_usage("claude-opus-5-5", usage).cost_usd == pytest.approx(4.0)
    assert price_usage("claude-opus-5", usage).cost_usd == pytest.approx(5.0)


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


@pytest.mark.parametrize("model", ["spark", "qwen"])
def test_muse_adapter_usage_does_not_double_count_cache_into_input(model):
    # Regression for the Muse adapter (MuseProcess.turn / _muse_usage_projection
    # in shim.py) forwarding generic {input_tokens, cache_read_tokens,
    # cache_write_tokens} usage rather than Claude-shaped keys. If it emitted
    # cache_read_input_tokens / cache_creation_input_tokens instead, this would
    # get classified claude_shape and fold cache into input, inflating cost.
    usage = {
        "muse": {"status": "complete", "source": "msp_retained_session_view"},
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cached_tokens": 500_000,
        "cache_read_tokens": 500_000,
        "cache_write_tokens": 0,
        "reasoning_tokens": 10,
        "prompt_tokens": 1_000_000,
        "total_tokens": 1_100_000,
    }
    priced = price_usage(model, usage)

    assert priced is not None
    assert priced.model_ref == "muse-spark-1.3-contributor"
    # input_per_million=0.10, output_per_million=0.20; cache reads are already
    # included in input_tokens and must not be charged again.
    assert priced.cost_usd == pytest.approx(
        (1_000_000 * 0.10 + 100_000 * 0.20) / 1_000_000
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gpt-6-astra", 12.75), ("codex-auto-review", 3.4375)],
)
def test_fixed_openai_prices_use_cache_inclusive_arithmetic(model, expected):
    priced = price_usage(
        model,
        {
            "input_tokens": 1_000_000,
            "cache_read_tokens": 250_000,
            "output_tokens": 100_000,
        },
    )

    assert priced is not None
    assert priced.cost_usd == expected
    assert priced.source == "list"
    assert priced.model_ref == model


@pytest.mark.parametrize(
    ("model", "expected"),
    [("gpt-6-astra", 12.75), ("codex-auto-review", 3.4375)],
)
@pytest.mark.parametrize(
    ("shape", "input_tokens"), [("claude", 750_000), ("codex", 1_000_000)]
)
def test_fixed_openai_prices_accept_both_collector_shapes(
    model, expected, shape, input_tokens
):
    priced = price_usage(
        model,
        {
            "shape": shape,
            "input_tokens": input_tokens,
            "cache_read_tokens": 250_000,
            "output_tokens": 100_000,
        },
    )

    assert priced is not None
    assert priced.cost_usd == expected


@pytest.mark.parametrize("model", ["gpt-6-astra", "codex-auto-review"])
def test_fixed_openai_prices_reject_cache_reads_above_input(model):
    assert (
        price_usage(
            model,
            {
                "shape": "codex",
                "input_tokens": 999,
                "cache_read_tokens": 1_000,
            },
        )
        is None
    )


def test_unknown_model_returns_none():
    assert price_usage("gpt-unknown", {"input_tokens": 1_000}) is None


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


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [("gpt-5.6-sol", 0.002564), ("sol", 0.001282)],
)
def test_reasoning_key_keeps_shim_cache_read_discount(model, expected_cost):
    # reasoning_output_tokens selects the Codex branch; the cache still sits
    # under the shim key, and dropping it prices gpt-5.6-sol at $0.0062.
    shim_usage = {
        "input_tokens": 1_100,
        "cache_read_tokens": 1_010,
        "output_tokens": 90,
    }

    priced = price_usage(model, {**shim_usage, "reasoning_output_tokens": 30})

    assert priced is not None
    assert priced.cost_usd == pytest.approx(expected_cost)
    assert priced == price_usage(model, shim_usage)


def test_claude_transcript_shape_uses_exclusive_input_semantics():
    shaped = price_usage(
        "claude-opus-5-5",
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
    assert claude.model_ref == "claude-opus-5"
    assert codex.model_ref == "claude-opus-5"
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
    assert claude.cost_usd == pytest.approx(expected)
    assert codex.cost_usd == pytest.approx(expected)


def test_dated_claude_model_retries_without_date_suffix():
    priced = price_usage(
        "claude-haiku-4-5-20251001", {"input_tokens": 1_000, "output_tokens": 100}
    )

    assert priced is not None
    assert priced.model_ref == "claude-haiku-4-5"


def test_astra_alias_prices_through_the_fixed_openai_table():
    priced = price_usage(
        "astra",
        {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 250_000,
            "output_tokens": 100_000,
        },
    )

    assert priced is not None
    assert priced.cost_usd == 12.75
    assert priced.source == "list"
    assert priced.model_ref == "gpt-6-astra"


def test_astra_and_gpt_6_astra_agree_on_the_same_usage():
    usage = {"input_tokens": 12_000, "output_tokens": 3_000}

    assert price_usage("astra", usage) == price_usage("gpt-6-astra", usage)
