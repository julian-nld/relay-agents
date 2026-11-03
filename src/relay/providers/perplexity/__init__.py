"""Perplexity with the user's own account (kind perplexity), the site in a headless browser."""

from relay.providers.perplexity.provider import PerplexityProvider

KINDS = (PerplexityProvider,)  # (what this folder adds: relay.providers finds it)

__all__ = ["PerplexityProvider", "KINDS"]
