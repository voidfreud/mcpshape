# mcpshape

A local proxy that reshapes what MCP servers expose. The user guide is `docs/guide.md`;
the design brief is `docs/DESIGN.md`.

## Develop

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```
