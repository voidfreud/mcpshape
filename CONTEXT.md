# mcpshape

A local proxy that sits between MCP clients and the MCP servers they use, so the user can reshape what those servers expose, by hiding, renaming, trimming, rewriting, and adding tools, before a model ever sees it.

## Language

**Upstream**:
An existing MCP server that the user adds once, as shipped by an app or a service. Has one or more Proxies.
_Avoid_: the MCP, source server, backend, remote

**Proxy**:
One curation of an Upstream, exposed as its own local MCP server. Belongs to exactly one Upstream.
_Avoid_: profile, virtual server, endpoint, gateway, view

**Client**:
An application that connects to a Proxy and lets a model use it, such as Claude Code or Cursor.
_Avoid_: app, host, harness, consumer

**Client Profile**:
What mcpshape knows about one kind of Client: its limits, where its configuration lives, what transports it accepts, and what integration conveniences it offers.
_Avoid_: adapter, integration, target, harness config

**Catalog**:
The set of tools, resources, and prompts an Upstream advertises, as last observed by mcpshape.
_Avoid_: manifest, inventory, tool list

**Override**:
A declarative per-item edit to how a Catalog item is presented to a Client: a new name, a new description, hidden or renamed arguments, or hidden entirely.
_Avoid_: transform, patch, rewrite, mapping

**Cap**:
A maximum length for a kind of text a Proxy exposes: names, descriptions, instructions, or tool output.
_Avoid_: limit, truncation, max length, budget

**Hook**:
User-written code that runs inside a Proxy on a request before it reaches the Upstream, or on a response before it returns to the Client.
_Avoid_: closure, middleware, interceptor, plugin, script

**Virtual Tool**:
A tool a Proxy exposes that has no counterpart in the Upstream's Catalog.
_Avoid_: custom tool, synthetic tool, composite tool

**Drift**:
The difference between the stored Catalog and what the Upstream advertises now.
_Avoid_: diff, change, delta, update

**Daemon**:
The single long-running mcpshape process.
_Avoid_: server, service, agent, engine
