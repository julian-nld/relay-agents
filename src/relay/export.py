"""Conversations as Markdown, for exporting."""

from __future__ import annotations

import time
from typing import Any

from relay.handover import tool_detail


def _tool_line(call: dict[str, Any], result: dict[str, Any] | None) -> str:
    outcome = "" if result is None else (" ✓" if result.get("ok") else " ✗")
    return f"> 🔧 **{call.get('name') or 'tool'}** `{tool_detail(call)}`{outcome}"


def _answer(message: dict[str, Any]) -> list[str]:
    """An assistant turn: its text, with tool calls noted where they happened."""
    lines, text = [], ""
    results = {e.get("id"): e for e in message.get("events", []) if e.get("type") == "tool_result"}
    for event in message.get("events", []):
        kind = event.get("type")
        if kind == "text":
            text += event.get("delta", "")
        elif kind == "tool_call":
            if text.strip():
                lines += [text.strip(), ""]
            text = ""
            lines += [_tool_line(event, results.get(event.get("id"))), ""]
        elif kind == "error":
            lines += [f"> ⚠️ {event.get('message')}", ""]
    if text.strip():
        lines += [text.strip(), ""]
    if not lines and message.get("content"):
        lines = [message["content"].strip(), ""]
    status = message.get("status")
    if status in ("failed", "interrupted"):
        lines += [f"*({status})*", ""]
    return lines


def answer_markdown(message: dict[str, Any]) -> str:
    """One answer as Markdown, its tool calls where they happened (Relay's SDK: a message's text)."""
    return "\n".join(_answer(message)).rstrip() + "\n"


def to_markdown(conversation: dict[str, Any], provider_name: str) -> str:
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(conversation["created"]))
    meta = [f"Exported from Relay · {created} · {provider_name}"
            + (f" · {conversation['model']}" if conversation.get("model") else "")]
    if conversation["mode"] == "code":
        meta.append(f"Project: `{conversation['directory']}`")
    lines = [f"# {conversation['title']}", "", *[f"*{m}*  " for m in meta], ""]
    for message in conversation["messages"]:
        if message["role"] == "user":
            lines += ["## You", ""]
            if message.get("content"):
                lines += [message["content"].strip(), ""]
            for attachment in message.get("attachments") or []:
                lines.append(f"📎 {attachment['name']}  ")
            if message.get("attachments"):
                lines.append("")
        else:
            lines += ["## Assistant", "", *_answer(message)]
    return "\n".join(lines).rstrip() + "\n"
