# mcpshape user guide

mcpshape is a local proxy that sits between the MCP servers you use and the Clients that use
them, so you decide, per server, exactly what a model sees. You add an MCP server once as an
Upstream. mcpshape reads what it advertises into a Catalog and serves one or more Proxies for
it, each a curated MCP server of its own at its own local URL. On a Proxy you hide, rename,
re-describe, and cap what the Upstream advertises, rewrite requests and responses in Python,
and add tools the Upstream never had. Clients are pointed at the Proxy instead of the server.

The words used here are the glossary's (`CONTEXT.md`); the reasons behind the rules are in
the design brief (`docs/DESIGN.md`).

## Install

```
uv tool install mcpshape
```

Python 3.12 or newer, macOS or Linux. mcpshape makes no network call except to your Upstreams:
no telemetry, no update checks.

## The first Upstream

```
mcpshape add github --stdio 'npx -y @modelcontextprotocol/server-github'
mcpshape add docs --url https://example.com/mcp
mcpshape add legacy --url https://example.com/sse --sse
```

`add` registers the Upstream and gives it a Proxy named `default`. Then read its Catalog:

```
mcpshape upstream sync github
```

The first sync stores what the Upstream advertises. `mcpshape ls` shows every Upstream and
Proxy with its health and address; `mcpshape upstream show github` shows one Upstream's file
and its Proxies; `mcpshape upstream rm github` removes an Upstream with its Proxies and its
Catalog.

`mcpshape upstream scan` finds the MCP servers already configured for your Clients, in the
locations mcpshape knows and in any directory you name, and offers to add each as an
Upstream. `--list` only lists; `--yes` adds them all.

## Proxies and where they are served

Every Upstream has a `default` Proxy. Another Proxy of the same Upstream is another curation
of it, for another Client or another purpose:

```
mcpshape proxy new github/review
```

The Daemon serves every Proxy on one port, loopback by default:

- `http://127.0.0.1:8321/github/mcp` and `http://127.0.0.1:8321/github/default/mcp` for the
  default Proxy;
- `http://127.0.0.1:8321/github/review/mcp` for the Proxy named `review`.

A Proxy file may set `port = 8400` to be served on that port as well, for a Client that cannot
take a path. `mcpshape proxy ls` prints every URL.

## Pointing a Client at a Proxy

```
mcpshape proxy install github/default --to claude-code
mcpshape proxy install github/default --to claude-desktop --disable github
mcpshape proxy export github/default
```

`proxy install` writes the entry the named Client expects into that Client's config file, in
the transport that Client accepts, and warns when a Proxy or tool name exceeds what the Client
allows. `--disable NAME` also turns off the Client's own entry for the raw Upstream, where the
Client documents an off switch. `proxy export` prints strict `mcpServers` JSON to paste
anywhere. For a Client that accepts only stdio, the entry runs the hidden `mcpshape serve`
command, which speaks stdio to the Client, forwards to the Proxy, and starts the Daemon if it
is not running. `mcpshape doctor --for claude-code` checks every exposed name against that
Client's documented limits.

## Catalog and Drift

A Proxy answers `initialize` and every list from the stored Catalog, instantly, without
waking the Upstream. The Catalog is rescanned when the Daemon starts, whenever an Upstream
reconnects, and on `upstream sync`. Where the stored Catalog and what the Upstream advertises
now disagree, that is Drift. Drift is recorded, not served: every CLI command prints a
one-line notice until you review it.

```
mcpshape upstream sync github            # shows the Drift
mcpshape upstream sync github --accept   # makes it the Catalog
```

New items are hidden in every Proxy until you expose them, so nothing reaches a model without
your say-so; `config.toml` can switch that default with `[drift] new_items = "visible"`.
An Override for a tool that vanished is kept and reported as orphaned; it applies again if the
tool returns. Most Clients only see a changed tool set after a reconnect; `sync --accept`
names the ones that do.

## Overrides: hide, rename, re-describe

Everything is keyed by the tool's Catalog name, which never changes; the exposed name is what
a Client sees.

```
mcpshape tool hide github/default create_gist
mcpshape tool show github/default create_gist
mcpshape tool rename github/default create_issue new_issue
mcpshape tool describe github/default create_issue 'Open an issue.'
mcpshape tool trim github/default create_issue
mcpshape tool hide github/default create_issue --arg assignees
mcpshape tool rename github/default create_issue --arg body text
```

`tool trim` shows the original description and opens it in `$EDITOR`; what you save replaces
it. These verbs edit the Proxy file, `~/.config/mcpshape/upstreams/github/default.toml`,
which you can also edit by hand; comments and formatting survive the CLI's edits. The same
file sets the Proxy's exposed server name and its instructions, which is what a Client that
defers tool loading shows the model first:

```toml
name = "GitHub, curated"
instructions = "Use new_issue for bugs. Never close issues."

[tools.create_issue]
name = "new_issue"
title = "New issue"
description = "Open an issue."
annotations = { read_only = false, destructive = false }

[tools.create_issue.args.assignees]
hidden = true
default = ["me"]

[resources."issues://open"]
description = "Open issues, newest first."

[prompts.triage]
hidden = true
```

An Override never changes an input or output schema type; an argument can be renamed,
re-described, defaulted, made optional, or hidden, and a hidden argument the Upstream
requires needs a default.

## Caps

A Cap is a ceiling on how long a kind of text a Proxy exposes may be: tool names, tool
descriptions, argument descriptions, the Proxy's instructions, and tool output. `config.toml`
holds the global master Cap per kind; an Upstream, a Proxy, or a tool may only lower what it
inherits, never raise it.

```toml
# config.toml
[caps]
tool_description = 1800
instructions = 1800
tool_output = 20000
```

```
mcpshape tool cap github/default search_issues --description 200 --output 4000
```

A name, description, or instructions text cut to a Cap ends with a marker; tool output cut to
a Cap instead ends with a note telling the model how many characters were cut. The defaults
sit comfortably under the 2KB at which Claude Code truncates descriptions and instructions.
A Cap that would raise what it inherits is refused on load and reported by `doctor`.

## Hooks and Virtual Tools

Next to the Proxy file, `default.py` holds Python that runs inside the Proxy:

```python
from mcpshape import hook, tool, upstream

@hook.before("create_issue")            # a tool, by Catalog name
def label(call):
    call.args["labels"] = ["proxied"]   # rewrite the arguments in place

@hook.after("create_issue")             # sync or async, as you like
async def trim(call, result):
    result.text = result.text[:2000]    # the result to send; None keeps it as is
    return result

@hook.after("search")
def soften(call, result):
    if result.is_error:                 # an error the Upstream reported
        result.text = "Search is unavailable right now."
    return result

@hook.before.resource("issues://{id}")
def note(call):
    call.args["id"] = call.args["id"].strip("#")

@tool                                   # a Virtual Tool: schema from the signature
async def close_all(ids: list[int]) -> str:
    """Close several issues."""         # description from the docstring
    for id in ids:
        await upstream.call("close_issue", id=id)
    return "done"
```

- A `before` Hook may change `call.args` or return a result, which answers the Client without
  calling the Upstream. An `after` Hook receives the result and returns the one to send; it
  runs on every result, a short-circuited one included, and on a tool's error result, which
  says `result.is_error`. It may turn an error into a success or a success into an error by
  setting that flag. Raising from a Hook becomes a tool error carrying the exception's
  message, and a log line; the Proxy stays healthy.
- Hooks exist for tools, resources (`hook.before.resource`, keyed by URI or URI template),
  and prompts (`hook.before.prompt`). A resource read or prompt get that the Upstream fails
  reaches the Client as an error without running the `after` Hooks.
- `upstream.call`, `upstream.read`, and `upstream.get` reach the Proxy's own Upstream under
  Catalog names, past the Hooks, and nothing else. An error the Upstream reports raises
  `UpstreamError`. From an `async` function you `await` them; from a plain function you call
  them and get the result.
- A Virtual Tool's exposed name is its identity: Hooks keyed by it run around it, and a
  Virtual Tool named like an exposed Catalog tool is a load error.
- A result must fit the tool's output schema. When user code returns something that cannot,
  for example a dict for a tool whose schema wraps a string, the Client gets a tool error
  naming the Hook, the tool, and what the schema expects, and the Daemon logs it.
- A Proxy file may `import helpers` to share code from `helpers.py` in the same Upstream
  directory. A helper is re-read whenever it changes, and one Upstream's helpers are never
  seen by another's Proxies.

### The trust model

Hooks and Virtual Tools run inside the Daemon process with no sandbox. Whatever your file
does, the Daemon does: read files, reach the network, import anything on the Python path. Two
things are not guarded against:

- A plain (sync) function runs in a worker thread, so one that blocks stalls only its own
  call. An `async` function runs on the Daemon's event loop, so one that blocks, with
  `time.sleep` or a blocking client, stalls every Proxy until it returns.
- `sys.exit` anywhere in user code, at load or in a Hook, is not caught.

Import errors, syntax errors, and exceptions while the file loads are contained: see below.

## Reload and health

Every file a Proxy serves from is watched: its Catalog, its Proxy file, its Python file, and
the helpers next to them. A change is served on the next request, for that Proxy alone, with
no Daemon restart. `mcpshape daemon reload` makes every Proxy re-read its files at once and
reports each one's health.

When a Python file cannot be loaded, or an Override or Cap cannot be applied, that Proxy is
marked unhealthy: it keeps advertising its last exposed tool set, so a Client with a cached
list is not confused, and every call, read, and get answers an error naming the Proxy and the
reason, for example `Proxy github/default is unhealthy: default.py, line 3: NameError: name
'x' is not defined`. Nothing reaches the Upstream meanwhile. Fix the file and the next request
serves it. `mcpshape daemon status` and `mcpshape ls` show which Proxy is unhealthy and why;
`mcpshape doctor` loads every Python file without starting anything and reports the same.

An Upstream that goes away does not take its Proxies down. The Proxy stays up; only calls
fail, with the Upstream's `unavailable_message`, until the Upstream is back. The connection is
opened on the first call and let go after `idle_timeout` seconds without one; `warm = true`
connects at Daemon start and pings on an interval, so a warm Upstream is never falsely
reported up. These are Upstream settings, in `upstream.toml`, with global defaults in
`config.toml`:

```toml
[lifecycle]
warm = false
idle_timeout = 600
connect_timeout = 10
ping_interval = 30
unavailable_message = "The Upstream is not reachable right now, so this call did not run. Nothing changed; try again in a moment."
```

## The Daemon

```
mcpshape daemon up        # run in the foreground; offers autostart the first time
mcpshape daemon status    # every Upstream's connection state and every Proxy's health
mcpshape daemon reload    # every Proxy re-reads its files now
mcpshape daemon logs      # the tail of the app log
mcpshape daemon down
mcpshape daemon install   # a launchd user agent (macOS) or a systemd user unit (Linux)
mcpshape daemon uninstall
```

The Daemon binds to `127.0.0.1:8321` by default. To bind elsewhere, `config.toml` must also
set a bearer token, which every request to a Proxy and to the management API must then carry;
mcpshape refuses an unguarded non-loopback bind. Every CLI command that edits or inspects
configuration works while the Daemon is down.

```toml
[daemon]
host = "127.0.0.1"
port = 8321
# token = "..."   # required for any other host
```

## Files and secrets

Configuration is hand-editable TOML under `~/.config/mcpshape/` (or `$XDG_CONFIG_HOME`, or
`--config-dir`, or `$MCPSHAPE_CONFIG_DIR`):

```
config.toml                       global settings: daemon, drift, lifecycle, caps
secrets.toml                      mode 0600; what ${VAR} resolves to when the environment has none
upstreams/github/upstream.toml    how the Upstream is reached, and its own lifecycle and Caps
upstreams/github/default.toml     the default Proxy's Overrides, Caps, name, instructions
upstreams/github/default.py       its Hooks and Virtual Tools
upstreams/github/review.toml      another Proxy
```

What mcpshape learned lives under `~/.local/state/mcpshape/`: Catalogs, Drift, encrypted
OAuth tokens, and `log/daemon.log`. Every file carries a `version`, and each TOML file starts
with a schema comment your editor's TOML extension uses to validate and complete it.

A secret never goes into a config file. Write `${GITHUB_TOKEN}` wherever a string goes in
`upstream.toml`, in a URL, a command argument, or an `env` value, and it is resolved when the
Upstream is reached: from the Daemon's environment first, then from `secrets.toml`. A resolved
value never appears in a log line, an error message, or the terminal.

```toml
# upstreams/github/upstream.toml
version = 1
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]

[env]
GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}"
```

A child process is given only a small set of the Daemon's environment (HOME, LOGNAME, PATH,
SHELL, TERM, USER) plus its `env` block, so anything the server needs goes in that block.

## Checking everything

```
mcpshape doctor
mcpshape doctor --for claude-code
```

`doctor` validates every config file against its schema, loads every Python file without
starting anything, reports Overrides and Hooks whose Catalog item vanished, and, with `--for`,
checks every exposed name and property name against the named Client's documented limits.
Nothing a Profile knows is enforced silently: it is reported, and you decide.
