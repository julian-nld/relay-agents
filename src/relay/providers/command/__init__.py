"""Any program that prints the uniform events as JSON lines (kind command): the escape hatch."""

from relay.providers.command.provider import CommandProvider

KINDS = (CommandProvider,)  # (what this folder adds: relay.providers finds it)

__all__ = ["CommandProvider", "KINDS"]
