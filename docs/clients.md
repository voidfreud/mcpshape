# Client and FastMCP facts

Dated facts about Clients and about FastMCP that mcpshape's decisions rest on. This file is
expected to change without touching the design brief or the ADRs. Client entries are the seed
data for Client Profiles. Checked 2026-09-06/07 against primary documentation.

## Clients

### Transports
- Streamable HTTP accepted by: Claude Code, Claude Desktop (Connectors UI only, not its config
  file), Cursor, Windsurf, VS Code, Gemini CLI, Codex CLI, Goose, JetBrains, Cline, Continue,
  OpenCode.
- stdio only: Zed. Claude Desktop's config file. Both need the Shim to reach an HTTP server.
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
- Checked 2026-09-08, primary docs, for ticket #23:
  - Claude Code's `local` scope nests per-project servers in the same `~/.claude.json` as the
    `user` scope, under `projects.<absolute project dir>.mcpServers`, one map per project
    directory, sibling to the top-level `mcpServers` the `user` scope writes.
    (code.claude.com/docs/en/mcp)
  - Claude Desktop for Linux exists as a beta (`code.claude.com/docs/en/desktop-linux`,
    apt-installed on Ubuntu/Debian), but no primary source (that page, the install and MCP
    help-center articles, or the enterprise-configuration article) documents a config file
    path for `claude_desktop_config.json` on Linux; only macOS's path is documented. Not added
    to the Claude Desktop Profile.
  - Goose and Continue stay YAML-only: mcpshape carries no YAML dependency for a listing, so
    their files are reported as found and unread, by name (decision recorded in each Profile's
    `notes`).
  - `discovery.py`'s `list` entry shape (`Profile.entry_shape = "list"`) is unreachable from
    `upstream scan`: the only list-shaped Client, Continue, is YAML and so is never parsed.
    Left as-is; it becomes reachable the day a JSON- or TOML-shaped list Client is added.

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
- What a sync Hook or Virtual Tool rests on (checked 2026-09-08, 4.0.3 with anyio 4.15.1).
  `FunctionTool.from_function(run_in_thread=...)` defaults to `True`, so a sync body runs in a
  worker thread and only `run_in_thread=False` runs it inline on the event loop's thread.
  FastMCP dispatches it through `fastmcp.utilities.async_utils.call_sync_fn_in_threadpool`,
  which is `anyio.to_thread.run_sync`; anyio 4 copies the caller's context into the worker
  thread, so a `ContextVar` set around the call is readable there. `asyncio.to_thread`, which
  mcpshape runs sync Hooks with, copies the context the same way. A thread reached either way
  has no running loop of its own, which is how the `upstream` handle tells a worker thread
  from the loop; it hands the coroutine to the loop `bound` captured with
  `asyncio.run_coroutine_threadsafe` and blocks on the result, and whatever the coroutine
  raises is raised again in the thread.
- What the Client does when structured content does not fit the output schema it was
  advertised (checked 2026-09-08, 4.0.3 with `mcp` 2.x). `ClientSession._validate_tool_result`
  (`mcp/client/session.py`) validates with `jsonschema` and raises
  `RuntimeError(f"Invalid structured content returned by tool {name}: {error}")`, `error` being
  the `jsonschema` validation failure, for structured content of the wrong shape; missing
  structured content on a tool with an output schema is the separate, already-pinned
  `"...did not return structured content"` `RuntimeError`. Both are the Client's own check,
  ahead of the caller ever seeing the result, which is why a Hook's mismatched result has to be
  turned into a tool error before it reaches the Client (#25).
- What an stdio Upstream's child process gets (checked 2026-09-08, 4.0.3 with `mcp` 2.x). The
  SDK spawns it with `get_default_environment() | transport.env`, and that default is only
  HOME, LOGNAME, PATH, SHELL, TERM and USER. Nothing else of the Daemon's environment reaches
  the child, so anything a server needs (`PYTHONPATH`, a token, a proxy setting) has to stand
  in the Upstream file's `env` block, where a value may be a `${VAR}` reference.
- `StdioTransport.keep_alive` defaults to `True` and leaves the child process running after
  the client's context exits, to be reused by the next connection (checked 2026-09-08, 4.0.3).
  mcpshape passes `keep_alive=False`: the Upstream's own lifecycle decides when a connection
  is let go, and a kept-alive child would outlive the connection nobody comes back for.
- A `Client` whose URL nothing answers raises `RuntimeError("Client failed to connect: ...")`
  out of `__aenter__`; it does not hand back a client that fails per call. `Client.close()` on
  one that never connected is harmless, which is what lets a connect cancelled by the connect
  timeout be cleaned up by a callback registered before the connect starts (checked
  2026-09-08, 4.0.3).
- The SDK hands the child process `sys.stderr` as its error log, so spawning one under Click's
  `CliRunner`, whose stdout and stderr have no `fileno`, fails with `Client failed to connect:
  fileno` (checked 2026-09-08). A stdio Upstream is therefore scanned through the Daemon in
  tests, not through Typer's runner.
- What an OAuth Upstream rests on (checked 2026-09-08, 4.0.3 with `mcp` 2.1.1). Sources:
  `fastmcp/client/auth/oauth.py`, `mcp/client/auth/oauth2.py`, `mcp/client/auth/utils.py`,
  `mcp/shared/auth.py`; pinned in `tests/test_fastmcp_contract.py`.
  - `fastmcp.client.auth.OAuth` always opens a browser (`webbrowser.open` in its own
    `redirect_handler`) and always runs a loopback uvicorn callback server; neither handler is
    a constructor parameter, and its `token_storage` is an `AsyncKeyValue`, not the SDK's
    `TokenStorage`. mcpshape therefore builds `mcp.client.auth.OAuthClientProvider` itself,
    which does take `redirect_handler`, `callback_handler`, and a `TokenStorage`.
  - `TokenStorage` is a four-method async protocol: `get_tokens`, `set_tokens`,
    `get_client_info`, `set_client_info`, over `OAuthToken` and `OAuthClientInformationFull`.
    Both are pydantic models that round-trip through `model_dump(mode="json")`; a plain
    `model_dump()` leaves `AnyUrl` objects `json.dumps` refuses.
  - A `ClientTransport` given an `auth=` that is not FastMCP's own `OAuth` passes it to httpx
    as it stands: it is neither bound to the URL nor handed the transport's client factory.
  - `OAuthClientProvider._initialize` loads the stored token set but leaves
    `token_expiry_time` unset, so a token stored long ago would be sent once and rejected.
    mcpshape stores the moment a token dies and sets the expiry on load, as FastMCP's own
    `OAuth` does.
  - A refresh that fails raises nothing: the provider logs it, clears the token set, and falls
    through to the full authorization flow, which reaches the redirect handler. That is why an
    expired, non-refreshable token surfaces through a handler that refuses, not an exception.
  - Discovery starts from a 401 only, and in this order: the `WWW-Authenticate`
    `resource_metadata` URL, `/.well-known/oauth-protected-resource<path>`, then the root one;
    then the authorization server's `/.well-known/oauth-authorization-server`, with
    `openid-configuration` as the fallback. The metadata `issuer` must equal the discovered
    authorization server URL as a plain string.
  - Scope selection overwrites what the client asked for: the `WWW-Authenticate` scope, else
    the protected resource's `scopes_supported`, else the authorization server's. An
    Upstream's own `scopes` therefore only decide what the browser flow asks for where the
    provider advertises nothing, and device-code pairing, which mcpshape drives itself, uses
    them as written.
  - There is no device-code grant in FastMCP 4.0.3 or `mcp` 2.1.1, and no `FileTokenStorage`:
    the default store is in memory and warns. mcpshape speaks RFC 8628 to the provider itself,
    with the `httpx2` FastMCP already ships.
  - `fastmcp.client.oauth_callback.create_oauth_callback_server` serves `/callback` on a given
    port and fills an `OAuthCallbackResult` with the code, state, and `iss`, then sets the
    event it was handed; the event is only ever `.set()`, so an `asyncio.Event` does.
- What a call over a dead connection raises (checked 2026-09-08, 4.0.3 with `mcp` 2.x). A
  `call_tool_mcp`, `read_resource` or `get_prompt` on a session whose other end is gone raises
  `MCPError` with `mcp_types.CONNECTION_CLOSED` (-32000), whatever the transport: a Streamable
  HTTP server that stopped answering, an stdio child that exited. The SDK uses the same code
  for its own "SSE stream ended without a response". An error the Upstream itself answered is
  something else: a failing or unknown tool is an `isError` result, and a missing resource or
  prompt is an `MCPError` carrying that error's own JSON-RPC code (`INVALID_PARAMS` for a
  resource FastMCP does not have). After a `CONNECTION_CLOSED` the client reports
  `is_connected()` false, and closing it raises whatever the transport failed with, so the
  caller has to swallow that. A new client on the same URL reaches an Upstream that came back;
  the dead one cannot be reused (it fails with `nesting counter should be 0`).
- Killing a FastMCP HTTP server in-process while a legacy-era client still has a session open
  leaves every later FastMCP HTTP server in that process unable to serve the legacy era: each
  new connect ends with `SSE stream ended without a response`, from another process too, while
  a modern-era `Client` and an in-memory `ProxyClient` still work (checked 2026-09-08, 4.0.3).
  mcpshape's Upstream client is a `ProxyClient`, which is legacy-era, so a test that kills an
  HTTP Upstream serves it from a child process instead of in-process uvicorn.
