# mcpshape design brief

What holds in mcpshape, why, and what was decided against. The words are `CONTEXT.md`'s.

## What it is

A local, per-user Daemon between MCP Clients and the MCP servers they use. An Upstream is
added once and gets one or more Proxies. A Proxy is a curated MCP server of its own at its
own local URL: tools hidden, renamed, re-described, capped, hooked, or added before a model
sees them. The user narrows what a model sees; mcpshape itself never does, and everything
FastMCP forwards, it forwards.

## What holds

### A Proxy serves one Upstream, and Upstreams are never merged

Nearly every tool of this kind merges the servers behind it into one server with namespaced
tool names, and that was the first idea here too. Clients expect one server per configured
entry: their enable, disable, permission, and trust controls are per server, and so are their
budgets. Claude Code shows the model tool names plus each server's instructions, truncated per
server, so N servers give N routing hints and one merged server gives one; it prefixes every
tool name with the server's inside a fixed name budget; and its permission rules are per
server and tool. Other Clients cap the tools visible in a session, and most budgets are
undocumented, so packing tools into one server cannot be reasoned about case by case.
Separate Proxies keep each one small, curated for one purpose, individually permissioned, and
individually failing: one dead Upstream takes down only its own Proxies, and the trust
boundary a Client sees maps one to one onto the Upstream behind it. Composing tools across
Upstreams is therefore not a feature, and cross-Proxy calls from user code are parked for the
same reason. The same Upstream may have several Proxies, for different Clients or purposes,
and every Upstream gets one named `default` when it is added.

### Unmodified FastMCP, behind the Adapter

FastMCP provides proxying, its own `ToolTransform`, visibility control, middleware, and
every transport needed, so writing an MCP implementation here would reinvent a maintained library.
Its public API changed twice in 2026, so anything bound to it directly is exposed to that
churn. mcpshape depends on FastMCP as a normal dependency pinned to a major, never forked or
patched. Only the Adapter imports it, which `tests/test_boundaries.py` enforces, and nothing
user-facing exposes a FastMCP type: not the Hook and Virtual Tool API, not a config file, not
the management API. A FastMCP behaviour the code relies on is pinned in
`tests/test_fastmcp_contract.py`; a docstring or a facts entry alone does not count. So a
feature FastMCP lacks, output-schema rewriting for one, is out of scope until it has it; a
FastMCP major is a deliberate, tested change to the Adapter, not a routine bump; and a change
FastMCP needs goes upstream, never into a local patch.

### The core knows no Client; a Client Profile knows one

The core speaks only MCP. Everything Client-specific is a Profile: the Client's documented
limits, its config file location and shape, the transports it accepts, whether it needs the
Shim, and its conveniences. `proxy install` and `doctor` consult the Profile named on the
command line, `upstream scan` searches every Profile's locations, and default Caps come from
a Profile. A new Client is a new Profile, never a special case in the core; Claude Code is the
first and best-integrated Profile and is no exception. Every number in a Profile is a dated
fact with a source in `docs/clients.md`, and a limit is never enforced silently: it is
reported, and the user decides.

### Nothing reaches the model unasked

A new Catalog item is hidden in every Proxy until the user exposes it. Drift is recorded, not
served: a Proxy's exposed set changes only on accept, and the CLI says so on every command
until the Drift is reviewed. An Override for a vanished item is kept as an orphan and applies
again if the item returns.

### Identity is the Catalog name

Config files, Hooks, and the CLI key every item by its Catalog name, which never changes, and
the call log records it; the exposed name is the last step before a Client sees it. A Virtual
Tool's exposed name is its identity, so one named like an exposed Catalog tool is a load
error, not a shadow.

### A Proxy stays up

A Proxy answers `initialize` and every list from the stored Catalog, without waking the
Upstream. An Upstream outage fails only the calls made while it is away, with a message the
user can set, because Clients cannot tell that a server behind a Proxy went down and came
back. A Proxy whose user files fail to load is unhealthy: it keeps advertising its last
exposed set, since Clients keep the tools they last received and call anyway, and every call
answers an error naming the Proxy and the reason. The Daemon never crashes on user code.

### One Daemon, one connection per Upstream

One long-running process on one loopback port, every Proxy at its own path, a per-Proxy port
only as an override. One connection per Upstream, shared by all its Proxies and all Clients;
nothing is per-Client. Lazy by default, warm on request, and never given up on: a failed
connect backs off to a ceiling, the next call tries again, and a warm Upstream is retried by
its Keeper. A bind beyond loopback requires a bearer token, and anything beyond that is a
reverse proxy's job. The CLI edits files and signals the Daemon; the management API is only
what a running Daemon knows, and every command that edits or inspects configuration works
with the Daemon down. Streamable HTTP is the Client-facing transport; the Shim exists for the
Clients that accept only stdio.

### Caps only lower

One global Cap per kind of text; an Upstream, a Proxy, or a tool may only lower what it
inherits. Text cut to a Cap says so: a marker on names, descriptions, and instructions, and a
note telling the model how much was cut on tool output.

### User code is Python, in-process, unsandboxed

Hooks and Virtual Tools run inside the Daemon, sync in a worker thread or async on the loop,
and reach only their own Upstream. Exceptions become tool errors and log lines. An async Hook
that blocks stalls the Daemon and `sys.exit` is not guarded against; both are documented, not
solved. A Proxy's files, and its Upstream's, are re-read on the next request after they
change, for that Proxy alone; no watcher runs between requests.

### Files

XDG layout on both OSes: configuration under the config directory, what mcpshape learned
under the state directory. Config is TOML read and rewritten with `tomlkit`, so hand-written
comments survive the CLI; every file carries a `version` integer, and a JSON Schema is shipped
per file for editors. A secret never goes into a config file: `${VAR}` is resolved from the
Daemon's environment, then from a mode-0600 secrets file, and a resolved value never reaches
a log line, an error, or the terminal. OAuth tokens are kept encrypted in the state
directory; the login runs from the CLI, and the Daemon never opens a browser or waits on one.
`transport = "memory"` is the test seam's alone: it imports Python into the Daemon by naming
it in a file, so a user's file that says it is refused with the reason.

### Observability

An app log with levels and an always-on call log, both files under one size cap, no
database. The dashboard is plain HTML, CSS, and JavaScript shipped in the package with no
build step, read-only, fed by the management API alone.

### Engineering

uv, ruff strict, pyright strict, pytest with Hypothesis; GitHub Actions on macOS and Linux;
Python 3.12 or newer; semver; MIT; releases to PyPI, the Homebrew tap's formula bumped to
each; `uv tool install` or `brew install` the install path. No telemetry, no update checks, no
network call except to configured Upstreams.

Tests drive the system through one seam, `tests/support/seam.py`: a temp config directory,
the Daemon app in-process, in-memory Upstreams, a FastMCP Client over ASGI, and the CLI
through Typer's runner. A test reaches past the seam only where no Client-driven path exists:
the Shim as a real subprocess, real transports and the OAuth provider behind the Daemon as
child processes, the Daemon under a signal as a real process, the autostart unit writers
against golden files, the property tests at their module, the Adapter contract, the import
boundary, and the Keeper's fault injection.

## Rejected

- One process per Proxy: noisier and heavier than one Daemon that holds every connection.
- One port per Proxy as the default: port bookkeeping; kept only as a per-Proxy override.
- A second port, or a Unix socket, for the management API: no benefit once the dashboard
  needs loopback TCP anyway.
- System directories such as `/etc` or `/Library`: for all-users, pre-login daemons, and root.
- JSON, JSONC, or YAML config: Python has no mature comment-preserving writer for them, and
  `tomlkit` is the only mature round-trip library. One format for config, strict JSON only on
  export.
- A YAML dependency so that `upstream scan` reads Goose and Continue files: they are reported
  by name and not read.
- Plaintext secrets in Proxy files, or the OS keychain: painful headless and under launchd.
- An expression mini-language for Hooks: a second thing to design; Python only.
- Passing new Catalog items through by default.
- An empty exposed set for an unhealthy Proxy, or serving Overrides without Hooks when user
  code fails: the first confuses Clients, the second silently skips rewrites.
- pip and pipx as documented install paths: uv and the Homebrew tap only.
- Merging Upstreams into one Proxy; see above.

## Parked

- Editing from the dashboard; disabled or temporary Upstreams as a file-level fact.
- Pinning tools against Client-side deferral: only Claude Code's whole-server flag exists.
- Per-Proxy search-and-call meta-tools for long-tail Upstreams, FastMCP's Tool Search
  transform: progressive disclosure inside a Proxy, opt-in only, never the default.
- Client-specific `_meta` hints from a Profile, when a Client documents them.
- Cross-Proxy calls from user code, programmatic tool-list Hooks, Virtual Upstreams, and
  tools composed across Upstreams beyond Virtual Tools.
- The OS keychain for secrets.
- Pushing `tools/list_changed` to idle Clients, once FastMCP serves the stream and Clients act
  on it.
- Windows.

## Out of scope

Merging Upstreams into one Proxy. Hosting or running Upstreams remotely.
