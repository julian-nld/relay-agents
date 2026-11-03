"""Plain chat with any OpenAI-compatible API (OpenRouter, Ollama, llama.cpp, …)."""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

from opencode_manager.bridge.billing import API, FREE, is_local_url, local_host
from relay import __version__, limits, tunables
from relay.providers.base import (
    Context,
    Event,
    Field,
    Provider,
    ProviderError,
    ProviderRun,
    TemplateError,
)
from relay.toolbox import Toolbox

CHAT_EFFORTS = ("low", "medium", "high")


class OpenAICompatibleProvider(Provider):
    KIND = "openai-compatible"
    LABEL = "OpenAI-compatible API"
    DESCRIPTION = (
        "Chats with any server that speaks the OpenAI chat-completions API: OpenRouter, "
        "Ollama, LM Studio, llama.cpp, vLLM, OpenAI, … Relay gives models that can call functions its "
        "own tools: files and commands in code tasks, web pages, skills and subagents."
    )
    TOOLS = False  # not an agent of its own: Relay runs the tool loop (runs_tools)
    REWINDABLE = True
    FIELDS = (
        Field("base_url", "string", "API base URL, e.g. https://openrouter.ai/api/v1.",
              required=True),
        Field("api_key", "string", "API key (stored in Relay's data folder).", secret=True),
        Field("api_key_env", "string", "Or: name of an environment variable holding the key."),
        Field("models", "model-list", "Models to offer; default: the server's /models list."),
        Field("system_prompt", "string", "System prompt sent with every conversation."),
        Field("headers", "object", "Extra HTTP headers, e.g. {\"HTTP-Referer\": \"…\"}."),
        Field("tools", "boolean", "Offer Relay's tools to models that can call functions: web pages, skills, "
              "subagents, and files and commands in code tasks (default: true)."),
    )
    EXAMPLE = {
        "id": "ollama",
        "name": "Ollama",
        "kind": "openai-compatible",
        "options": {"base_url": "http://127.0.0.1:11434/v1", "models": ["llama3.2"]},
    }

    def validate(self) -> None:
        super().validate()
        url = self.options.get("base_url", "")
        if not url.startswith(("http://", "https://")):
            raise TemplateError("Option 'base_url' must start with http:// or https://")

    def _headers(self) -> dict[str, str]:
        # Python's own User-Agent gets turned away by some servers (Cloudflare's error 1010).
        headers = {"Content-Type": "application/json", "User-Agent": f"Relay/{__version__}",
                   **self.options.get("headers", {})}
        key = self.options.get("api_key") or os.environ.get(self.options.get("api_key_env") or "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _url(self, path: str) -> str:
        return self.options["base_url"].rstrip("/") + path

    def _get(self, path: str) -> Any:
        request = urllib.request.Request(self._url(path), headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{self._url(path)}: {exc}") from exc

    def models(self) -> list[dict[str, str]]:
        if self.options.get("models"):
            return super().models()
        try:
            data = self._get("/models").get("data", [])
        except ProviderError:
            return []
        return sorted(({"id": m["id"], "name": m.get("name") or m["id"]} for m in data
                       if isinstance(m, dict) and m.get("id")), key=lambda m: m["name"].lower())

    def efforts(self, model: str | None) -> list[str]:
        return list(CHAT_EFFORTS)  # reasoning_effort; servers ignore it for models without reasoning

    def local_server(self, model: str | None) -> str | None:
        return local_host(self.options["base_url"])

    def runs_tools(self) -> bool:
        return self.options.get("tools", True) is not False

    def boxed(self) -> bool:
        return True  # (no program: Relay calls its API, and runs its tools' commands in a WorkBox)

    def billing(self, model: str | None) -> str | None:
        if is_local_url(self.options["base_url"]) or (model or "").endswith(":free"):  # OpenRouter's free ones
            return FREE
        return API

    def check(self) -> str:
        data = self._get("/models").get("data", [])
        return f"Reachable; {len(data)} models available"

    def suggest_title(self, prompt: str, answer: str, model: str | None = None) -> str | None:
        """A short title for a new chat, from the same model (one small request)."""
        model = model or next((m["id"] for m in self.models() if m["id"]), None)
        if not model:
            return None
        body = {
            "model": model, "stream": False, "max_tokens": 24, "temperature": 0.2,
            "messages": [{"role": "user", "content":
                          "Write a title of at most 6 words for this conversation. "
                          "Reply with the title only, no quotes.\n\n"
                          f"User: {prompt[:1500]}\n\nAssistant: {answer[:1500]}"}],
        }
        request = urllib.request.Request(self._url("/chat/completions"), data=json.dumps(body).encode(),
                                         headers=self._headers(), method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        choice = (data.get("choices") or [{}])[0]
        return (choice.get("message") or {}).get("content")

    def start(self, context: Context, prompt: str) -> ProviderRun:
        model = context.model or next((m["id"] for m in self.models() if m["id"]), None)
        if not model:
            raise ProviderError("No model selected and the server lists none")
        messages = []
        mode = context.agent_mode
        toolbox = None
        if self.runs_tools():
            toolbox = Toolbox(context.directory, code=context.mode == "code", shell=context.shell,
                              relay_url=context.mcp_url,
                              computers=context.computers, focus=context.computer, search=context.search,
                              fetch=context.fetch,
                              memory=bool(context.memory), subagents=context.subagents)
        system = [self.options.get("system_prompt"), mode.guidance if mode else None,
                  toolbox.guidance() if toolbox else None, context.memory]
        if any(system):
            messages.append({"role": "system", "content": "\n\n".join(filter(None, system))})
        messages += [
            {"role": m["role"], "content": _content(m["content"], m.get("attachments") or [])
             if m["role"] == "user" else m["content"]}
            for m in context.history
        ]
        messages.append({"role": "user", "content": _content(prompt, context.attachments)})
        body = {"model": model, "messages": messages, "stream": True,
                "stream_options": {"include_usage": True}}
        if context.effort:
            # The OpenAI-style levels; stronger ones become the strongest there is.
            body["reasoning_effort"] = context.effort if context.effort in CHAT_EFFORTS else "high"
        return _ChatRun(self._url("/chat/completions"), self._headers(), body,
                        context.provider_session or str(uuid.uuid4()), model,
                        self.billing(model),
                        toolbox=toolbox)


# (base URL, model) of servers that refused tools: plain chat from then on.
NO_TOOLS: set[tuple[str, str]] = set()


class _Call:
    """A tool call as it streams in."""

    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments = ""


class _ChatRun(ProviderRun):
    def __init__(self, url: str, headers: dict[str, str], body: dict, session: str,
                 model: str, billing: str, toolbox: Toolbox | None = None) -> None:
        self.url = url
        self.billing = billing
        self.headers = headers
        self.body = body
        self.session = session
        self.model = model
        self.toolbox = toolbox
        self.response = None
        self.stopped = threading.Event()
        self.call_ids: set[str] = set()  # some models number their calls anew each round

    def events(self) -> Iterator[Event]:
        # These conversations live in Relay; the "session" is just Relay's own id.
        yield {"type": "session", "session": self.session, "new": False}
        yield {"type": "step", "agent": None, "model": self.model}
        if self.stopped.is_set():
            yield {"type": "done", "status": "interrupted"}
            return
        key = (self.url, self.model)
        tools = self.toolbox.definitions() if self.toolbox and key not in NO_TOOLS else None
        messages = list(self.body["messages"])
        used = {"prompt_tokens": 0, "completion_tokens": 0}
        cost, reported = 0.0, False
        rounds = 0
        failed = False  # an error ends it as failed (after the usage so far is told)
        while True:
            body = {**self.body, "messages": messages, **({"tools": tools} if tools else {})}
            calls: dict[int, _Call] = {}
            said: list[str] = []
            try:
                for chunk in self._stream(body):
                    for event in _translate(chunk, self.billing):
                        if event["type"] == "usage":
                            reported = True
                            for name in used:
                                used[name] += int(event["tokens"].get(name) or 0)
                            cost += float(event.get("cost") or 0)
                            continue
                        if event["type"] == "text":
                            said.append(event["delta"])
                        elif event["type"] == "error":
                            failed = True
                        yield event
                    for choice in chunk.get("choices") or []:
                        for part in (choice.get("delta") or {}).get("tool_calls") or []:
                            call = calls.setdefault(int(part.get("index", len(calls))), _Call())
                            call.id = part.get("id") or call.id
                            function = part.get("function") or {}
                            call.name += function.get("name") or ""
                            arguments = function.get("arguments")
                            call.arguments += arguments if isinstance(arguments, str) else (
                                json.dumps(arguments) if arguments else "")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if tools and rounds == 0 and "tool" in detail.lower():
                    NO_TOOLS.add(key)  # this server or model doesn't do function calling
                    tools = None
                    continue
                if exc.code == 429:  # a rate or usage limit: say when it resets, if the server does
                    resets = limits.from_headers(exc.headers) or limits.parse_reset(detail)
                    yield {"type": "limit", "status": "rejected", "resets_at": resets, "message": detail[:300]}
                yield {"type": "error", "message": f"HTTP {exc.code}: {detail}"}
                failed = True
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if not self.stopped.is_set():
                    yield {"type": "error", "message": str(exc)}
                    failed = True
                    break
            if failed or self.stopped.is_set() or not calls:
                break
            rounds += 1
            if rounds > tunables.get("chat.rounds"):
                yield {"type": "error", "message": f"Stopped after {tunables.get('chat.rounds')} rounds of tools."}
                failed = True
                break
            yield from self._run_tools(calls, messages, rounds, "".join(said))
        if reported:
            yield {"type": "usage", "tokens": used, "cost": cost or None, "billing": self.billing}
        yield {"type": "done",
               "status": "interrupted" if self.stopped.is_set() else "failed" if failed else "succeeded"}

    def _stream(self, body: dict) -> Iterator[dict]:
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers=self.headers, method="POST")
        try:
            self.response = urllib.request.urlopen(request, timeout=300)
            if self.stopped.is_set():  # interrupted while connecting
                self.response.close()
            finished = False  # [DONE], or a choice's finish_reason: not cut off halfway
            for raw in self.response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    finished = True
                    break
                chunk = json.loads(payload)
                finished = finished or any(c.get("finish_reason") for c in chunk.get("choices") or [])
                yield chunk
            if not finished and not self.stopped.is_set():
                raise ValueError("The answer broke off: the connection ended before it was done")
        finally:
            if self.response is not None:
                self.response.close()

    def _run_tools(self, calls: dict[int, _Call], messages: list, round_: int, said: str) -> Iterator[Event]:
        """Run the tools the model called and add their results to the conversation."""
        ordered = [calls[i] for i in sorted(calls)]
        for n, call in enumerate(ordered):
            call.id = call.id or f"call_{round_}_{n}"
            if call.id in self.call_ids:  # events (and the answers) must tell calls apart
                call.id = f"{call.id}_{round_}_{n}"
            self.call_ids.add(call.id)
        messages.append({"role": "assistant", "content": said or None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments or "{}"}}
            for c in ordered]})
        for call in ordered:
            try:
                shown = json.loads(call.arguments or "{}")
            except ValueError:
                shown = call.arguments
            yield {"type": "tool_call", "id": call.id, "name": call.name, "input": shown}
            if self.stopped.is_set():
                ok, output = False, "Stopped."
            else:
                ok, output = self.toolbox.call(call.name, call.arguments)
            yield {"type": "tool_result", "id": call.id, "name": call.name, "ok": ok, "output": output}
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

    def interrupt(self) -> None:
        self.stopped.set()
        if self.toolbox is not None:
            self.toolbox.stop()
        if self.response is not None:
            try:
                self.response.close()
            except OSError:
                pass


MAX_INLINE_TEXT = 200_000


def _content(prompt: str, attachments: list) -> str | list[dict[str, Any]]:
    """The user message: plain text, or text and image parts with attachments.

    Images go as image parts, text files are inlined; the model is told about
    anything else it can't read.
    """
    if not attachments:
        return prompt
    text = prompt
    images = []
    for attachment in attachments:
        if attachment.is_image:
            data = base64.b64encode(attachment.path.read_bytes()).decode()
            images.append({"type": "image_url",
                           "image_url": {"url": f"data:{attachment.mime};base64,{data}"}})
        elif attachment.is_text and attachment.size <= MAX_INLINE_TEXT:
            body = attachment.path.read_text(errors="replace")
            text += f"\n\n--- {attachment.name} ---\n{body}"
        else:
            text += f"\n\n[Attached file {attachment.name} ({attachment.mime}) can't be read by this model.]"
    return [{"type": "text", "text": text}, *images]


def _translate(chunk: dict[str, Any], billing: str | None = None) -> list[Event]:
    out: list[Event] = []
    if chunk.get("error"):
        out.append({"type": "error", "message": str(chunk["error"])})
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            out.append({"type": "reasoning", "delta": reasoning})
        if delta.get("content"):
            out.append({"type": "text", "delta": delta["content"]})
    if chunk.get("usage"):
        # OpenRouter reports what a request cost; most servers don't.
        out.append({"type": "usage", "tokens": chunk["usage"], "cost": chunk["usage"].get("cost"),
                    "billing": billing})
    return out
