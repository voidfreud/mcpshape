"""Hooks and Virtual Tools: what a Proxy's Python file is written against, and how it runs.

A user drops ``<proxy>.py`` next to ``<proxy>.toml`` and writes::

    from mcpshape import hook, tool, upstream

    @hook.before("create_issue")            # a tool, by Catalog name
    def label(call):
        call.args["labels"] = ["proxied"]   # rewrite the arguments in place

    @hook.after("create_issue")             # sync or async, as the user likes
    async def trim(call, result):
        return result.text[:2000]           # the result to send; None keeps it as is

    @hook.before.resource("issues://{id}")  # resources and prompts too, by Catalog name
    @hook.after.prompt("triage")

    @tool                                   # a Virtual Tool: schema from the signature
    async def close_all(ids: list[int]) -> str:
        \"\"\"Close several issues.\"\"\"    # description from the docstring
        for id in ids:
            await upstream.call("close_issue", id=id)
        return "done"

A ``before`` Hook may change ``call.args`` or return a result, which short-circuits the
Upstream. An ``after`` Hook runs on a tool's result whether it is a success or an error the
Upstream reported (``result.is_error``), short-circuited or not, and returns the one to send:
a rewritten error, a success (set ``result.is_error = False``), or ``None`` to leave it as is.
Raising anywhere becomes an error to the Client carrying the exception's message. A resource
read or prompt get has no error result on the wire, only an exception, which still skips its
``after`` Hooks. A Virtual Tool's exposed name is its identity, so Hooks keyed by it run around
it too.
``upstream`` reaches the Proxy's own Upstream under Catalog names, and nothing else; what it
calls does not run the Hooks. Hooks run in the Daemon process with no sandbox: a function
that blocks forever or calls ``sys.exit`` is not guarded against.

Results are mcpshape's own small types over MCP's wire shapes, never FastMCP's (ADR 0001).
The adapter converts at its edge. This module knows no FastMCP.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import re
import sys
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, overload

from mcpshape.catalog import Item

if TYPE_CHECKING:
    from collections.abc import Awaitable, Generator

log = logging.getLogger("mcpshape.hooks")

Target = Literal["tool", "resource", "prompt"]
"""What a Hook is keyed on. A resource template is hooked under ``resource`` by its template."""

When = Literal["before", "after"]


class UpstreamError(Exception):
    """The Upstream answered a call, read, or get from user code with an error."""


class UserCodeError(Exception):
    """A Proxy's Python file could not be loaded. ``traceback`` holds the full story."""

    def __init__(self, message: str, formatted: str) -> None:
        super().__init__(message)
        self.traceback = formatted


# --- what user code sees and returns ---------------------------------------------------------


@dataclass
class Call:
    """One request on its way to the Upstream, under Catalog names."""

    kind: Target
    name: str
    """The Catalog name: a tool's or prompt's name, a resource's URI or URI template."""
    args: dict[str, Any]
    """Arguments under Catalog names, or a template's parameters. Change them in place."""

    def __str__(self) -> str:
        return f"{self.kind} {self.name}"


@dataclass
class ToolResult:
    """What a tool call answers: MCP content blocks as plain dicts, plus structured content.

    A tool that advertises an output schema is expected to answer with structured content
    matching it, and Clients check. When user code leaves ``structured`` as ``None``, mcpshape
    derives it from the text where the schema allows: a schema wrapping one value takes the
    text (or the JSON it parses as), and an object schema takes the text when it is a JSON
    object. Anything else is the user's to match.

    ``is_error`` says whether the Upstream reported this as an error; an ``after`` Hook sees it
    on the result it is handed and may flip it in either direction (setting ``.text`` leaves it
    as it is: a Hook that wants a success sets ``is_error = False`` itself).
    """

    content: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    structured: dict[str, Any] | None = None
    is_error: bool = False

    @property
    def text(self) -> str:
        """The text blocks joined. Setting it replaces the whole result with that text."""
        return _joined_text(self.content)

    @text.setter
    def text(self, value: str) -> None:
        self.content = [_text_block(value)]
        self.structured = None

    @classmethod
    def of(cls, value: object) -> ToolResult:
        """``value`` as a successful tool result: a string is text, a dict is structured, blocks
        stay, and a ``ToolResult`` passes through unchanged, ``is_error`` included."""
        match value:
            case ToolResult():
                return value
            case str():
                return cls(content=[_text_block(value)])
            case dict():
                structured: dict[str, Any] = dict(value)  # pyright: ignore[reportUnknownArgumentType]  # user data
                return cls(content=[_text_block(_json(structured))], structured=structured)
            case list() if _all_blocks(value):  # pyright: ignore[reportUnknownArgumentType]  # user data
                return cls(content=list(value))  # pyright: ignore[reportUnknownArgumentType]  # user data
            case _:
                return cls(content=[_text_block(_json(value))])


@dataclass
class Content:
    """One item of a resource's contents: text or bytes, with its MIME type."""

    data: str | bytes
    mime_type: str | None = None


@dataclass
class ResourceResult:
    """What a resource read answers."""

    contents: list[Content] = field(default_factory=list[Content])

    @property
    def text(self) -> str:
        """The text contents joined; setting it replaces everything with one text content."""
        return "\n".join(item.data for item in self.contents if isinstance(item.data, str))

    @text.setter
    def text(self, value: str) -> None:
        mime = next((item.mime_type for item in self.contents if isinstance(item.data, str)), None)
        self.contents = [Content(value, mime)]

    @classmethod
    def of(cls, value: object) -> ResourceResult:
        """``value`` as a read result: a string or bytes is one content, a list is several."""
        match value:
            case ResourceResult():
                return value
            case str() | bytes():
                return cls(contents=[Content(value)])
            case list():
                items = cast("list[object]", value)
                return cls(contents=[_content(item) for item in items])
            case _:
                return cls(contents=[Content(_json(value), "application/json")])


@dataclass
class Message:
    """One prompt message: plain text, or a raw MCP content block, with a role."""

    content: str | dict[str, Any]
    role: str = "user"

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return str(self.content.get("text", ""))

    @text.setter
    def text(self, value: str) -> None:
        self.content = value

    @classmethod
    def of(cls, value: object) -> Message:
        match value:
            case Message():
                return value
            case str():
                return cls(value)
            case _:
                return cls(_json(value))


@dataclass
class PromptResult:
    """What a prompt get answers."""

    messages: list[Message] = field(default_factory=list[Message])
    description: str | None = None

    @classmethod
    def of(cls, value: object) -> PromptResult:
        """``value`` as a prompt result: a string is one user message, a list is several."""
        match value:
            case PromptResult():
                return value
            case list():
                items = cast("list[object]", value)
                return cls(messages=[Message.of(item) for item in items])
            case _:
                return cls(messages=[Message.of(value)])


def _text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _joined_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")


def _all_blocks(items: list[object]) -> bool:
    return bool(items) and all(isinstance(item, dict) and "type" in item for item in items)


def _content(item: object) -> Content:
    match item:
        case Content():
            return item
        case str() | bytes():
            return Content(item)
        case _:
            return Content(_json(item), "application/json")


def _json(value: object) -> str:
    return json.dumps(value, default=str)


# --- the upstream handle -----------------------------------------------------------------------


class UpstreamHandle(Protocol):
    """The Proxy's own Upstream, as the adapter provides it to user code."""

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult: ...
    async def read(self, uri: str) -> ResourceResult: ...
    async def get(self, name: str, args: dict[str, Any]) -> PromptResult: ...


_bound: ContextVar[UpstreamHandle | None] = ContextVar("mcpshape_upstream", default=None)


class _Upstream:
    """``upstream``: the Proxy's own Upstream, from inside a Hook or a Virtual Tool.

    Names are Catalog names. Calls, reads, and gets go straight to the Upstream, past every
    Hook. An error the Upstream reports raises ``UpstreamError``.
    """

    async def call(
        self, name: str, args: dict[str, Any] | None = None, /, **kwargs: object
    ) -> ToolResult:
        """Call the Upstream tool ``name`` with ``args``, as a dict, as keywords, or both."""
        return await _handle().call(name, {**(args or {}), **kwargs})

    async def read(self, uri: str) -> ResourceResult:
        """Read the Upstream resource at ``uri``."""
        return await _handle().read(uri)

    async def get(
        self, name: str, args: dict[str, Any] | None = None, /, **kwargs: object
    ) -> PromptResult:
        """Get the Upstream prompt ``name`` with ``args``, as a dict, as keywords, or both."""
        return await _handle().get(name, {**(args or {}), **kwargs})


upstream = _Upstream()


def _handle() -> UpstreamHandle:
    handle = _bound.get()
    if handle is None:
        msg = "upstream can only be used from inside a Hook or a Virtual Tool while it runs"
        raise RuntimeError(msg)
    return handle


@contextmanager
def bound(handle: UpstreamHandle) -> Generator[None]:
    """Make ``upstream`` reach ``handle`` for the user code run inside."""
    token = _bound.set(handle)
    try:
        yield
    finally:
        _bound.reset(token)


# --- what a file defines -----------------------------------------------------------------------


@dataclass(frozen=True)
class VirtualTool:
    """A tool the Proxy exposes that the Upstream never had: the user's function."""

    name: str
    fn: Callable[..., Any]
    description: str | None = None


Hooks = dict[Item, list[Callable[..., Any]]]
"""Hooks by the item they are keyed on, in file order."""


def _no_hooks() -> dict[When, Hooks]:
    return {"before": {}, "after": {}}


@dataclass
class UserCode:
    """Everything one Proxy's Python file registered."""

    registered: dict[When, Hooks] = field(default_factory=_no_hooks)
    tools: dict[str, VirtualTool] = field(default_factory=dict[str, VirtualTool])
    """Virtual Tools by exposed name, in file order."""

    def hooks(self, when: When, call: Call) -> list[Callable[..., Any]]:
        return self.registered[when].get(Item(call.kind, call.name), [])

    def add(self, when: When, item: Item, fn: Callable[..., Any]) -> None:
        self.registered[when].setdefault(item, []).append(fn)

    def hooked(self) -> set[Item]:
        """Every item any Hook names."""
        return set(self.registered["before"]) | set(self.registered["after"])


_loading: ContextVar[UserCode | None] = ContextVar("mcpshape_loading", default=None)


def _registry() -> UserCode:
    code = _loading.get()
    if code is None:
        msg = "mcpshape's decorators only work in a Proxy's Python file while mcpshape loads it"
        raise RuntimeError(msg)
    return code


class _Registrar:
    """``hook.before`` or ``hook.after``: called for a tool, or ``.resource`` / ``.prompt``."""

    def __init__(self, when: When) -> None:
        self._when: When = when

    def __call__(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.tool(name)

    def tool(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._register(Item("tool", name))

    def resource(self, uri: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._register(Item("resource", uri))

    def prompt(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._register(Item("prompt", name))

    def _register(self, item: Item) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            _registry().add(self._when, item, fn)
            return fn

        return decorator


class _HookApi:
    before = _Registrar("before")
    after = _Registrar("after")


hook = _HookApi()


@overload
def tool(fn: Callable[..., Any], /) -> Callable[..., Any]: ...
@overload
def tool(
    *, name: str | None = None, description: str | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...
def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable[..., Any]:
    """Expose the function as a Virtual Tool, as ``@tool`` or ``@tool(name=..., description=...)``.

    The exposed name is the function's unless given; the description is the docstring unless
    given; the input schema comes from the signature's annotations.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        code = _registry()
        exposed = name or fn.__name__
        if exposed in code.tools:
            msg = f"Virtual Tool {exposed!r} is defined twice"
            raise ValueError(msg)
        code.tools[exposed] = VirtualTool(exposed, fn, description)
        return fn

    return decorator(fn) if fn is not None else decorator


# --- loading -----------------------------------------------------------------------------------

MODULE_PREFIX = "mcpshape_user."
"""User files are imported as ``mcpshape_user.<upstream>_<proxy>``, replaced on every load."""


def load_user_code(path: Path, label: str) -> UserCode:
    """Import the Proxy ``label``'s Python file at ``path``; nothing there means no user code.

    While the file loads, its Upstream's directory is importable, so ``import helpers`` finds
    ``upstreams/<name>/helpers.py``: the directory goes on the front of ``sys.path`` for the
    duration and comes off again after, in a ``finally``. A helper is dropped from
    ``sys.modules`` first, so it is re-imported fresh on every load: every Proxy of the
    Upstream re-reads its files when a helper changes, and a helper of one Upstream is never
    seen by another Upstream's Proxy, since the module cache never keeps the wrong one bound
    to a name two Upstreams both use. Bytecode caching is off for the duration too, so a
    helper edited twice within the same second, which its cached ``.pyc`` would otherwise
    consider unchanged, is still read fresh.

    Raises ``UserCodeError`` when the file cannot be loaded, for whatever reason: a syntax
    error, an import that fails, an exception at module level, including one raised while
    importing a helper. The Daemon never crashes on user code, and ``doctor`` reports the
    same message.
    """
    if not path.is_file():
        return UserCode()
    name = MODULE_PREFIX + re.sub(r"\W", "_", label)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"{path}: cannot be imported"
        raise UserCodeError(msg, "")
    module = importlib.util.module_from_spec(spec)
    code = UserCode()
    token = _loading.set(code)
    sys.modules[name] = module
    upstream_dir = str(path.parent)
    _drop_stale_helpers(path.parent.parent.resolve())
    sys.path.insert(0, upstream_dir)
    dont_write_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        formatted = "".join(traceback.format_exception(exc))
        raise UserCodeError(_summary(path, exc), formatted) from exc
    finally:
        sys.path.remove(upstream_dir)
        sys.dont_write_bytecode = dont_write_bytecode
        _loading.reset(token)
    return code


def _drop_stale_helpers(upstreams_dir: Path) -> None:
    """Forget every module already imported from under ``upstreams_dir``.

    Import caches by module name, so a ``helpers`` module one Upstream's Proxy imported would
    otherwise be handed straight back to another Upstream's Proxy, or to this same Proxy on a
    later load after the file on disk changed. Dropping it here is what makes the next
    ``import helpers`` read the file fresh, from whichever Upstream's directory is on
    ``sys.path`` for this load.
    """
    for mod_name, module in list(sys.modules.items()):
        if mod_name.startswith(MODULE_PREFIX):
            continue
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            resolved = Path(file).resolve()
        except OSError:
            continue
        if upstreams_dir in resolved.parents:
            del sys.modules[mod_name]


def _summary(path: Path, exc: BaseException) -> str:
    """``default.py, line 3: NameError: name 'x' is not defined``: what the Client is told."""
    line = getattr(exc, "lineno", None) if isinstance(exc, SyntaxError) else None
    if line is None:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == str(path)]
        line = frames[-1].lineno if frames else None
    where = f"{path.name}, line {line}" if line is not None else path.name
    reason = "".join(traceback.format_exception_only(exc)).strip().splitlines()[-1]
    return f"{where}: {reason}"


# --- the chain a call runs through -----------------------------------------------------------


async def run_call[R](  # noqa: PLR0913, PLR0917  # every one of these is state the chain needs
    code: UserCode,
    call: Call,
    forward: Callable[[Call], Awaitable[R]],
    of: Callable[[object], R],
    cap: Callable[[R], R] | None = None,
    check: Callable[[R, str], None] | None = None,
) -> R:
    """Run ``call`` through its Hooks: before, the Upstream unless short-circuited, after, Cap.

    ``forward`` reaches the Upstream with the arguments as the ``before`` Hooks left them;
    ``of`` turns whatever a Hook returns into the result type. ``cap`` is where the tool output
    Cap slots in: it runs last, on whatever the Hooks leave, short-circuited or not, so what a
    Client receives never exceeds it either way. ``check``, when given, runs right after each
    Hook that returned a value, ``before`` or ``after``, on the result and that Hook's name
    (its ``__name__``, or ``repr`` when it has none); whatever it raises propagates like a Hook
    raising. A Hook that raises is logged and its exception re-raised for the adapter to turn
    into the Client's error.
    """
    result: R | None = None
    for fn in code.hooks("before", call):
        answer = await _invoke(fn, call, call)
        if answer is not None:
            result = of(answer)
            if check is not None:
                check(result, _hook_name(fn))
            break
    if result is None:
        result = await forward(call)
    for fn in code.hooks("after", call):
        answer = await _invoke(fn, call, call, result)
        if answer is not None:
            result = of(answer)
            if check is not None:
                check(result, _hook_name(fn))
    return cap(result) if cap is not None else result


async def _invoke(fn: Callable[..., Any], call: Call, *args: object) -> object:
    """Call the Hook, sync or async, logging what it raises before letting it through."""
    try:
        answer: object = fn(*args)
        if inspect.isawaitable(answer):
            answer = await answer
    except Exception:
        log.warning("Hook %s on %s raised", _hook_name(fn), call, exc_info=True)
        raise
    return answer


def _hook_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", None) or repr(fn)
