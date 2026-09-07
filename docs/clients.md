# Client and FastMCP facts

Dated facts about Clients and about FastMCP that mcpshape's decisions rest on. This file is
expected to change without touching the design brief or the ADRs. Client entries are the seed
data for Client Profiles. Checked 2026-09-06/07 against primary documentation.

## Clients

### Transports
- Streamable HTTP accepted by: Claude Code, Claude Desktop (Connectors UI only, not its config
  file), Cursor, Windsurf, VS Code, Gemini CLI, Codex CLI, Goose, JetBrains, Cline, Continue,
  OpenCode.
- stdio only: Zed. Claude Desktop's config file. Both would need a stdio bridge to reach an HTTP server.
- Remote HTTPS only: ChatGPT.

### Behavior
- Live refresh on `tools/list_changed` is documented only for Claude Code. Other Clients need
  a reconnect to see a changed tool set.
- Clients do not detect an Upstream behind a Proxy going down and coming back. Claude Code
  needs a manual reconnect and re-auth to notice server-side changes.
- Native per-tool enable/disable: Windsurf, VS Code, ChatGPT. Not in Claude Code, Cursor,
  Claude Desktop, Gemini CLI, Codex. None rename or rewrite.
- Per-tool allow/deny in some form: every Client except Zed and Claude Desktop.
- Claude Code defers MCP tool definitions by default and searches on demand; the model sees
  tool names plus server instructions until it loads a tool. Only escape: `alwaysLoad: true`
  per server in its config. No per-tool flag exists in any Client or in the MCP spec.

### Verified limits
- Anthropic API: tool names must match `^[a-zA-Z0-9_-]{1,64}$`.
- Claude Code: presents tools as `mcp__<server>__<tool>`, leaving 57 characters for server
  plus tool name; longer names fail the whole request. Property names: 1-64 chars of letters,
  digits, `_`, `.`, `-`.
- Claude Code: "truncates tool descriptions and server instructions at 2KB each. Keep them
  concise to avoid truncation, and put critical details near the start."
  (code.claude.com/docs/en/mcp). Whether 2KB is 2048 characters or bytes, and whether it is
  per server or a shared pool (anthropics/claude-code#43474 suggests a pool), is untested.
- Claude Code output: warning at 10,000 tokens, default max 25,000 (`MAX_MCP_OUTPUT_TOKENS`),
  per-tool override `_meta["anthropic/maxResultSizeChars"]` up to 500,000 chars.
- Claude Code: idle timeouts 5 min HTTP, 30 min stdio. Calls over 2 min are backgrounded.
  Mid-session drops retry with backoff up to 5 times. Permission rules are regex style,
  `mcp__<server>__.*`.
- Gemini CLI: names tools `mcp_<server>_<tool>`, replaces invalid chars, middle-truncates
  names over 63 chars. Per-server timeout default 10 min. `includeTools`/`excludeTools`.
- Codex CLI: startup timeout 10 s, tool timeout 60 s, per-tool output token limit,
  recommends the first 512 chars of instructions be self-contained.
- Windsurf: 100 visible tools total, 20 calls per prompt. Cline: 60 s tool timeout.
  OpenCode: 5 s tool-fetch timeout. Goose: 300 s per extension.
- Issue-tracker lore about per-Client tool caps (Cursor, VS Code, claude.ai) and a Claude
  Code connect timeout was checked and not substantiated by any primary source.

### Config locations
- Claude Code: `.mcp.json` (project) or `~/.claude.json` (user); strict JSON, no comments.
- Claude Desktop: `claude_desktop_config.json`, stdio entries only.
- Cursor: `~/.cursor/mcp.json` or `.cursor/mcp.json`.
- Windsurf: `~/.codeium/windsurf/mcp_config.json`.
- VS Code: `.vscode/mcp.json` or user profile.
- Zed: `settings.json`, `context_servers` key.
- Gemini CLI: `settings.json`, `mcpServers` key.
- Codex CLI: `~/.codex/config.toml`, `[mcp_servers.<name>]`.
- Goose: YAML `extensions:` block. Cline: `cline_mcp_settings.json`.
  Continue: `config.yaml`. OpenCode: `opencode.json`.

## FastMCP 4.0.3
- Requires Python 3.10+. Ships with `httpx2` (the `httpx` 2.x package name) and `mcp` 2.x. Public API changed 2.x to 3.0 (Feb 2026) and 3.0 to 4.0 (Aug 2026).
- `create_proxy` accepts URL, path, or `mcpServers` dict; forwards tools, resources, prompts,
  logging, progress. Caches upstream lists with a 300 s default TTL.
- `ToolTransform` renames, re-describes, tags, hides or renames arguments. No output-schema
  rewriting. `enable`/`disable(only=True)` give allowlists and emit `tools/list_changed`.
- `Middleware` hooks: `on_call_tool`, `on_list_tools`, `on_read_resource`, `on_get_prompt`, etc.
- Tool Search transform: server-side deferral behind search and call meta-tools, with an
  `always_visible` list.
- Multiple servers in one process: mount each `http_app()` into one Starlette app. Each
  `http_app()` has its own lifespan that must run, so the parent app composes them.
- In-process testing: `fastmcp.utilities.asgi_transport.StreamingASGITransport` drives an ASGI
  app from an `httpx2.AsyncClient`, streaming responses, so a FastMCP `Client` can reach
  `http_app()` with no sockets via `httpx_client_factory`. The factory is called with
  `follow_redirects` on top of what `McpHttpClientFactory` declares.
- Client transports: stdio, Streamable HTTP, SSE, in-memory. OAuth client built in; tokens
  in-memory by default, persistence must be supplied.
- Protocol is sessionless by default in 4.x; server-initiated sampling and roots are removed,
  elicitation is gated.
