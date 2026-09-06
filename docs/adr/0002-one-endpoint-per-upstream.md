---
status: accepted
date: 2026-09-07
---

# One endpoint per Upstream, never a merged endpoint

Nearly every MCP gateway merges all upstream servers into a single endpoint with namespaced tool names, and that was also the first idea for this project: one Proxy holding every tool you want, the only MCP server ever installed in a Client.

We decided against it. Each Upstream gets its own Proxy endpoints, and mcpshape never merges Upstreams. The first reason, stated at the start of the project, is compatibility: a merged endpoint "would not be recognized by a lot of applications and it would defeat the purpose". Clients expect one server per configured entry, and a Client's per-server controls (enable, disable, permissions, trust) only work if each Upstream is its own entry. The second reason is budgets. Clients impose per-server budgets that a merged endpoint spends all at once. Claude Code defers tool definitions and shows the model only tool names plus each server's instructions, and truncates each server's instructions at 2KB, so N servers give N routing hints and one merged server gives a single 2KB blob for everything; it names tools `mcp__<server>__<tool>` inside a 64-character API limit, so one shared namespace eats the name budget with prefixes; and its permission rules are per server and tool, so one merged server is one coarse allow/deny surface. Some Clients cap the tools visible in a session (see `docs/clients.md`). Every Client has its own budgets and most are undocumented, so packing tools into one endpoint cannot be reasoned about case by case. Separate endpoints keep each Proxy small, curated for one purpose, individually permissioned, and individually failing: one dead Upstream takes down only its own Proxies. The trust boundary a Client sees maps one to one onto the Upstream behind it. This is also why Caps and Client Profiles exist: they keep every Proxy under whatever the target Client tolerates.

## Consequences

- Composing tools across Upstreams is not a feature. Cross-Proxy calls from user code are parked for the same reason.
- Curation happens per Proxy; the same Upstream can have several Proxies for different Clients or purposes.
