"""Handing a conversation over to an agent that starts a fresh session.

Agents keep their own sessions. When one starts without the conversation so
far (after switching provider, after an edit it can't rewind, or when its
session is gone), its first prompt carries a condensed transcript: every
message's text, tool calls as one line each, no tool output. When that is
too long, the oldest messages are left out of the prompt and the whole
transcript is written to a file the agent can read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay import config, tunables

# How much goes inline (Settings → Limits): the transcript, and one message within it.
MAX_CHARS: int | None = None          # set to override the setting (tests)
MAX_MESSAGE_CHARS: int | None = None


def _max_chars() -> int:
    return MAX_CHARS or tunables.get("handover.chars")


def _max_message_chars() -> int:
    return MAX_MESSAGE_CHARS or tunables.get("handover.message_chars")


@dataclass
class Handover:
    prompt: str
    file: Path | None = None  # the full transcript, when the prompt has only part of it


def prepare(conversation_id: str, history: list[dict[str, Any]], prompt: str) -> Handover:
    """`prompt` preceded by the conversation so far (nothing to add without history)."""
    blocks = [_block(m) for m in history if m.get("content") or m.get("tools")]
    if not blocks:
        return Handover(prompt)
    kept, left_out = _fit(blocks)
    file = None
    note = "tool output is left out"
    if left_out:
        file = _write(conversation_id, [_block(m, limit=None) for m in history
                                        if m.get("content") or m.get("tools")])
        note = (f"{left_out} earlier message{'s are' if left_out != 1 else ' is'} left out, and tool "
                f"output everywhere; the whole conversation is in {file} if you need more")
    # Worded as the user giving context: a system-like note in a user message
    # reads as a prompt injection, and careful models refuse it.
    transcript = "\n\n".join(kept)
    return Handover(
        "Some context first: this conversation started earlier, before this session (possibly "
        "with another assistant or model). Here it is so far, condensed "
        f"({note}):\n\n<earlier-conversation>\n{transcript}\n</earlier-conversation>\n\n"
        f"My new message:\n\n{prompt}",
        file,
    )


def answer_text(events: list[dict[str, Any]]) -> str:
    """A turn's answer as one text: its parts (said before, between and after its tool calls) as
    paragraphs, not run together ("…the files.The tests pass")."""
    text = part = ""
    for event in [*events, {"type": "step"}]:
        if event.get("type") == "text":
            part += event.get("delta", "")
        elif part and event.get("type") in ("tool_call", "tool_result", "step"):
            text += "\n\n" if text and not text[-1].isspace() and not part[0].isspace() else ""
            text, part = text + part, ""
    return text


def tool_detail(call: dict[str, Any]) -> str:
    """The gist of a tool call's input: its command, file, pattern, …"""
    detail = call.get("input")
    if isinstance(detail, dict):
        for key in ("command", "file_path", "filePath", "path", "pattern", "url", "query"):
            if isinstance(detail.get(key), str):
                return detail[key][:200]
        return json.dumps(detail)[:200]
    return str(detail or "")[:200]


def tool_line(call: dict[str, Any]) -> str:
    """One line for a tool call, e.g. "shell `ls -la`"."""
    return f"{call.get('name') or 'tool'} `{tool_detail(call)}`"


def _block(message: dict[str, Any], limit: int | None = -1) -> str:
    if limit == -1:
        limit = _max_message_chars()
    who = "User" if message["role"] == "user" else "Assistant"
    text = (message.get("content") or "").strip()
    if limit and len(text) > limit:
        text = text[:limit] + " […]"
    lines = [f"{who}: {text}" if text else f"{who}:"]
    lines += [f"  (tool: {tool})" for tool in message.get("tools") or []]
    names = [a.name for a in message.get("attachments") or []]
    if names:
        lines.append(f"  (attached: {', '.join(names)})")
    return "\n".join(lines)


def _fit(blocks: list[str]) -> tuple[list[str], int]:
    """The first message (it usually sets the task) plus as many recent ones as fit."""
    if sum(len(b) + 2 for b in blocks) <= _max_chars():
        return blocks, 0
    first, rest = blocks[0], blocks[1:]
    budget = _max_chars() - len(first)
    recent: list[str] = []
    for block in reversed(rest):
        if len(block) + 2 > budget:
            break
        recent.insert(0, block)
        budget -= len(block) + 2
    left_out = len(rest) - len(recent)
    if not left_out:  # (the first message alone was cut short)
        return [first, *recent], 0
    return [first, f"[… {left_out} message{'s' if left_out != 1 else ''} left out …]", *recent], left_out


def _write(conversation_id: str, blocks: list[str]) -> Path:
    folder = config.data_dir() / "handover" / conversation_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "transcript.md"
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def remove(conversation_id: str) -> None:
    import shutil

    shutil.rmtree(config.data_dir() / "handover" / conversation_id, ignore_errors=True)
