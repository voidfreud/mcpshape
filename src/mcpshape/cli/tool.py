"""``mcpshape tool``: hide, show, rename, describe, trim, cap.

Each verb writes one Override into the Proxy file, keyed by the tool's Catalog name, or by
the argument's Catalog name under it with ``--arg``. Resources and prompts are curated by
editing the Proxy file. The Daemon does not need to be up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

import click
import typer
from rich.markup import escape

from mcpshape import catalog, config
from mcpshape.cli.common import (
    HELP_OPTIONS,
    console,
    example,
    fail,
    parse_proxy_ref,
    reporting_errors,
    state,
)

if TYPE_CHECKING:
    from pathlib import Path

app = typer.Typer(
    help="Edit how a Proxy presents a tool: hide, show, rename, describe, trim, cap.",
    epilog=example("tool hide github/default create_gist"),
    context_settings=HELP_OPTIONS,
    no_args_is_help=True,
)

RefArg = Annotated[
    str, typer.Argument(metavar="UPSTREAM/PROXY", help="The Proxy, as <upstream>/<proxy>.")
]
ToolArg = Annotated[str, typer.Argument(metavar="TOOL", help="The tool, by its Catalog name.")]
ArgOpt = Annotated[
    str | None,
    typer.Option(
        "--arg",
        metavar="ARGUMENT",
        show_default=False,
        help="Edit this argument of the tool instead, by its Catalog name.",
    ),
]


@dataclass(frozen=True)
class Target:
    """The tool, or one argument of it, an edit applies to. Both by Catalog name."""

    tool: str
    argument: str | None = None

    def __str__(self) -> str:
        return f"{self.tool} --arg {self.argument}" if self.argument else self.tool


def target_file(ctx: typer.Context, ref: str, target: Target) -> Path:
    """The Proxy file to edit, after checking the Proxy exists and warning about unknown names."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    upstream, proxy = parse_proxy_ref(ref)
    config.load_proxy(config_dir, upstream, proxy)
    stored = catalog.load_catalog(state_dir, upstream)
    if stored is not None:
        if target.tool not in stored.tools:
            console.print(
                f"[yellow]![/] No tool {target.tool!r} in the Catalog of {upstream}; the "
                "Override is written anyway and applies if the tool appears"
            )
        elif target.argument is not None and target.argument not in _arguments(
            stored.tools[target.tool]
        ):
            console.print(
                f"[yellow]![/] Tool {target.tool!r} has no argument {target.argument!r} in the "
                "Catalog; the Override is written anyway and applies if the argument appears"
            )
    return config.proxy_file(config_dir, upstream, proxy)


def _arguments(definition: dict[str, Any]) -> list[str]:
    schema: object = definition.get("inputSchema")
    if not isinstance(schema, dict):
        return []
    properties: object = cast("dict[str, Any]", schema).get("properties")
    return list(cast("dict[str, Any]", properties)) if isinstance(properties, dict) else []


def write(ctx: typer.Context, ref: str, target: Target, key: str, value: object) -> None:
    """Write one Override key and say what changed."""
    with reporting_errors():
        path = target_file(ctx, ref, target)
        if target.argument is None:
            config.set_override(path, catalog.Item("tool", target.tool), key, value)
        else:
            config.set_argument_override(path, target.tool, target.argument, key, value)
    console.print(f"Set [bold]{key} = {escape(repr(value))}[/bold] on {target} in {path}")


@app.command("hide", epilog=example("tool hide github/default create_gist"))
def hide(ctx: typer.Context, ref: RefArg, tool: ToolArg, arg: ArgOpt = None) -> None:
    """Hide a tool, or one of its arguments, from Clients."""
    write(ctx, ref, Target(tool, arg), "hidden", True)  # noqa: FBT003  # the value being written


@app.command("show", epilog=example("tool show github/default create_gist"))
def show(ctx: typer.Context, ref: RefArg, tool: ToolArg, arg: ArgOpt = None) -> None:
    """Expose a hidden tool, or one of its arguments, again."""
    write(ctx, ref, Target(tool, arg), "hidden", False)  # noqa: FBT003  # the value being written


@app.command("rename", epilog=example("tool rename github/default create_issue new_issue"))
def rename(
    ctx: typer.Context,
    ref: RefArg,
    tool: ToolArg,
    name: Annotated[str, typer.Argument(metavar="NAME", help="The exposed name.")],
    arg: ArgOpt = None,
) -> None:
    """Expose a tool, or one of its arguments, under another name."""
    write(ctx, ref, Target(tool, arg), "name", name)


@app.command(
    "describe", epilog=example("tool describe github/default create_issue 'Open an issue.'")
)
def describe(
    ctx: typer.Context,
    ref: RefArg,
    tool: ToolArg,
    text: Annotated[str, typer.Argument(metavar="TEXT", help="The description Clients see.")],
    arg: ArgOpt = None,
) -> None:
    """Replace the description of a tool, or of one of its arguments."""
    write(ctx, ref, Target(tool, arg), "description", text)


@app.command("trim", epilog=example("tool trim github/default create_issue"))
def trim(ctx: typer.Context, ref: RefArg, tool: ToolArg) -> None:
    """Show a tool's original description and open it in $EDITOR; what you save replaces it."""
    config_dir, state_dir = state(ctx).config_dir, state(ctx).state_dir
    upstream, proxy = parse_proxy_ref(ref)
    with reporting_errors():
        config.load_proxy(config_dir, upstream, proxy)
        stored = catalog.load_catalog(state_dir, upstream)
    if stored is None:
        fail(f"no stored Catalog for {upstream}; run: mcpshape upstream sync {upstream}")
    if tool not in stored.tools:
        fail(f"no tool {tool!r} in the Catalog of {upstream}")
    original = str(stored.tools[tool].get("description") or "")
    console.print(f"Original description of [bold]{tool}[/bold] ({len(original)} characters):")
    console.print(escape(original) or "[dim](none)[/dim]")
    edited = click.edit(original, extension=".md")
    if edited is None or edited.strip() == original.strip():
        console.print("Unchanged; nothing written.")
        return
    write(ctx, ref, Target(tool), "description", edited.strip())


@app.command("cap", epilog=example("tool cap github/default create_issue --description 200"))
def cap(
    ctx: typer.Context,  # noqa: ARG001  # the signature of the command to come
    ref: RefArg,
    tool: ToolArg,
    description: Annotated[
        int | None, typer.Option("--description", metavar="CHARS", show_default=False)
    ] = None,
) -> None:
    """Lower the Caps one tool inherits."""
    fail(f"tool cap is not available in this version (asked for {ref} {tool} {description})")
