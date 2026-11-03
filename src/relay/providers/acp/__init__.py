"""Agents that speak the Agent Client Protocol (kind acp), and GitHub Copilot through it (kind copilot)."""

from relay.providers.acp.provider import AcpProvider, CopilotProvider

KINDS = (CopilotProvider, AcpProvider)  # (what this folder adds: relay.providers finds it)

__all__ = ["AcpProvider", "CopilotProvider", "KINDS"]
