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
# projects/embervm/runtimes/claude/shim.py:527-529.
MUSE_PRICES = {
    "muse-spark-1.3-contributor": {
        "input_per_million": 0.10,
        "output_per_million": 0.20,
        "cache_read_per_million": 0.10,
    }
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
    if model.startswith("gpt-"):
        return model, "openai"
    if model in MUSE_PRICES:
        return model, "muse"
    return model, None


def _normalized_usage(
    usage: Mapping[str, Any], provider_id: str | None
) -> tuple[Usage, tuple[int | float, ...]]:
    input_tokens = _token_count(usage, "input_tokens")
    output_tokens = _token_count(usage, "output_tokens")

    if _CLAUDE_CACHE_KEYS & usage.keys():
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
            prices = MUSE_PRICES[model_ref]
            cost = (
                counts[0] * prices["input_per_million"]
                + counts[2] * prices["cache_read_per_million"]
                + counts[1] * prices["output_per_million"]
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
