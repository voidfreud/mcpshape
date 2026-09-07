"""What one Proxy exposes: the Upstream's accepted Catalog, curated by the Proxy file.

Overrides are applied here, with no FastMCP in sight, so the result can be checked by
``doctor``, shown by the CLI, and property-tested. Identity stays the Catalog name: every
exposed item remembers where it came from, and every curated tool remembers how its exposed
arguments map back onto the Catalog tool's, so a call under the exposed name reaches the
Upstream under the Catalog name with the arguments it expects. Schema types are never changed.
"""

from __future__ import annotations

import copy
import itertools
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from mcpshape import catalog as catalogs
from mcpshape.catalog import KINDS, Item
from mcpshape.config import ArgumentOverride, PromptOverride, ResourceOverride, ToolOverride
from mcpshape.hooks import UserCode

if TYPE_CHECKING:
    from mcpshape.catalog import Catalog, Kind
    from mcpshape.config import ItemOverride, ProxyFile
    from mcpshape.model import CapSettings

TEMPLATE_PARAMETER = re.compile(r"\{([^}]*)\}")


class OverrideError(ValueError):
    """An Override that cannot be applied: a collision, or a hidden argument nothing supplies."""


@dataclass(frozen=True)
class ArgumentMap:
    """How a curated tool's exposed arguments map back onto the Catalog tool's."""

    renamed: dict[str, str] = field(default_factory=dict[str, str])
    """Exposed argument name to Catalog argument name, for the arguments that were renamed."""
    defaults: dict[str, Any] = field(default_factory=dict[str, Any])
    """Catalog argument name to the value sent when the Client gives none, hidden ones included."""

    def to_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The arguments the Catalog tool receives for ``arguments`` given under exposed names."""
        mapped = {self.renamed.get(name, name): value for name, value in arguments.items()}
        return {**self.defaults, **mapped}


@dataclass(frozen=True)
class Exposed:
    """Everything a Proxy exposes, under exposed names, and the way back to Catalog names."""

    catalog: Catalog
    """The exposed items, keyed by exposed name, with their curated definitions."""
    name: str | None
    """The exposed server name, or ``None`` for mcpshape's default."""
    origins: dict[Item, str] = field(default_factory=dict[Item, str])
    """Each exposed item to the Catalog name it stands for."""
    arguments: dict[str, ArgumentMap] = field(default_factory=dict[str, ArgumentMap])
    """By exposed tool name, for the tools whose arguments were curated."""
    code: UserCode = field(default_factory=UserCode)
    """The Hooks that run on calls, and the Virtual Tools exposed beside the Catalog's."""
    output_caps: dict[str, int] = field(default_factory=dict[str, int])
    """By exposed tool name: the tool output Cap a call through it answers under."""

    def origin(self, item: Item) -> str:
        return self.origins.get(item, item.name)

    def tool_names(self) -> list[str]:
        """Every exposed tool name: the Catalog's, curated, then the Virtual Tools."""
        return [*self.catalog.tools, *self.code.tools]


def expose(catalog: Catalog, proxy: ProxyFile, code: UserCode | None = None) -> Exposed:
    """Apply the Proxy file's Overrides to ``catalog``, then add ``code``'s Virtual Tools.

    Raises ``OverrideError`` when it cannot: a collision, a hidden argument nothing supplies,
    a Virtual Tool under a name a Catalog tool is exposed as.
    """
    exposed = catalog.model_copy(deep=True)
    exposed.instructions = (
        proxy.instructions if proxy.instructions is not None else catalog.instructions
    )
    origins: dict[Item, str] = {}
    arguments: dict[str, ArgumentMap] = {}
    for kind in KINDS:
        items = exposed.items(kind)
        items.clear()
        for name, definition in catalog.items(kind).items():
            override = proxy.overrides(kind).get(name)
            if override is not None and override.hidden:
                continue
            curated = copy.deepcopy(definition)
            exposed_name = _curate(kind, name, curated, override, arguments)
            _claim(exposed, kind, exposed_name, name, origins)
            items[exposed_name] = curated
    code = code or UserCode()
    for virtual in code.tools:
        if virtual in exposed.tools:
            taken = origins[Item("tool", virtual)]
            msg = (
                f"Virtual Tool {virtual!r} and {Item('tool', taken)} would both be exposed as "
                f"{virtual!r}; rename or hide one of them"
            )
            raise OverrideError(msg)
    return Exposed(
        catalog=exposed, name=proxy.name, origins=origins, arguments=arguments, code=code
    )


def _claim(
    exposed: Catalog, kind: Kind, exposed_name: str, name: str, origins: dict[Item, str]
) -> None:
    """Take ``exposed_name`` for ``name``; resources and templates share the URI space."""
    rivals = ("resource", "resource_template") if kind.startswith("resource") else (kind,)
    for rival in rivals:
        if exposed_name in exposed.items(rival):
            taken = origins[Item(rival, exposed_name)]
            msg = (
                f"{Item(kind, name)} and {Item(rival, taken)} would both be exposed as "
                f"{exposed_name!r}; rename or hide one of them"
            )
            raise OverrideError(msg)
    origins[Item(kind, exposed_name)] = name


def _curate(
    kind: Kind,
    name: str,
    definition: dict[str, Any],
    override: ItemOverride | None,
    arguments: dict[str, ArgumentMap],
) -> str:
    """Apply ``override`` to ``definition`` in place and return the exposed name."""
    if override is None:
        return name
    _replace(definition, override, "description")
    match override:
        case ToolOverride():
            _replace(definition, override, "title")
            _set_annotations(definition, override)
            argument_map = _curate_arguments(name, definition, override)
            exposed_name = override.name or name
            if argument_map.renamed or argument_map.defaults:
                arguments[exposed_name] = argument_map
            return exposed_name
        case ResourceOverride():
            _replace(definition, override, "name")
            if override.uri is None:
                return name
            if kind == "resource_template":
                _check_parameters(name, override.uri)
            return override.uri
        case PromptOverride():
            return override.name or name


def _replace(
    definition: dict[str, Any], override: ItemOverride | ArgumentOverride, *keys: str
) -> None:
    """Copy each of ``keys`` the user set onto the raw definition."""
    for key in keys:
        value: object = getattr(override, key)
        if value is not None:
            definition[key] = value


def _set_annotations(definition: dict[str, Any], override: ToolOverride) -> None:
    if override.annotations is None:
        return
    current: dict[str, Any] = dict(definition.get("annotations") or {})
    definition["annotations"] = {**current, **override.annotations.as_mcp()}


def _curate_arguments(tool: str, definition: dict[str, Any], override: ToolOverride) -> ArgumentMap:
    """Apply the argument Overrides that name an argument the schema has; the rest are orphans."""
    schema: dict[str, Any] = definition.setdefault("inputSchema", {"type": "object"})
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])
    renamed: dict[str, str] = {}
    defaults: dict[str, Any] = {}
    curated: dict[str, Any] = {}
    for name, property_schema in properties.items():
        argument = override.args.get(name)
        if argument is None:
            curated[name] = property_schema
            continue
        if argument.default is not None:
            defaults[name] = argument.default
        if argument.hidden:
            _check_hideable(tool, name, argument, required)
            required = [item for item in required if item != name]
            continue
        exposed_name = argument.name or name
        if exposed_name != name:
            renamed[exposed_name] = name
        curated[exposed_name] = _curate_property(property_schema, argument)
        required = _required(required, name, exposed_name, flag=argument.required)
    if "properties" in schema or curated:
        schema["properties"] = curated
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return ArgumentMap(renamed=renamed, defaults=defaults)


def _check_hideable(tool: str, name: str, argument: ArgumentOverride, required: list[str]) -> None:
    if name in required and argument.default is None:
        msg = (
            f"tool {tool}: argument {name!r} is required by the Upstream, so hiding it needs a "
            "default to send in its place"
        )
        raise OverrideError(msg)


def _required(required: list[str], name: str, exposed_name: str, *, flag: bool | None) -> list[str]:
    """The required list under the exposed name, with the user's say on whether it belongs."""
    kept = [exposed_name if item == name else item for item in required]
    if flag is True and exposed_name not in kept:
        kept.append(exposed_name)
    if flag is False:
        kept = [item for item in kept if item != exposed_name]
    return kept


def _curate_property(property_schema: dict[str, Any], argument: ArgumentOverride) -> dict[str, Any]:
    curated = dict(property_schema)
    _replace(curated, argument, "description", "default")
    return curated


def _check_parameters(name: str, uri: str) -> None:
    before, after = TEMPLATE_PARAMETER.findall(name), TEMPLATE_PARAMETER.findall(uri)
    if sorted(before) != sorted(after):
        msg = (
            f"resource template {name} cannot be exposed as {uri}: the parameters must stay "
            f"{', '.join('{' + p + '}' for p in before) or 'none'}"
        )
        raise OverrideError(msg)


# --- Caps: cut what expose() built down to what a Cap allows, second and separately -------------
#
# A Cap is pure work over what expose() already decided to show: nothing here hides, renames by
# choice, or re-describes anything an Override did not already touch, it only shortens. Only a
# tool's own name, description, argument descriptions, and output may be cut; a Proxy's
# instructions are cut too, but at the Proxy level, since a tool has none of its own.

MARKER = "…"
"""What a name, description, or the instructions end with once a Cap cuts them."""


def cap(exposed: Exposed, caps: CapSettings, proxy: ProxyFile) -> Exposed:
    """Cut ``exposed`` down to ``caps``, the Cap already resolved through global, Upstream, and
    Proxy. A tool's own Cap Override may lower ``caps`` further for its own name, description,
    argument descriptions, and output.

    Raises ``CapError`` when a tool's Cap tries to raise what it inherits, and ``OverrideError``
    when cutting a tool's name to its Cap cannot keep every exposed name unique, exactly as
    ``expose`` refuses a collision Overrides caused.
    """
    tools: dict[str, dict[str, Any]] = {}
    origins = dict(exposed.origins)
    arguments = dict(exposed.arguments)
    output_caps: dict[str, int] = dict.fromkeys(exposed.code.tools, caps.tool_output)
    taken = set(exposed.code.tools)  # Virtual Tool names are fixed; claimed before any cutting
    for name, definition in exposed.catalog.tools.items():
        origin_name = exposed.origin(Item("tool", name))
        override = proxy.tools.get(origin_name)
        tool_caps = override.caps.over(caps, f"tool {origin_name}") if override else caps
        cut_name = _cut_unique(name, tool_caps.tool_name, taken)
        taken.add(cut_name)
        if cut_name != name:
            origins[Item("tool", cut_name)] = origins.pop(Item("tool", name))
            if name in arguments:
                arguments[cut_name] = arguments.pop(name)
        tools[cut_name] = _cap_tool(definition, tool_caps)
        output_caps[cut_name] = tool_caps.tool_output
    instructions = exposed.catalog.instructions
    if instructions is not None:
        instructions = _cut(instructions, caps.instructions)
    capped = exposed.catalog.model_copy(update={"tools": tools, "instructions": instructions})
    return Exposed(
        catalog=capped,
        name=exposed.name,
        origins=origins,
        arguments=arguments,
        code=exposed.code,
        output_caps=output_caps,
    )


def _cap_tool(definition: dict[str, Any], caps: CapSettings) -> dict[str, Any]:
    """``definition`` with its description and its arguments' descriptions cut to ``caps``."""
    curated = dict(definition)
    if (description := curated.get("description")) is not None:
        curated["description"] = _cut(str(description), caps.tool_description)
    schema: object = curated.get("inputSchema")
    properties: object = (
        cast("dict[str, Any]", schema).get("properties") if isinstance(schema, dict) else None
    )
    if isinstance(schema, dict) and isinstance(properties, dict):
        typed_schema = cast("dict[str, Any]", schema)
        typed_properties = cast("dict[str, dict[str, Any]]", properties)
        curated["inputSchema"] = {
            **typed_schema,
            "properties": {
                argument: _cap_argument(property_schema, caps.argument_description)
                for argument, property_schema in typed_properties.items()
            },
        }
    return curated


def _cap_argument(property_schema: dict[str, Any], ceiling: int) -> dict[str, Any]:
    if (description := property_schema.get("description")) is None:
        return property_schema
    return {**property_schema, "description": _cut(str(description), ceiling)}


def _cut(text: str, ceiling: int) -> str:
    """``text`` as is when it already fits ``ceiling``; cut with ``MARKER`` at the end otherwise."""
    if len(text) <= ceiling:
        return text
    if ceiling <= len(MARKER):
        return MARKER[:ceiling]
    return text[: ceiling - len(MARKER)] + MARKER


def _cut_unique(text: str, ceiling: int, taken: set[str]) -> str:
    """``_cut(text, ceiling)``, extending the marker rather than colliding with ``taken``.

    A name a Cap did not have to touch is never renamed here, so two names ``expose`` already
    kept apart stay apart; only names a Cap cuts to the same result are told apart, by how much
    longer a marker each carries. When even that runs out of room, the collision is refused
    like any other Override collision: two different tools cannot share one exposed name.
    """
    cut = _cut(text, ceiling)
    if cut not in taken:
        return cut
    if len(text) <= ceiling:
        msg = (
            f"tool {text!r} would be exposed as {cut!r}, the same as another tool a Cap cut to "
            "it; rename or hide one of them"
        )
        raise OverrideError(msg)
    for number in itertools.count(2):
        suffix = f"{MARKER}{number}"
        if ceiling <= len(suffix):
            msg = (
                f"tool {text!r} cannot be cut to a name unique under a Cap of {ceiling} "
                f"characters; every name that short is already taken"
            )
            raise OverrideError(msg)
        candidate = text[: ceiling - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise AssertionError  # pragma: no cover  # itertools.count never stops on its own


def cut_output(text: str, ceiling: int) -> str:
    """``text`` as is when it already fits ``ceiling``; otherwise cut, with a note at the end
    saying how many characters were cut, instead of ``MARKER``: the model is told a number, not
    shown a mark it has no context for.

    The note counts what the model does not see, itself included in what fits under the Cap.
    A Cap with no room for the note at all still gets the note, on its own: then the model is
    told that everything was cut, which is the one thing it must know.
    """
    if len(text) <= ceiling:
        return text
    kept = ceiling
    while True:
        note = f" [{len(text) - kept} characters cut]"
        if kept + len(note) <= ceiling or kept == 0:
            return text[:kept] + note
        kept = max(0, ceiling - len(note))


# --- what doctor reports -----------------------------------------------------------------------


def orphaned_overrides(catalog: Catalog, proxy: ProxyFile) -> list[Item]:
    """Overrides whose Catalog item is gone. Kept, so a returning item keeps its curation."""
    return [
        Item(kind, name)
        for kind in ("tool", "resource", "prompt")
        for name in proxy.overrides(kind)
        if name not in catalog.items(kind)
        and (kind != "resource" or name not in catalog.resource_templates)
    ]


def orphaned_hooks(catalog: Catalog, code: UserCode) -> list[Item]:
    """Hooks naming an item the Catalog lacks. They never run until it appears."""
    return sorted(
        item
        for item in code.hooked()
        if item.name not in catalog.items(item.kind)
        and (item.kind != "resource" or item.name not in catalog.resource_templates)
    )


def orphaned_arguments(catalog: Catalog, proxy: ProxyFile) -> list[tuple[str, str]]:
    """Argument Overrides naming an argument the Catalog tool lacks, as ``(tool, argument)``."""
    return [
        (tool, argument)
        for tool, override in proxy.tools.items()
        if tool in catalog.tools
        for argument in override.args
        if argument not in catalogs.arguments(catalog.tools[tool])
    ]
