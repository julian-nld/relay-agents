"""The guide's worked example (kind example): toy-agent, a tiny coding agent with a format of its own, and the
layer that brings it to Relay. docs/ADDING-A-PROVIDER.md builds it step by step."""

from relay.providers.example.provider import ToyAgentProvider

KINDS = (ToyAgentProvider,)  # (what this folder adds: relay.providers finds it)

__all__ = ["KINDS", "ToyAgentProvider"]
