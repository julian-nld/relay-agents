"""opencode (kind opencode), through an opencode-manager profile's service."""

from relay.providers.opencode.provider import OpencodeProvider, opencode_profiles, opencode_template

KINDS = (OpencodeProvider,)  # (what this folder adds: relay.providers finds it)

__all__ = ["OpencodeProvider", "opencode_profiles", "opencode_template", "KINDS"]
