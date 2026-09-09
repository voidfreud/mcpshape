# mcpshape

A local proxy that sits between MCP clients and the MCP servers they use, so the user can reshape what those servers expose, by hiding, renaming, trimming, rewriting, and adding tools, before a model ever sees it.

## Language

Each entry defines one concept and lists the words that must not name it. An Avoid word is banned as a name for that concept only; it stays usable in its plain sense, and in a Client's own words where the text describes what that Client documents.

**Upstream**:
An existing MCP server that the user adds once, local or hosted. Has one or more Proxies.
_Avoid_: the MCP, source server, backend, remote

**Proxy**:
One curation of an Upstream, exposed as its own local MCP server. Belongs to exactly one Upstream.
_Avoid_: profile, virtual server, endpoint, gateway, view

**Client**:
An application that connects to a Proxy and lets a model use it, such as Claude Code or Cursor.
_Avoid_: app, host, harness, consumer

**Client Profile**:
What mcpshape knows about one kind of Client: its limits, where its configuration lives, what transports it accepts, and what conveniences it offers. Profile, for short.
_Avoid_: integration, target, harness config

**Catalog**:
The set of tools, resources, and prompts an Upstream advertises, as last observed by mcpshape.
_Avoid_: manifest, inventory, tool list

**Override**:
A declarative per-item edit to how a Catalog item is presented to a Client: a new name, a new description, hidden or renamed arguments, or hidden entirely.
_Avoid_: transform, patch, rewrite, mapping

**Cap**:
The ceiling on how long a kind of text a Proxy exposes may be: names, descriptions, instructions, or tool output.
_Avoid_: limit, truncation, max length, budget

**Hook**:
User-written code that runs inside a Proxy on a request before it reaches the Upstream, or on a response before it returns to the Client.
_Avoid_: closure, middleware, interceptor, plugin, script

**Virtual Tool**:
A tool a Proxy exposes that has no counterpart in the Upstream's Catalog.
_Avoid_: custom tool, synthetic tool, composite tool

**Drift**:
Where the stored Catalog and what the Upstream advertises now disagree: items added, gone, or altered.
_Avoid_: diff, change, delta, update

**Daemon**:
The single long-running mcpshape process.
_Avoid_: server, service, agent, engine

**Keeper**:
The task the Daemon runs beside one Upstream's connection: the idle disconnect, the ping of a warm Upstream, and the retry when a backoff runs out.
_Avoid_: supervisor, watchdog, monitor, reconnector

**Exposed set**:
What a Proxy advertises to Clients: the accepted Catalog after Overrides and Caps, plus its Virtual Tools. An item's exposed name is what a Client sees; its Catalog name is its identity everywhere else.
_Avoid_: public tools, visible tools, alias

**Shim**:
The hidden `serve` command: speaks stdio to a Client, forwards to one Proxy over Streamable HTTP, and starts the Daemon if it is not running. For Clients that accept only stdio.
_Avoid_: bridge, wrapper, launcher

**Adapter**:
The one module that imports FastMCP and turns an Upstream with its Overrides, Caps, Hooks, and Virtual Tools into a running Proxy. Nothing else in mcpshape, and nothing a user writes, sees a FastMCP type.
_Avoid_: wrapper, internal layer, bridge
