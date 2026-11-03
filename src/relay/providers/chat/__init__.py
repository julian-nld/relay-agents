"""Any OpenAI-compatible chat API (kind openai-compatible), with Relay's tools."""

from relay.providers.chat.provider import OpenAICompatibleProvider

KINDS = (OpenAICompatibleProvider,)  # (what this folder adds: relay.providers finds it)

__all__ = ["OpenAICompatibleProvider", "KINDS"]
