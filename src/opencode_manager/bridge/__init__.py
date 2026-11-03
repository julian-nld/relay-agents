"""Bridge: drive an AI coding agent backend and stream its output as uniform events.

Every backend emits the same event dicts (see events.py), so callers don't
need to know whether opencode or Claude Code produced them.
"""
