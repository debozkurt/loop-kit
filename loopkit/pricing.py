"""Per-adapter cost accounting — what makes the budget stop actually bite (Chapter 14).

The loop's budget ceiling (`stops.BudgetCeiling`) is only as real as the dollar figure the
agent reports each tick. A `MockAgent` can fake a number; a real adapter must turn the provider's
native token *usage* into a `cost_usd`. That conversion lives here, in one place, for two reasons:

1. **It's reference data, not logic.** Model prices change between releases; keeping the table in a
   single module means a price update is a one-line edit, not a hunt through adapter code.
2. **It's cross-provider.** Claude and OpenAI both bill input/output/cached tokens, but at
   different rates and with different cache economics. A normalized `Usage` + a per-model
   `ModelPrice` lets every API adapter compute cost the same way.

Unknown model → cost `0.0` (not an error). A zero cost means the budget stop can't fire, which is
exactly what `loopkit doctor` warns about: better a loud "I can't price this" than a silent wrong
number. Keep this module stdlib-only — pricing must work without any provider SDK installed.
"""
from __future__ import annotations

from dataclasses import dataclass

_PER_MTOK = 1_000_000  # table is written in $/million-tokens for readability; stored per-token


@dataclass
class Usage:
    """Normalized token usage for one or more model calls, summed across a tick.

    The four buckets are the union of what Claude and OpenAI report. `cache_read`/`cache_write`
    default to 0, so a provider that doesn't surface caching just leaves them empty and the cost
    collapses to the plain input/output terms.
    """

    input_tokens: int = 0          # uncached prompt tokens, full price
    output_tokens: int = 0         # generated tokens
    cache_read_tokens: int = 0     # served from cache (~0.1x input on Claude)
    cache_write_tokens: int = 0    # written to cache (~1.25x input on Claude, 5-min TTL)

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class ModelPrice:
    """Dollars per *token* for each usage bucket (built from $/Mtok table entries below)."""

    input: float
    output: float
    cache_write: float
    cache_read: float


def _anthropic(input_mtok: float, output_mtok: float) -> ModelPrice:
    """Claude pricing. Cache economics are a fixed multiple of the input rate: a 5-minute cache
    write costs 1.25x input, a cache read costs 0.1x input (the published Anthropic ratios)."""
    base = input_mtok / _PER_MTOK
    return ModelPrice(input=base, output=output_mtok / _PER_MTOK,
                      cache_write=base * 1.25, cache_read=base * 0.10)


def _openai(input_mtok: float, output_mtok: float, cached_input_mtok: float) -> ModelPrice:
    """OpenAI pricing. OpenAI charges no separate cache-*write* premium (writes bill as normal
    input) but discounts cache *reads* to an explicit `cached_input` rate, so we pass it in."""
    return ModelPrice(input=input_mtok / _PER_MTOK, output=output_mtok / _PER_MTOK,
                      cache_write=input_mtok / _PER_MTOK, cache_read=cached_input_mtok / _PER_MTOK)


# $/million-tokens, current list prices. Claude figures are authoritative (the production billing
# path, per the claude-api reference); the OpenAI rows are representative list prices — verify
# against current OpenAI pricing for your account, or override per run. Update on any model launch.
PRICES: dict[str, ModelPrice] = {
    # Claude (input / output $ per Mtok)
    "claude-opus-4-8": _anthropic(5.0, 25.0),
    "claude-opus-4-7": _anthropic(5.0, 25.0),
    "claude-opus-4-6": _anthropic(5.0, 25.0),
    "claude-sonnet-4-6": _anthropic(3.0, 15.0),
    "claude-haiku-4-5": _anthropic(1.0, 5.0),
    "claude-fable-5": _anthropic(10.0, 50.0),
    # OpenAI (input / output / cached-input $ per Mtok)
    "gpt-4o": _openai(2.5, 10.0, 1.25),
    "gpt-4o-mini": _openai(0.15, 0.60, 0.075),
    "gpt-4.1": _openai(2.0, 8.0, 0.50),
    "gpt-4.1-mini": _openai(0.40, 1.60, 0.10),
    "o4-mini": _openai(1.10, 4.40, 0.275),
}

# Per-adapter default model, so an adapter with no configured model still prices correctly.
DEFAULT_MODELS: dict[str, str] = {
    "claude-api": "claude-opus-4-8",
    "openai-api": "gpt-4o",
}


def known_model(model: str | None) -> bool:
    """True if we can price this model — i.e. the budget stop can actually fire on it."""
    return model is not None and model in PRICES


def estimate_cost(model: str | None, usage: Usage) -> float:
    """Dollar cost of `usage` on `model`. Unknown model → 0.0 (doctor warns; budget can't bite)."""
    price = PRICES.get(model or "")
    if price is None:
        return 0.0
    return (usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * price.cache_write)
