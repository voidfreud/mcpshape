---
status: accepted
date: 2026-09-07
---

# Build on unmodified FastMCP, behind our own thin layer

FastMCP already provides proxying, tool transforms, visibility control, middleware, and every transport we need, so writing an MCP implementation ourselves would reinvent a maintained library. But FastMCP changed its public API twice in 2026 (2.x to 3.0 in February, 3.0 to 4.0 in August), so anything that binds directly to it is exposed to that churn.

We decided: depend on FastMCP as a normal, unmodified dependency pinned to a major version, and never fork or patch it. Every FastMCP touchpoint goes through a small internal layer, and user-facing surfaces (Hook and Virtual Tool decorators, config files, the management API) never expose FastMCP types. Only that layer changes when FastMCP does.

## Considered options

- **Fork and modify FastMCP.** Rejected: we would own their code, absorb every upstream change as a merge conflict, and turn updates into a standing job.
- **Implement MCP ourselves on the official SDK.** Rejected: redundant with FastMCP, and we would re-solve proxying, transforms, and transport edge cases they already handle.
- **Use FastMCP directly, no layer.** Rejected: user Hook files and our config schema would break on every FastMCP major.

## Consequences

- Features FastMCP does not offer (output-schema rewriting, for example) are out of scope until it does, rather than patched in.
- Upgrading a FastMCP major is a deliberate, tested change to the internal layer, not a routine bump.
- If a need ever forces a modification to FastMCP, the path is a contribution upstream, not a local patch.
