# mcpshape design brief

Outcome of the design session of 2026-09-06/07, recorded verbatim in `docs/sessions/`; that
transcript is history, not rules, and this brief wins where they differ. Vocabulary is in
`CONTEXT.md`, the reasoning behind the hard-to-reverse decisions in `docs/adr/`, and the dated
Client and FastMCP facts the decisions rest on in `docs/clients.md`. This is the shared
understanding the implementation starts from.

## Why

MCP servers as shipped are bloated: thirty to fifty tools each, many redundant or broken, with
long descriptions. Clients such as Claude Code offer no per-tool control, so the choice is
everything or nothing. Several such servers together dilute the model's attention until MCP
stops being useful. Nobody had built the obvious fix: a local layer that lets the user decide
what a model sees, per server, without touching the server.

## Goals
- Curated, tailored MCP servers for any Client, from any Upstream, with no changes to either.
- Resource-friendly and minimally invasive: one lazy daemon, nothing running when nothing is used.
- Runs on a laptop and on a headless server alike.
- Full FastMCP compliance: every field, property, and value FastMCP supports is displayed
  and forwarded; the user narrows what is shown, mcpshape does not.
- Production quality from the first release: packaging, tests, docs, and upgrade path.
- Minimalistic, elegant, upgradeable, tested through and through.
- Open source, MIT.

## What it is

A local, per-user daemon that sits between MCP Clients and the MCP servers they use.
Each Upstream is added once and gets one or more Proxies. A Proxy is a curated MCP
server of its own: tools hidden, renamed, re-described, capped, hooked, or added,
before a model sees them. A Proxy serves one Upstream; Upstreams are never merged (ADR 0002).

Built on unmodified FastMCP 4.x, pinned to a major, with every FastMCP touchpoint
behind the Adapter so user-facing files and APIs survive FastMCP churn (ADR 0001).

Client-agnostic: the core speaks only MCP and knows no Client. Everything Client-specific,
limits, config paths, transports, conveniences, lives in a Client Profile (ADR 0003).
Claude Code is the first and best-integrated Profile, never a special case in the core.

## Decisions

### Topology
- The CLI edits config files directly and signals the Daemon to reload. The Daemon exposes a
  small management API only for live state (Catalog, health, logs, call log). Every CLI
  command that edits or inspects config works when the Daemon is down. The dashboard uses the
  same API, with as little scaffolding as possible.
- One Daemon process. One TCP port, loopback by default: `/<upstream>/mcp` and
  `/<upstream>/<proxy>/mcp` for Proxies (`default` is also reachable at `/<upstream>/mcp`),
  `/api` for management, `/` for the dashboard. Optional per-Proxy port override.
- Non-loopback bind requires a static bearer token. Anything beyond is a reverse proxy's job.
  The dashboard is meant for a browser on the same machine at `localhost:<port>`; reaching it
  on a headless server is the user's routing, not mcpshape's.
- Upstreams are reached over stdio (a child process the Daemon spawns), Streamable HTTP, or
  legacy SSE, with or without OAuth.
- One Upstream connection, shared by all its Proxies and all Clients. One Proxy serves any
  number of Clients at once. Nothing is per-Client.
- Every Upstream gets a Proxy named `default` when it is added.
- Streamable HTTP is the Client-facing transport. A hidden `serve` stdio shim speaks stdio to
  the Client, forwards to the Proxy URL, and starts the Daemon if it is not running. It exists for the Clients that accept only stdio (see `docs/clients.md`).
  `proxy install` writes whichever form the Client named with `--to` needs.

### Upstream lifecycle
- Lazy by default: connect on first tool call, disconnect after `idle_timeout`
  (default 10 min, 0 = never). `warm = true` connects at Daemon start and pings on an interval
  so a warm Upstream is never falsely reported up. These settings belong to the Upstream,
  since the connection is the Upstream's and shared by its Proxies.
- Clients cannot detect that an Upstream behind a Proxy went down and came back, and Claude
  Code needs a manual reconnect and re-auth to notice. So a Proxy stays up and reachable
  through every Upstream outage; only individual calls fail while the Upstream is away.
- `initialize` and `tools/list` are answered from the stored Catalog instantly.
- Connect failures within the connect timeout return a tool error with a configurable message.
- Settled in #64, replacing "auto-reconnect with capped exponential backoff": an Upstream
  that cannot be connected to costs nothing nobody asked for, and the Daemon never gives up
  on one. After a failed connect the backoff doubles from a second to the Upstream's
  `backoff_cap` (a lifecycle setting, default 30 minutes); a call within it is answered with
  the `unavailable_message` at once, and a call after it is what tries again. A warm Upstream
  is also retried by the keeper when the backoff runs out, since warm means keep it up; a lazy
  one never on its own, since lazy means connect when asked, at failure as at start. The app
  log says it once per reason and once per doubling, not once per attempt. `ls`,
  `daemon status`, and `/api/status` say when the next attempt is, and `daemon status` names
  `upstream connect`, which tries now whatever the backoff says. An edit to the Upstream file
  reconnects as #46 says.
- A call that fails because the open connection is dead, not because the Upstream answered
  an error, moves the Upstream to `unavailable` at once and is answered with the same
  configurable message; the backoff and reconnect follow as after a failed connect (settled
  in #22). A warm Upstream's failed ping does the same.
- The keeper's restart cap is a rate, five failures within ten minutes on the Daemon's clock,
  so failures spread over a long life never end supervision (settled in #50). Past it the
  Upstream is `unavailable` with the reason, `daemon status` says the keeper stopped, and
  `daemon reload` starts a keeper again.
- Settled in #46 and #62: the Daemon re-reads an Upstream file when it changes, checked on
  every request as a Proxy's own files are (#10) and on `daemon reload`. A Cap edit is in
  force on the next request to any of the Upstream's Proxies; a transport or lifecycle edit
  connects again under the new settings, and that reconnect rescans, since what a changed
  transport reaches may advertise something else; an Upstream file that cannot be read leaves
  the connection on its last settings and marks every Proxy of it unhealthy with the reason.
  An Upstream file that is gone retires the Upstream: its Proxies answer not found, its
  connection is let go, it is no longer listed, and its state directory is never written
  again, with a reconnect's rescan that finds the file gone dropping what it saw and removing
  anything it wrote. `upstream rm` signals a running Daemon, so this happens at once.
- Settled in #67: an Upstream added while the Daemon runs (its directory and `upstream.toml`
  appearing under `upstreams/`) is found on its first request, on `daemon status`, and on
  `daemon reload`, and launched the same way one present at Daemon start is; a Proxy added
  the same way to an Upstream already held is found and launched the same way, sharing its
  Upstream's connection; a removed and re-added name is a new Upstream, not an edit of the
  old one. The one hand-edit limit this leaves: an Upstream directory removed and re-created
  by hand between two looks, with no `upstream rm` in between, is seen as an edit of the same
  Upstream, since only the file's stamp is watched.
- Settled in #68 and #70: a Proxy file that is gone has its Proxy let go on the next look at
  its Upstream, on a request to any of its Proxies, on `daemon status`, or on `daemon reload`:
  its URL answers not found naming it, it is no longer listed, and its app is closed, while
  the Upstream's connection and its other Proxies stay as they are; a file back under the same
  name is a new Proxy. A Proxy's `port` override is kept in step with its file the same way:
  a port set is listened on after the next look at the file, one changed moves the listener,
  one removed closes it, and one that cannot be bound is an unhealthy Proxy with the reason
  while its path is served as before, asked for again on the next look or on `daemon reload`.
- Health of every Upstream and Proxy is shown by `ls`, `daemon status`, and the dashboard.

### Catalog and Drift
- Catalog persisted per Upstream in the state dir. Rescan on Daemon start, on reconnect,
  and on `upstream sync`.
- Drift default: new items hidden, vanished items' Overrides kept as orphaned. The default
  is configurable in the global settings. The CLI prints a one-line notice on every command until
  the Drift is reviewed. `upstream sync` shows the Drift; `--accept` applies it.
- A Proxy's exposed set changes only on accept, so `tools/list_changed` reaches Clients only
  then. Most Clients need a reconnect to see it; the CLI and dashboard say so.

### Curation layers on a Proxy
1. **Overrides** (declarative): per tool: exposed name, title, description, per-argument
   name/description/default/required/hidden, annotations, hidden. Per resource and prompt:
   exposed name, description, hidden. Per Proxy: exposed server name and instructions.
   Not editable: input/output schema types.
2. **Caps**: ceilings on the length of tool names, tool and argument descriptions, Proxy
   instructions, and tool output. One global master Cap per kind; an Upstream, a Proxy, or a
   tool may each only lower what it inherits (global, then Upstream, then Proxy, then tool).
   Names, descriptions, and instructions cut to a Cap end with a marker; tool output cut to a
   Cap instead tells the model how much was cut.
3. **Hooks**: user Python, before/after per tool, resource, and prompt. Can rewrite args and
   results, short-circuit, or raise. Reach only their own Upstream.
4. **Virtual Tools**: user Python tools with a handle to call their own Upstream.
- Identity is the Catalog name everywhere in config and Hooks. Exposed name is the last step.
- User code lives in `<proxy>.py` next to `<proxy>.toml`, uses mcpshape's own decorator API
  (`@hook.before`, `@hook.after`, `@tool`, `upstream.call`), sync or async.
- Settled while landing #9: an `after` Hook runs on every result the Proxy is about to send,
  a short-circuited one included; a Virtual Tool's exposed name is its identity, so Hooks
  keyed by it run around it, and a Virtual Tool named like an exposed Catalog tool is a load
  error, not a shadow; `upstream.call`, `read`, and `get` go straight to the Upstream, past
  the Hooks.
- Settled in #24: a tool's `after` Hooks run on an error result too. The result they receive
  says `is_error`, and what they return is what the Client gets: a success, or another error.
  Raising from a Hook stays an error. A resource read or a prompt get has no error result on
  the wire, only an exception, so an error there still skips the `after` Hooks.
- Settled in #25: a Hook's result that cannot satisfy the tool's output schema is a tool
  error at the Proxy naming the Hook, the tool, and what the schema expects, not a rejection
  at the Client. `doctor` cannot know what a Hook returns, so the runtime error is the check.
- Settled in #26: a sync Hook or Virtual Tool runs in a worker thread, as FastMCP runs a sync
  tool, and from there `upstream.call`, `read`, and `get` block until the Upstream answers;
  an async one awaits them. So a sync function reaches the Upstream, and one that blocks
  stalls only its own call.
- Settled in #27: while a Proxy's Python file loads, its Upstream's directory is importable,
  so `import helpers` finds `upstreams/<name>/helpers.py`; a helper is re-imported on every
  load, so every Proxy of the Upstream re-reads its files when a helper changes, and a
  helper of one Upstream is never seen by another's Proxy. `doctor` loads files the same way.
- Settled in #45: a Virtual Tool's name is its identity, so a Cap never cuts it; one longer
  than the tool name Cap in force is a load error naming the tool and the Cap, and `doctor`
  reports it.
- Hooks run in-process with no sandbox. Exceptions become tool errors and log lines. An async
  Hook that blocks forever stalls the Daemon, and `sys.exit` anywhere in user code is not
  guarded against; this is documented, not solved.
- Files are watched and the affected Proxy reloaded (`daemon reload` also exists). Load errors
  mark the Proxy unhealthy: it keeps advertising its last exposed set and every call
  returns a tool error naming the Proxy and reason. Nothing reaches the Upstream. The Daemon
  never crashes on user code.
- Settled while landing #10: watching is a look at the files' stamps on every request a Proxy
  serves and on every read of the live state, so a change is served on the next request
  after it, the affected Proxy alone, and no watcher runs between requests. `daemon reload`
  makes every Proxy re-read its files now and reports each one's health with its reason.
- Per-Proxy `instructions` override is a first-class feature: in Clients that defer tool
  loading (Claude Code today), instructions are what the model sees first.

### Files
- XDG layout on both OSes: `~/.config/mcpshape/` (global `config.toml`,
  `upstreams/<name>/upstream.toml`, `upstreams/<name>/<proxy>.toml` + `<proxy>.py`),
  `~/.local/state/mcpshape/` (Catalogs, Drift, encrypted OAuth tokens, `log/`).
  `--config-dir` and env var override.
- Upstream and Proxy names are user-chosen slugs. Both appear in the Proxy's URL.
- TOML read and rewritten with `tomlkit` so hand-written comments survive CLI edits.
  Each file carries a `version` integer for migrations. A JSON Schema is shipped for editor
  validation via the TOML schema comment. `proxy export` produces strict `mcpServers` JSON
  (Client config files reject comments) pointing at the Proxy.
- Secrets: `${ENV_VAR}` references resolved from the Daemon environment or a 0600 secrets file.
- An Upstream is reached over stdio, http, or sse, and those are the transports a user's file
  may name. `transport = "memory"` is the test seam's alone (settled in #18): it imports Python
  into the Daemon process by naming it in a config file, so the loader refuses it in a user's
  file with the reason, the shipped schema does not list it, and only the seam enables it, in
  Python; no config value, environment variable, or CLI flag does.
- OAuth for remote Upstreams: CLI opens the browser and receives the loopback callback;
  dashboard flow second; device-code pairing on headless.
- Settled in #12: an http or sse Upstream file says `auth = "oauth"`; the login runs from
  the CLI, at `upstream add --oauth` and, whenever no usable token is stored, at
  `upstream sync`, with `--device` asking for device-code pairing where the provider's
  metadata offers it. The Daemon never opens a browser or waits on a login: with no usable
  token it moves the Upstream to `unavailable` with a message naming the command to run.
  Tokens and the provider's client registration are kept under the state directory,
  encrypted with a key held in a mode-0600 file there; a token is refreshed without the user
  when the provider allows, and an expired token that cannot be refreshed is the
  `unavailable` case above. No token, key, or code ever reaches a log line, an error
  message, or the terminal.
- Settled in #16, the dashboard's flow: `POST /api/upstreams/<name>/oauth` starts the same
  login the CLI runs, but the Daemon opens no browser: it answers with the provider's page for
  the dashboard to open, receives the callback on loopback, and stores what the CLI would.
  `GET` says whether a token set is stored, whether a login is pending and at which page, and
  why the last one failed. A login that succeeds scans the Upstream, since the start-up scan
  had nothing to log in with, and makes it connect at once instead of waiting out its backoff.
  A login nobody finishes is given up after the browser flow's own five minutes, so a fresh
  one can start. A connect still never waits on a login.
- Settled in #56, #57, and #58: a stored login is a token set, which only the Adapter can tell
  from the registration the SDK writes first, so `upstream show` and `upstream sync` ask the
  Adapter; an Upstream the start-up scan could not reach is scanned on its first connect,
  since that is the first look the Daemon gets, while one the scan reached still rescans only
  on a reconnect; and a login from the CLI is followed by `POST /api/upstreams/<name>/connect`
  when a Daemon answers, so an `unavailable` Upstream tries again now instead of waiting out
  its backoff.
- Settled in #52, #42, and #51: letting go of a connection a call found dead logs one line
  naming the Upstream and the reason, never a traceback; what an stdio Upstream's child
  writes to stderr goes to the app log line by line under the Upstream's name, so `daemon
  logs` shows it, through a pipe the Daemon reads and never the Daemon's own stderr; and
  `upstream show` says which scopes the stored login was granted, with `upstream sync` and
  `show` saying once when the browser flow, which asks for what the provider advertises,
  was granted other scopes than the file's `scopes`.

### Client Profiles
- One Profile per supported Client: config file path and format, transports accepted, whether
  the stdio shim is needed, the naming scheme the Client applies to tool names, documented
  limits with source and date, and conveniences. Seed data: `docs/clients.md`.
- Used by: `proxy install` (writes the right entry, optionally disables the Client's
  original entry for that server, warns when names exceed what the Client allows),
  `upstream scan` (where to look), default Caps, and `doctor` (validates exposed names and
  schemas against the Profile of the Client named with `--for`).
- Limits are dated facts; nothing is enforced silently.
- Initial Profiles: every Client listed in `docs/clients.md`. Only those with documented
  limits carry real numbers at first.
- Claude Code Profile defaults, set now: Caps for tool descriptions and Proxy instructions
  comfortably under the 2KB at which Claude Code cuts them off, and `doctor` reminds that
  critical text goes first because the first sentence carries the routing hint.
- Settled in #44: `doctor --for` compares each Proxy's resolved Cap (global, then Upstream,
  then Proxy) with the Profile's documented number and names the level that set it, instead of
  only printing the Profile's recommendation as a note.

### Observability
- App log with standard levels. Verbose by default during development; configurable down to
  warnings or errors only. A separate, always-on call log (name, args, duration, outcome,
  truncated result) feeds the dashboard: in-memory ring buffer plus JSON lines on disk.
  Size-based rotation under one global size cap across all log files. No database.
- Settled in #16: both logs live under `<state>/log/`, `daemon.log` and `calls.jsonl`, and
  `[log]` in `config.toml` sets the app log's `level` (`debug` by default) and `max_bytes`, the
  one cap both share. A file is rotated aside at a quarter of the cap, and the oldest rotated
  files go, whichever log they belong to, until the directory fits. A call record carries the
  tool's Catalog name and exposed name, the arguments under Catalog names as the Client sent
  them, the time the Client waited with the Hooks included, `ok` or `error`, and the first
  500 characters of the result or the error, with the full length beside it, and every
  string among the arguments cut the same way. Every tool call a Proxy runs is recorded, one
  it refused as unhealthy or one its Upstream was away for included; a call FastMCP refuses
  before it reaches the Proxy's chain, to a name it does not expose or with arguments that
  fail the tool's schema, is not, and neither are resource reads and prompt gets. The ring buffer keeps the latest 200 calls per Proxy. `daemon logs` reads the
  app log tail, `--calls` the call log, from the running Daemon when there is one and from
  the files otherwise.
- Settled in #16: the management API under `/api` is the live state and what only a running
  Daemon can do: `status`, `reload`, `shutdown`, per Upstream the stored Catalog, the Drift,
  a `sync` that rescans and records Drift, and the OAuth flow; `calls` and `logs` tails. A
  sync through the API never accepts Drift: accepting edits Proxy files, which is the CLI's.
  The bearer token, when configured, guards every one of them.

### CLI
```
mcpshape add <upstream> --stdio '...' | --url ...   convenience for upstream add + default Proxy
mcpshape ls                                          convenience for upstream ls + proxy ls
mcpshape upstream   add | env | ls | show | sync | connect | rm | scan
mcpshape proxy      new | ls | show | rm | install | export
mcpshape tool       hide | show | rename | describe | trim | cap
mcpshape daemon     up | down | status | logs | reload | install | uninstall
mcpshape ui
mcpshape doctor
```
- Typer + Rich. Every command answers `-h` and `--help` with one example. `serve` is hidden.
- `doctor` validates every config file against the shipped schema and loads every user Python
  file without starting anything, then reports.
- `tool trim` shows the original description and opens it for editing. It is a replace;
  cutting text to a length is a Cap.
- `upstream scan` discovers servers in known Client config locations, typical directories,
  and any directory the user names.

### Autostart and distribution
- `daemon install` writes a launchd user agent (macOS) or a `systemd --user` unit with linger
  (Linux). Offered on first `daemon up`.
- The unit carries the PATH of the shell `daemon install` ran in, captured once (settled in
  #48): the MCP SDK gives the Daemon's PATH to every stdio child, so an Upstream started by a
  bare `npx` or `uvx` is found under autostart as it is in a terminal. Nothing is resolved at
  spawn, and install is not expected to be run again. A command that cannot be found is a
  different failure: `doctor` reports it against the shell it runs in and `daemon status`
  against the Daemon's PATH, each naming the Upstream, the command, and the PATH looked in.
- Home is GitHub. Releases go to PyPI; `uv tool install mcpshape` is the install path.
  Homebrew formula after the first stable release, built from PyPI. Python 3.12 minimum.
- No telemetry, no update checks, no network calls except to configured Upstreams.

### Engineering
- uv, ruff (strict), pyright strict, pytest with in-memory FastMCP Upstreams, Hypothesis for
  config round-trips and Override application over generated Catalogs. A benchmark script for
  proxy overhead instead of a stress-test suite. GitHub Actions on macOS and Linux. Semver. MIT.
- Every commit message, pull request title, and merge subject is a Conventional Commit,
  `type: subject`, the subject in the glossary's words. CI checks a pull request's title and
  every commit on its branch, and the ruleset requires that check; the repository's default
  merge subject is the pull request title, so a merge carries a checked subject.
- Protected `main`: pull requests only, one per wave (one issue, or a group of issues, with
  their review commits; a wave that changes only rules or docs has no ticket), merged with a
  merge commit once CI is green. The merge closes the wave's tickets, and only the merge: CI
  refuses a pull request that changes `src/` or `tests/` without a `Closes` line, so no ticket
  is left to be closed by hand. The ruleset refuses anything else; nothing is ever committed on
  `main`. How a branch is kept, and how a pull request is titled, written, and merged, is in
  `docs/agents/issue-tracker.md`.
- A wave's branch is brought up to date by rebasing onto `main`, never by merging `main` in;
  CI refuses a branch that holds a merge commit.
- A wave is reviewed on its branch before its pull request opens, on two axes: standards,
  against this brief and the glossary, and spec, against the ticket. The findings are applied
  in review commits on the same branch, and the session landing the wave reads the riskiest
  file itself, whoever wrote it. A finding is never deferred to a later pull request.
- A session files every follow-up it observes while landing a wave, as a child ticket of the
  spec with labels and blocked-by edges, before the wave's pull request merges. Nothing is
  left for the user to remember.
- Tests drive the system through one seam: a temp config directory, the Daemon app in-process,
  in-memory Upstreams, a FastMCP Client over ASGI, and the CLI via Typer's runner. Tests never
  import internal modules to assert on their state. Exceptions: the stdio shim (real subprocess),
  real Upstream transports (`tests/test_real_transports.py`: a child process and a loopback
  server behind the Daemon, since only those show one child shared by every Proxy and session),
  autostart unit writers (golden files), the Daemon under a signal (`tests/test_daemon_ports.py`:
  a real process, since a signal cannot be sent to the test process itself), the Adapter
  contract tests,
  `tests/test_config_roundtrip.py` (the tomlkit round-trip property, at the config module),
  `tests/test_overrides_property.py` (the Override application property, at the proxy module), and
  `tests/test_boundaries.py`, which reads source files to enforce the import rule below, and
  the keeper fault injection in `tests/test_upstream_lifecycle.py`, since no Client-driven path
  makes the keeper raise.
- Only the Adapter imports FastMCP (ADR 0001). A FastMCP behavior that
  mcpshape's code relies on is pinned in `tests/test_fastmcp_contract.py`; a docstring or
  `docs/clients.md` alone does not count.
- One repo, modular for clarity. Dashboard as a separate package directory in the same repo,
  built to static files the Daemon serves.
- Name: `mcpshape`. The working name `mcpi` is the Minecraft Pi API on PyPI, npm, and GitHub.

## Rejected alternatives

- One process per Proxy: noisier and heavier than one Daemon with supervised connections.
- One port per Proxy as the default: port bookkeeping; kept only as a per-Proxy override.
- Two ports (Proxies vs management), or a Unix socket for the management API: no benefit once
  the dashboard needs loopback TCP anyway.
- System directories (`/etc`, `/Library`): those are for all-users, pre-login daemons and need root.
- JSON/JSONC/YAML config: Python has no mature comment-preserving JSONC writer; `tomlkit` is
  the only mature round-trip library. One format for config, strict JSON only on export.
- Plaintext secrets in Proxy files, or OS keychain: env references + 0600 file instead;
  keychain is painful headless and under launchd.
- Expression mini-language for Hooks: a second thing to design; Python only.
- Passing new Catalog items through by default: violates "nothing reaches the model unasked".
- An empty exposed set for an unhealthy Proxy: Clients keep the tools they last received and
  call anyway, so the last exposed set stays advertised and every call errors instead.
- Serving Overrides without Hooks when user code fails: would silently skip rewrites.
- pip/pipx as documented install paths: uv only.

## Parked
- Pinning tools against Client-side deferral (only Claude Code's whole-server flag exists).
- Per-Proxy "search + call" meta-tools for long-tail Upstreams (FastMCP's Tool Search
  transform). Progressive disclosure inside a Proxy, opt-in only; never the default.
- Emitting Client-specific `_meta` hints from a Profile when the Client documents them.
- Cross-Proxy calls from user code: would merge Upstreams by another route.
- Programmatic tool-list Hooks.
- Virtual Upstreams (tools wrapping non-MCP things); tools composed across Upstreams, beyond
  Virtual Tools.
- OS keychain for secrets.
- Windows: "not required at this stage".
- Dashboard framework choice: separate design session. Constraints: lightweight, nothing the
  Daemon already does, CLI parity via the management API only.

## Out of scope
- Merging Upstreams into one Proxy. Hosting or running Upstreams remotely.

## To verify before relying on it
- Claude Code's documented 2KB cutoff of instructions and tool descriptions: measure the
  exact cutoff (characters vs bytes) and whether it is per server or a shared pool across
  servers. Test with a throwaway Upstream. Tighten the Claude Code Profile's default Caps
  to the measured numbers if they differ.
- FastMCP 4 behavior when an stdio Upstream is shared across many concurrent Client
  sessions (assumed fine; confirm under load with the benchmark script).
