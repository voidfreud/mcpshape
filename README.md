# mcpshape

A local proxy that reshapes what MCP servers expose. See `docs/DESIGN.md`.

## Develop

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```
