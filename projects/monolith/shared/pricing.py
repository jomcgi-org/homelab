"""Normalize agent token usage and calculate list-price costs."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from genai_prices import Usage, calc_price

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PricedUsage:
    cost_usd: float
    source: str
    model_ref: str


_MODEL_ALIASES = {
    "luna": ("gpt-5.6-luna", "openai"),
    "terra": ("gpt-5.6-terra", "openai"),
    "sol": ("gpt-5.6-sol", "openai"),
    "opus": ("claude-opus-5", "anthropic"),
    "sonnet": ("claude-sonnet-5", "anthropic"),
    "fable": ("claude-fable-5-1", "anthropic"),
    "spark": ("muse-spark-1.3-contributor", "muse"),
    "pi-spark": ("muse-spark-1.3-contributor", "muse"),
    "qwen": ("muse-spark-1.3-contributor", "muse"),
}

# Contributor-tier figures documented beside the guest model table in
# projects/embervm/runtimes/claude/shim.py:526-528. spark and qwen route to the
# Muse adapter whose turn returns usage={} (shim.py:3424), so only pi-spark
# reaches the table today.
FIXED_PRICES = {
    "muse-spark-1.3-contributor": {
        "input_per_million": 0.10,
        "output_per_million": 0.20,
    },
    "gpt-6-astra": {
        "input_per_million": 10.00,
        "cache_read_per_million": 1.00,
        "output_per_million": 50.00,
        "note": "OpenAI list price, September 2026",
    },
    "codex-auto-review": {
        "input_per_million": 2.50,
        "cache_read_per_million": 0.25,
        "output_per_million": 15.00,
        "note": (
            "estimate: OpenAI publishes no price for the Codex auto reviewer "
            "(openai/codex#20981); aggregator listing used"
        ),
    },
}

_CLAUDE_CACHE_KEYS = frozenset(
    {"cache_read_input_tokens", "cache_creation_input_tokens"}
)
_CODEX_KEYS = frozenset(
    {"cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"}
)
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _token_count(usage: Mapping[str, Any], key: str) -> int | float:
    value = usage.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"invalid token count for {key}")
    return value


def _model_ref(model: str) -> tuple[str, str | None]:
    if model in _MODEL_ALIASES:
        return _MODEL_ALIASES[model]
    if model.startswith("claude-"):
        return model, "anthropic"
    if model in FIXED_PRICES:
        if model.startswith("muse-"):
            return model, "muse"
        return model, "fixed"
    if model.startswith("gpt-"):
        return model, "openai"
    return model, None


def _normalized_usage(
    usage: Mapping[str, Any], provider_id: str | None
) -> tuple[Usage, tuple[int | float, ...]]:
    input_tokens = _token_count(usage, "input_tokens")
    output_tokens = _token_count(usage, "output_tokens")

    collector_shape = usage.get("shape")
    if collector_shape not in {None, "claude", "codex"}:
        raise ValueError("invalid collector usage shape")

    if collector_shape in {"claude", "codex"}:
        cache_read_tokens = _token_count(usage, "cache_read_tokens")
        cache_write_tokens = _token_count(usage, "cache_write_tokens")
        claude_shape = collector_shape == "claude"
    elif _CLAUDE_CACHE_KEYS & usage.keys():
        cache_read_tokens = _token_count(usage, "cache_read_input_tokens")
        cache_write_tokens = _token_count(usage, "cache_creation_input_tokens")
        claude_shape = True
    elif _CODEX_KEYS & usage.keys():
        cache_read_tokens = _token_count(usage, "cached_input_tokens")
        cache_write_tokens = _token_count(usage, "cache_write_input_tokens")
        claude_shape = False
    else:
        cache_read_tokens = _token_count(usage, "cache_read_tokens")
        cache_write_tokens = _token_count(usage, "cache_write_tokens")
        claude_shape = provider_id == "anthropic"

    # genai-prices treats input_tokens as inclusive of cache tokens. Anthropic
    # reports uncached input separately, so Claude-shaped usage must be summed
    # before pricing. OpenAI and Codex already report inclusive input tokens.
    if claude_shape:
        input_tokens += cache_read_tokens + cache_write_tokens

    counts = (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_write_tokens,
    )
    return (
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        counts,
    )


def price_usage(
    model: str | None, usage: Mapping[str, Any] | None
) -> PricedUsage | None:
    """Return the list price for supported usage, or None when it cannot price."""
    if not model or not usage or not isinstance(usage, Mapping):
        return None

    model_ref, provider_id = _model_ref(model)
    try:
        normalized, counts = _normalized_usage(usage, provider_id)
        if not any(counts):
            return None

        if provider_id == "muse":
            prices = FIXED_PRICES[model_ref]
            # Muse prices input inclusively like OpenAI (cache_read_tokens are
            # counted in input_tokens); do not charge separately for cache.
            cost = (
                counts[0] * prices["input_per_million"]
                + counts[1] * prices["output_per_million"]
            ) / 1_000_000
            return PricedUsage(float(cost), "list", model_ref)

        if provider_id == "fixed":
            prices = FIXED_PRICES[model_ref]
            input_tokens, output_tokens, cache_read_tokens, _ = counts
            if cache_read_tokens > input_tokens:
                raise ValueError("cache read tokens exceed input tokens")
            cost = (
                (input_tokens - cache_read_tokens) * prices["input_per_million"]
                + cache_read_tokens * prices["cache_read_per_million"]
                + output_tokens * prices["output_per_million"]
            ) / 1_000_000
            return PricedUsage(float(cost), "list", model_ref)

        try:
            calculation = calc_price(normalized, model_ref, provider_id=provider_id)
        except LookupError:
            retry_ref = _DATE_SUFFIX.sub("", model_ref)
            if retry_ref == model_ref:
                raise
            calculation = calc_price(normalized, retry_ref, provider_id=provider_id)
        return PricedUsage(float(calculation.total_price), "list", calculation.model.id)
    except LookupError:
        logger.debug("No list price for model %r", model, exc_info=True)
        return None
    except (KeyError, TypeError, ValueError, OverflowError):
        logger.debug("Invalid usage for model %r", model, exc_info=True)
        return None
