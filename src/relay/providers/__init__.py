"""Provider kinds and the registry of configured providers.

Configured providers are JSON templates in ~/.local/share/relay/providers/<id>.json:

    {"id": "openrouter", "name": "OpenRouter", "kind": "openai-compatible",
     "enabled": true, "options": {"base_url": "https://openrouter.ai/api/v1", ...}}

They can be added, edited and removed in the web UI (Settings → Providers),
or by editing these files.

Each kind lives in a folder of its own here (providers/<name>/: its Provider subclass, whatever it
needs, its tests), whose package names the kinds it adds (KINDS). They're found, not listed: adding a
kind is adding a folder (docs/ADDING-A-PROVIDER.md).
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencode_manager.jsonfile import write_json
from relay.providers.base import (
    ID_PATTERN,
    Context,
    Field,
    Provider,
    ProviderError,
    ProviderRun,
    Template,
    TemplateError,
)

# Relay's own kinds in the order Settings offers them; others (added folders) after them, by name
ORDER = ("claude-code", "opencode", "copilot", "openai-compatible", "acp", "perplexity", "command")


FIELD_TYPES = ("string", "boolean", "string-list", "model-list", "object")
# Provider folders that can't be loaded, and why (with the file and line): the others are, and Relay starts as
# always. Settings → Providers and `relay doctor` say so.
BROKEN: dict[str, str] = {}
HERE = Path(__file__).resolve().parent


def discover(folder: Path = HERE, package: str = __name__,
             broken: dict[str, str] | None = None) -> dict[str, type[Provider]]:
    """The kinds of the provider folders in `folder`: each package there with KINDS. A folder that can't be
    loaded (a mistake in it: a syntax error, an import, a kind that isn't one) goes into `broken`, the rest
    load: a mistake in a provider folder never keeps Relay from starting."""
    broken = BROKEN if broken is None else broken
    found: dict[str, type[Provider]] = {}
    where: dict[str, str] = {}
    for module in sorted(pkgutil.iter_modules([str(folder)]), key=lambda m: m.name):
        if not module.ispkg:
            continue
        name = f"providers/{module.name}"
        try:
            loaded = importlib.import_module(f"{package}.{module.name}")
            kinds = getattr(loaded, "KINDS", None)
            if not isinstance(kinds, (tuple, list)) or not kinds:
                raise TemplateError("its __init__.py names no kinds: KINDS = (TheProvider,)")
            for cls in kinds:
                problem = _invalid(cls, where)
                if problem:
                    raise TemplateError(problem)
                found[cls.KIND] = cls
                where[cls.KIND] = name
        except Exception as exc:  # noqa: BLE001 - whatever a folder does wrong is its own, said
            broken[name] = _why(exc, folder / module.name)
            print(f"relay: {name} can't be loaded: {broken[name]}", file=sys.stderr, flush=True)
            for kind in [k for k, w in where.items() if w == name]:  # (none of a folder half loaded)
                del found[kind], where[kind]
    rank = {kind: index for index, kind in enumerate(ORDER)}
    return dict(sorted(found.items(), key=lambda item: (rank.get(item[0], len(ORDER)), item[0])))


def _invalid(cls: Any, taken: dict[str, str]) -> str | None:
    """What makes `cls` no provider kind Relay can use; None when it is one."""
    if not (inspect.isclass(cls) and issubclass(cls, Provider)):
        return f"{cls!r} in KINDS isn't a Provider subclass"
    kind = getattr(cls, "KIND", None)
    if not isinstance(kind, str) or not ID_PATTERN.match(kind):
        return f"{cls.__name__}.KIND must be lowercase letters, digits and '-' (max 40), like \"my-agent\""
    if kind in taken:
        return f"kind {kind!r} is taken by {taken[kind]}: choose another KIND"
    for attribute, kinds in (("LABEL", str), ("DESCRIPTION", str), ("TOOLS", bool), ("EXAMPLE", dict)):
        if not isinstance(getattr(cls, attribute, None), kinds):
            return f"{cls.__name__}.{attribute} is missing (it must be a {kinds.__name__})"
    for field in cls.FIELDS:
        if not isinstance(field, Field) or field.type not in FIELD_TYPES:
            return f"{cls.__name__}.FIELDS: {field!r} isn't a Field of type {', '.join(FIELD_TYPES)}"
    if inspect.isabstract(cls):
        return f"{cls.__name__} doesn't write {' and '.join(sorted(cls.__abstractmethods__))} yet"
    example = cls.EXAMPLE
    if example.get("kind") != kind:
        return f"{cls.__name__}.EXAMPLE's kind is {example.get('kind')!r}, not {kind!r}"
    try:
        cls(Template(str(example.get("id") or kind), str(example.get("name") or kind), kind, True,
                     dict(example.get("options") or {})))
    except Exception as exc:  # noqa: BLE001 - its own options check, on its own example
        return f"{cls.__name__}.EXAMPLE isn't valid for it: {exc}"
    return None


def _why(exc: BaseException, folder: Path) -> str:
    """An exception as the mistake it is: what, and where in the folder (its file and line)."""
    if isinstance(exc, SyntaxError) and exc.filename:
        return f"{exc.msg} ({_short(exc.filename, folder)}, line {exc.lineno})"
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).resolve().is_relative_to(folder)]
    at = f" ({_short(frames[-1].filename, folder)}, line {frames[-1].lineno})" if frames else ""
    message = str(exc) if isinstance(exc, TemplateError) else f"{type(exc).__name__}: {exc}"
    return message + at


def _short(filename: str, folder: Path) -> str:
    try:
        return str(Path(filename).resolve().relative_to(folder.parent))
    except ValueError:
        return filename


def safely(call: Callable[..., Any], default: Any, *args: Any) -> Any:
    """What a provider's layer answers, or `default` when it raises: a mistake in a provider folder is that
    provider's trouble, never the page's or a loop's (said on stderr)."""
    try:
        return call(*args)
    except Exception as exc:  # noqa: BLE001 - whatever the layer raised
        owner = getattr(call, "__self__", None)
        kind = getattr(owner, "KIND", None) or getattr(type(owner), "KIND", "?")
        print(f"relay: provider kind {kind}: {getattr(call, '__name__', 'a call')} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return default


KINDS: dict[str, type[Provider]] = discover()


def has_shell(template: Template) -> bool:
    """Whether a provider has a Shell (Provider.has_shell): its kind may be gone, or have a mistake in its folder."""
    try:
        return KINDS[template.kind](template).has_shell()
    except Exception:  # noqa: BLE001 - whatever the layer raised: no shell known
        return False

# Created on first start so a fresh install has something to talk to. The
# opencode one follows opencode-manager's active profile; providers pinned
# to a profile are suggested in Settings.
DEFAULT_TEMPLATES = ("claude-code", "opencode")

__all__ = [
    "BROKEN", "KINDS", "Context", "Provider", "ProviderError", "ProviderRun", "Registry", "Template",
    "TemplateError", "draft_template", "has_shell", "parse_template", "safely",
]


def parse_template(data: Any) -> Template:
    """Validate a template document and return it (raises TemplateError)."""
    if not isinstance(data, dict):
        raise TemplateError("A template must be a JSON object")
    unknown = set(data) - {"id", "name", "kind", "enabled", "options", "managed", "home"}
    if unknown:
        raise TemplateError(f"Unknown template keys: {', '.join(sorted(unknown))}")
    template_id = data.get("id")
    if not isinstance(template_id, str) or not ID_PATTERN.match(template_id):
        raise TemplateError("'id' must be lowercase letters, digits and '-' (max 40)")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TemplateError("'name' is required")
    kind = data.get("kind")
    if kind not in KINDS and BROKEN:  # (its folder's mistake, most likely: that first)
        raise TemplateError(f"kind {kind!r} isn't loaded: a provider folder can't be ({', '.join(BROKEN)}: Settings → "
                            f"Providers says why); the kinds there are: {', '.join(KINDS)}")
    if kind not in KINDS:
        raise TemplateError(f"'kind' must be one of: {', '.join(KINDS)}")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise TemplateError("'enabled' must be true or false")
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise TemplateError("'options' must be an object")
    managed = data.get("managed", False)
    if not isinstance(managed, bool):
        raise TemplateError("'managed' must be true or false")
    home = data.get("home", "")
    if not isinstance(home, str) or (home and not Path(home).is_absolute()):
        raise TemplateError("'home' must be a folder's absolute path")
    template = Template(template_id, name.strip(), kind, enabled, options, managed, home)
    try:
        KINDS[kind](template)  # validates the options
    except TemplateError:
        raise
    except Exception as exc:  # (its layer's own check failed: this template's trouble, said)
        raise TemplateError(f"its kind {kind!r} couldn't check the options: {type(exc).__name__}: {exc}") from exc
    return template


def draft_template(kind: str, data: Any) -> Template:
    """A template as Add provider's page has it, not added yet: of the kind that page is for, and without a home of
    its own (that's a provider added with an agent: relay.creations)."""
    template = parse_template(data)
    if template.kind != kind or template.home:
        raise TemplateError(f"That's not a template of a new {kind} provider")
    return template


class Registry:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = threading.Lock()

    def ensure_defaults(self) -> None:
        """Create the default providers on a fresh install."""
        if self.directory.exists():
            return
        self.directory.mkdir(parents=True)
        for kind in DEFAULT_TEMPLATES:
            if kind in KINDS:  # (its folder loaded)
                self._write(parse_template(KINDS[kind].EXAMPLE))

    def suggestions(self) -> list[dict[str, Any]]:
        """Templates for opencode-manager profiles no provider is pinned to."""
        from relay.providers.opencode import opencode_profiles, opencode_template
        used = {t.options.get("profile") for t in self.templates() if t.kind == "opencode"}
        taken = {t.id for t in self.templates()}
        found = []
        for profile in opencode_profiles():
            template = opencode_template(profile)
            if profile not in used and template["id"] not in taken:
                found.append(template)
        return found

    def templates(self) -> list[Template]:
        found = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                found.append(parse_template(json.loads(path.read_text())))
            except (TemplateError, json.JSONDecodeError, OSError):
                continue  # listed as broken by problems()
        return found

    def problems(self) -> dict[str, str]:
        """Template files that can't be loaded, with the reason."""
        broken = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                parse_template(json.loads(path.read_text()))
            except (TemplateError, json.JSONDecodeError, OSError) as exc:
                broken[path.name] = str(exc)
        return broken

    def get(self, template_id: str) -> Template:
        for template in self.templates():
            if template.id == template_id:
                return template
        raise TemplateError(f"No provider {template_id!r}")

    def provider(self, template_id: str) -> Provider:
        template = self.get(template_id)
        return KINDS[template.kind](template)

    def add(self, data: Any) -> Template:
        template = parse_template(data)
        with self.lock:
            if self._path(template.id).exists():
                raise TemplateError(f"A provider with id {template.id!r} already exists")
            self._write(template)
        return template

    def update(self, template_id: str, data: Any) -> Template:
        template = parse_template(data)
        if template.id != template_id:
            raise TemplateError("The id of a provider can't be changed")
        with self.lock:
            if not self._path(template_id).exists():
                raise TemplateError(f"No provider {template_id!r}")
            self._write(template)
        return template

    def remove(self, template_id: str) -> None:
        with self.lock:
            path = self._path(template_id)
            if not ID_PATTERN.match(template_id) or not path.exists():
                raise TemplateError(f"No provider {template_id!r}")
            path.unlink()

    def _path(self, template_id: str) -> Path:
        return self.directory / f"{template_id}.json"

    def _write(self, template: Template) -> None:
        write_json(self._path(template.id), template.to_dict())
