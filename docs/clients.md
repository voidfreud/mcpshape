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

### Config file shapes (checked 2026-09-07, primary docs)
What `proxy install` has to write. Where nothing is listed here, no primary source was found
and the Profile writes the common `mcpServers` + `{"type": "http", "url": ...}` form.
- VS Code: the top-level key of `.vscode/mcp.json` is `servers`, not `mcpServers`; an HTTP
  entry is `{"type": "http", "url": "..."}`.
  (code.visualstudio.com/docs/agents/reference/mcp-configuration)
- Zed: a `context_servers` entry needs only `command`/`args`/`env`; no `source` field is
  required. Its docs now also show a remote entry taking `url` (plus optional `headers`),
  which contradicts "stdio only" above. Unconfirmed against a second reading, so the Zed
  Profile still writes the shim entry; settle this before changing it.
  (zed.dev/docs/ai/mcp, and `docs/src/ai/mcp.md` in zed-industries/zed)
- OpenCode: the top-level key of `opencode.json` is `mcp`; a remote entry is
  `{"type": "remote", "url": "...", "enabled": true}`. (opencode.ai/docs/mcp-servers/)
- Continue: `mcpServers` in `config.yaml` is a **list** of objects, each carrying its own
  `name`; a remote entry is `name`, `type: streamable-http`, `url`. (docs.continue.dev)
- Goose: `extensions:` is a map keyed by extension name; a remote entry is
  `type: streamable_http`, `name`, `enabled: true`, and **`uri:`**, not `url`.
  (block/goose `documentation/docs/guides/config-files.md`)
- Codex CLI: a streamable-HTTP server under `[mcp_servers.<name>]` takes `url`, with optional
  `bearer_token_env_var` and `http_headers`. No extra flag is documented as required.
  (developers.openai.com/codex/mcp)
- Windsurf and Gemini CLI: their remote entry shapes are not documented in anything checked.
- Filled in from general knowledge, not from a primary source, so that `proxy install` has a
  default path: `~/.gemini/settings.json`, `~/.config/zed/settings.json`,
  `~/.config/goose/config.yaml`, `~/.continue/config.yaml`, and Claude Desktop's
  `~/Library/Application Support/Claude/claude_desktop_config.json`. Cline's
  `cline_mcp_settings.json` lives in editor storage, so its Profile carries no default path.

### Disabling a Client's own entry (checked 2026-09-07)
- Cline is the only Client that documents a per-server off switch inside its MCP config file:
  `"disabled": true`, a sibling of `command`/`args`/`url` in the server object.
  (docs.cline.bot/mcp/mcp-overview)
- Claude Code has `disabledMcpjsonServers` and `enabledMcpjsonServers`, arrays of server
  names, but they live in `.claude/settings.json`, not in the MCP config file.
  (code.claude.com/docs/en/settings-reference)
- Cursor, VS Code, and Windsurf: not substantiated. Cursor and VS Code tie enable/disable to
  a UI toggle whose state is stored outside the config file; Windsurf documents only an
  admin-level `disabledTools` array, which is per tool. Third-party posts claiming a
  `"disabled"` key for these three do not hold up against the vendors' own text.

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
- `ping` exists only in the legacy protocol era (checked 2026-09-07, 4.0.3). A plain `Client`
  over an in-memory server negotiates `2026-07-28` and answers `Method not found` to
  `client.ping()`; a `ProxyClient` negotiates `2025-11-25`, where `ping` answers. mcpshape
  checks a warm Upstream over its `ProxyClient`, so it gets the era that has it.
- A `Client` reference-counts its session: entering an already-entered client reuses the
  session and leaving that inner context keeps it open. That is what lets one Upstream
  connection be held open and borrowed by every call (checked 2026-09-07, 4.0.3).
- A client factory handed to `ProxyTool`/`ProxyResource`/`ProxyPrompt` may be async, and an
  exception it raises is handled like any tool failure: a `ToolError` reaches the caller as an
  `isError` result carrying its message, leaving the Client's own session untouched.
- No server-wide broadcast of `tools/list_changed` (checked 2026-09-07, 4.0.3). The only sender
  is the per-request `Context.send_notification`, which on the 2026-07-28 protocol rides the
  request's own stream. That protocol delivers list-changed events through
  `subscriptions/listen`, which FastMCP's low-level server does not register a handler for. So
  a Proxy whose exposed set changed can only answer the next `tools/list` with the new set; it
  cannot push the change to an idle Client.
- What Hooks and Virtual Tools rest on (checked 2026-09-07, 4.0.3). `FunctionTool.from_function`
  builds a subclass instance (`cls(...)`) from a plain function: name, docstring as description,
  input schema from the signature; `run_in_thread=False` runs a sync function inline. Its `run`
  routes the body's return through `convert_result`, so a subclass can accept its own result
  type. A FastMCP tool returning one value advertises an output schema marked
  `x-fastmcp-wrap-result` with the value under `result`, and the MCP client refuses a result
  for such a tool that carries no structured content, so a Hook that replaces a result with
  plain text needs that structured content rebuilt from the schema. `ProxyTemplate` reads the
  Upstream inside `create_resource` and hands back a `ProxyResource` whose `_cached_content`
  is served by `read()`. `ProxyPrompt.render` returns a `PromptResult` of `Message`s. A
  `call_tool_mcp` on the borrowed client answers a failing or unknown tool with an `isError`
  result carrying the message, not by raising.
