"""Applying arbitrary Overrides to arbitrary Catalogs keeps names unique and identity intact.

At the proxy module on purpose (see CLAUDE.md): the property is about the pure application,
and driving thousands of generated Catalogs through in-memory Upstreams would prove nothing
more. Whatever the Overrides say, an exposed set has unique names per kind, maps every exposed
item back to exactly one Catalog item, never changes a schema type, and only refuses for the
four reasons it may: a name collision, a hidden argument nothing would supply, a resource
template exposed with other parameters than it takes, or a Virtual Tool named over the tool
name Cap in force (its name is its identity, so it cannot be cut).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcpshape.catalog import KINDS, Catalog, Item
from mcpshape.config import ProxyFile
from mcpshape.hooks import UserCode, VirtualTool
from mcpshape.model import CapError, CapSettings, ToolCapOverrides
from mcpshape.proxy import Exposed, OverrideError, cap, expose

names = st.from_regex(r"[a-c][a-z0-9_]{0,3}", fullmatch=True)
types = st.sampled_from(["string", "integer", "boolean", "array"])


@st.composite
def tools(draw: st.DrawFn) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name in draw(st.lists(names, max_size=4, unique=True)):
        properties = {
            p: {"type": draw(types)} for p in draw(st.lists(names, max_size=4, unique=True))
        }
        required = [p for p in properties if draw(st.booleans())]
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        found[name] = {"name": name, "description": "d", "inputSchema": schema}
    return found


@st.composite
def catalogs(draw: st.DrawFn) -> Catalog:
    return Catalog(
        scanned_at=datetime.now(UTC),
        tools=draw(tools()),
        resources={
            f"r://{n}": {"uri": f"r://{n}", "name": n}
            for n in draw(st.lists(names, max_size=3, unique=True))
        },
        resource_templates={
            f"t://{n}/{{id}}": {"uriTemplate": f"t://{n}/{{id}}", "name": n}
            for n in draw(st.lists(names, max_size=2, unique=True))
        },
        prompts={n: {"name": n} for n in draw(st.lists(names, max_size=3, unique=True))},
    )


optional_text = st.none() | st.text(min_size=1, max_size=5)


@st.composite
def argument_overrides(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "name": draw(st.none() | names),
        "description": draw(optional_text),
        "default": draw(st.none() | st.integers() | st.text(max_size=3)),
        "required": draw(st.none() | st.booleans()),
        "hidden": draw(st.booleans()),
    }


@st.composite
def proxy_files(draw: st.DrawFn, catalog: Catalog) -> ProxyFile:
    """Overrides keyed by names the Catalog has, plus a few it does not."""
    keys = {kind: list(catalog.items(kind)) + draw(st.lists(names, max_size=1)) for kind in KINDS}
    file: dict[str, Any] = {"version": 1, "name": draw(optional_text)}
    file["tools"] = {
        tool: {
            "name": draw(st.none() | names),
            "description": draw(optional_text),
            "hidden": draw(st.booleans()),
            "args": {
                arg: draw(argument_overrides())
                for arg in draw(st.lists(names, max_size=3, unique=True))
            },
        }
        for tool in keys["tool"]
        if draw(st.booleans())
    }
    file["resources"] = {
        uri: {
            "uri": draw(st.none() | names.map(lambda n: f"r://{n}")),
            "hidden": draw(st.booleans()),
        }
        for uri in keys["resource"]
        if draw(st.booleans())
    } | {
        uri: {
            "uri": draw(
                st.none()
                | names.map(lambda n: f"t://{n}/{{id}}")
                | names.map(lambda n: f"t://{{{n}}}")
            ),
            "hidden": draw(st.booleans()),
        }
        for uri in keys["resource_template"]
        if draw(st.booleans())
    }
    file["prompts"] = {
        prompt: {"name": draw(st.none() | names), "hidden": draw(st.booleans())}
        for prompt in keys["prompt"]
        if draw(st.booleans())
    }
    return ProxyFile.model_validate(file)


@st.composite
def cases(draw: st.DrawFn) -> tuple[Catalog, ProxyFile]:
    catalog = draw(catalogs())
    return catalog, draw(proxy_files(catalog))


@given(cases())
def test_exposed_names_are_unique_and_map_back_to_every_visible_catalog_item(
    case: tuple[Catalog, ProxyFile],
) -> None:
    catalog, proxy = case
    exposed = _exposed_or_reason(catalog, proxy)
    if isinstance(exposed, str):
        reasons = ("would both be exposed as", "needs a default", "the parameters must stay")
        assert any(reason in exposed for reason in reasons)
        return

    for kind in KINDS:
        visible = {name for name in catalog.items(kind) if not _hidden(proxy, kind, name)}
        origins = {exposed.origin(Item(kind, name)) for name in exposed.catalog.items(kind)}
        assert origins == visible
        assert len(exposed.catalog.items(kind)) == len(visible)
    uris = set(exposed.catalog.resources) & set(exposed.catalog.resource_templates)
    assert uris == set()

    for name, definition in exposed.catalog.tools.items():
        origin = catalog.tools[exposed.origin(Item("tool", name))]
        before: dict[str, Any] = origin["inputSchema"]["properties"]
        after: dict[str, Any] = definition["inputSchema"]["properties"]
        arguments = exposed.arguments.get(name)
        renamed = arguments.renamed if arguments else {}
        for exposed_argument, schema in after.items():
            assert schema["type"] == before[renamed.get(exposed_argument, exposed_argument)]["type"]
        required = definition["inputSchema"].get("required", [])
        assert set(required) <= set(after)
        mapped = (
            arguments.to_catalog(dict.fromkeys(after, 1)) if arguments else dict.fromkeys(after, 1)
        )
        assert set(mapped) <= set(before)
        for still_required in origin["inputSchema"].get("required", []):
            supplied = still_required in mapped
            assert supplied, f"{name}: nothing supplies {still_required!r}"


def _exposed_or_reason(catalog: Catalog, proxy: ProxyFile) -> Exposed | str:
    """What the Proxy exposes, or the one-line reason it refused to."""
    try:
        return expose(catalog, proxy)
    except OverrideError as refused:
        return str(refused)


def _capped_or_reason(
    exposed: Exposed, caps: CapSettings, proxy: ProxyFile, *, over_cap: bool
) -> Exposed | str:
    """What Caps do to ``exposed``, or the one-line reason it refused to.

    ``tool_cap_overrides`` never generates a value above what it inherits, so a ``CapError``
    here is only ever the ``over_cap`` case a Virtual Tool named over the tool name Cap raises;
    anything else would mean the property's own setup is wrong, not something ``cap`` should
    refuse.
    """
    try:
        return cap(exposed, caps, proxy)
    except OverrideError as refused:
        return str(refused)
    except CapError as raised:
        if not over_cap:
            pytest.fail(f"a Cap that only ever lowers should never raise: {raised}")
        return str(raised)


def _hidden(proxy: ProxyFile, kind: str, name: str) -> bool:
    override = proxy.overrides(kind).get(name)  # type: ignore[arg-type]
    return override is not None and override.hidden


# --- Caps: cutting what expose() built keeps names unique and every length within its Cap ------
#
# Ticket #7's rule for a name Cap versus uniqueness: a name Cap never touches a name it did not
# have to cut, so two names `expose` already told apart stay apart; a name a Cap does cut is
# told apart from another cut the same way by how much longer a marker it carries, and only
# when even that runs out of room is the collision refused, exactly like an Override collision.
# Generating tool names that share a long common prefix (below) is what forces that path.

_small = st.integers(min_value=1, max_value=6)
"""A tool name Cap small enough that generated names collide once cut."""
_modest = st.integers(min_value=1, max_value=20)


@st.composite
def cap_settings(draw: st.DrawFn) -> CapSettings:
    return CapSettings(
        tool_name=draw(_small),
        tool_description=draw(_modest),
        argument_description=draw(_modest),
        instructions=draw(_modest),
        tool_output=draw(_modest),
    )


@st.composite
def tool_cap_overrides(draw: st.DrawFn, base: CapSettings) -> ToolCapOverrides:
    """A tool's own Caps: unset, or lower than ``base``, so applying them never raises."""

    def maybe(ceiling: int) -> int | None:
        return draw(st.none() | st.integers(min_value=1, max_value=ceiling))

    return ToolCapOverrides(
        name=maybe(base.tool_name),
        description=maybe(base.tool_description),
        argument_description=maybe(base.argument_description),
        output=maybe(base.tool_output),
    )


def _fn() -> None:
    """A trivial function: only its name and description matter to a Virtual Tool's Cap."""


virtual_tool_names = st.from_regex(r"[d-f][a-z0-9_]{0,9}", fullmatch=True)
"""Lengths 1 to 10, straddling the 1-to-6 range ``cap_settings`` draws a tool name Cap from, so
generated Virtual Tool names land both under and over it. Prefixed away from ``names`` (which
starts ``a``-``c``) only to keep the strategies easy to read apart; a collision with a Catalog
tool name is still possible and is left to ``expose`` to refuse, as the design calls for."""


@st.composite
def virtual_tools(draw: st.DrawFn) -> UserCode:
    drawn = draw(st.lists(virtual_tool_names, max_size=3, unique=True))
    return UserCode(tools={name: VirtualTool(name, _fn, "d") for name in drawn})


@st.composite
def capped_cases(draw: st.DrawFn) -> tuple[Catalog, ProxyFile, CapSettings, UserCode]:
    catalog, proxy = draw(cases())
    caps = draw(cap_settings())
    capped_tools = {
        name: override.model_copy(update={"caps": draw(tool_cap_overrides(caps))})
        for name, override in proxy.tools.items()
    }
    proxy = proxy.model_copy(update={"tools": capped_tools})
    code = draw(virtual_tools())
    return catalog, proxy, caps, code


@given(capped_cases())
def test_capping_keeps_names_unique_every_length_within_its_cap_and_origins_intact(
    case: tuple[Catalog, ProxyFile, CapSettings, UserCode],
) -> None:
    catalog, proxy, caps, code = case
    try:
        exposed = expose(catalog, proxy, code)
    except OverrideError:
        return  # the Override property already covers why this refuses
    over_cap = any(len(virtual.name) > caps.tool_name for virtual in code.tools.values())
    capped = _capped_or_reason(exposed, caps, proxy, over_cap=over_cap)
    if isinstance(capped, str):
        if over_cap:
            assert "Virtual Tool" in capped
        else:
            # only a Cap-caused name collision that no marker could tell apart may refuse here.
            assert "cannot be cut to a name unique" in capped
        return
    assert not over_cap, (
        "a Virtual Tool over the tool name Cap should have refused with CapError, not exposed"
    )

    _assert_names_unique_and_origins_intact(catalog, proxy, capped)
    if capped.catalog.instructions is not None:
        assert len(capped.catalog.instructions) <= caps.instructions
    for name, definition in capped.catalog.tools.items():
        origin_name = capped.origin(Item("tool", name))
        override = proxy.tools.get(origin_name)
        effective = override.caps.over(caps, "tool") if override is not None else caps
        _assert_tool_within_its_caps(name, definition, effective)
    _assert_virtual_tools_exposed_intact(capped)


def _assert_names_unique_and_origins_intact(
    catalog: Catalog, proxy: ProxyFile, capped: Exposed
) -> None:
    for kind in KINDS:
        kind_names = list(capped.catalog.items(kind))
        assert len(kind_names) == len(set(kind_names))
        visible = {name for name in catalog.items(kind) if not _hidden(proxy, kind, name)}
        origins = {capped.origin(Item(kind, name)) for name in kind_names}
        assert origins == visible
    tool_names = capped.tool_names()  # Catalog tools and Virtual Tools together
    assert len(tool_names) == len(set(tool_names))


def _assert_virtual_tools_exposed_intact(capped: Exposed) -> None:
    """Every Virtual Tool is exposed under exactly its own name, uncut."""
    for name, virtual in capped.code.tools.items():
        assert virtual.name == name


def _assert_tool_within_its_caps(
    name: str, definition: dict[str, Any], effective: CapSettings
) -> None:
    assert len(name) <= effective.tool_name
    description = definition.get("description")
    if description is not None:
        assert len(str(description)) <= effective.tool_description
    properties = definition.get("inputSchema", {}).get("properties", {})
    for property_schema in properties.values():
        argument_description = property_schema.get("description")
        if argument_description is not None:
            assert len(str(argument_description)) <= effective.argument_description
