"""Applying arbitrary Overrides to arbitrary Catalogs keeps names unique and identity intact.

At the proxy module on purpose (see CLAUDE.md): the property is about the pure application,
and driving thousands of generated Catalogs through in-memory Upstreams would prove nothing
more. Whatever the Overrides say, an exposed set has unique names per kind, maps every exposed
item back to exactly one Catalog item, never changes a schema type, and only refuses for the
two reasons it may: a name collision, or a hidden argument nothing would supply.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from mcpshape.catalog import KINDS, Catalog, Item
from mcpshape.config import ProxyFile
from mcpshape.proxy import Exposed, OverrideError, expose

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
            "uri": draw(st.none() | names.map(lambda n: f"t://{n}/{{id}}")),
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
        assert "would both be exposed as" in exposed or "needs a default" in exposed
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


def _hidden(proxy: ProxyFile, kind: str, name: str) -> bool:
    override = proxy.overrides(kind).get(name)  # type: ignore[arg-type]
    return override is not None and override.hidden
