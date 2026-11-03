"""Claude Code (kind claude-code): `claude -p` with stream-json, through the bridge's ClaudeCodeBackend."""

from relay.providers.claude.provider import ClaudeCodeProvider, claude_config_dir, claude_project_folder

KINDS = (ClaudeCodeProvider,)  # (what this folder adds: relay.providers finds it)

__all__ = ["ClaudeCodeProvider", "claude_config_dir", "claude_project_folder", "KINDS"]
