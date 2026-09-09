# mcpshape

Reshape what MCP servers expose before a model sees it.

An MCP server as shipped offers every tool it has, described at whatever length its author
chose, and most Clients take it whole or not at all. mcpshape is a local proxy between the
servers you use and the Clients that use them. You add a server once as an Upstream, and
mcpshape serves one or more Proxies for it, each a curated MCP server of its own: tools
hidden, renamed, re-described, capped, rewritten in Python, or added, before a model sees
them. Clients are pointed at the Proxy.

## Install

Python 3.12 or newer, macOS or Linux.

```
uv tool install mcpshape
```

From this repository instead, `uv tool install git+https://github.com/voidfreud/mcpshape`; from
a checkout, `uv tool install .`, and `uv sync` with `uv run mcpshape` runs it in place. mcpshape
makes no network call except to your Upstreams.

## First steps

```
mcpshape add github --stdio 'npx -y @modelcontextprotocol/server-github'
mcpshape upstream sync github
mcpshape tool hide github/default create_gist
mcpshape proxy install github/default --to claude-code
mcpshape daemon up
```

Every command explains itself with an example: `mcpshape --help`, and `--help` on any command.

How work lands is `CONTRIBUTING.md`.
