# mcpshape

A local proxy that reshapes what MCP servers expose. Python, built on unmodified FastMCP 4.

## Read first
- `CONTEXT.md`: the glossary. Use its terms exactly; the Avoid lists are binding.
- `docs/DESIGN.md`: the design brief. Every settled decision and every rule lives there; the
  engineering rules, the workflow included, are under "Engineering".
- `docs/adr/`: the reasons behind the decisions that must not be undone without a new ADR.
- `docs/clients.md`: dated facts about Clients and FastMCP; update it, not the brief, when facts change.
- `docs/sessions/`: the verbatim design transcript. History, not rules; the brief wins where
  they differ.
- Spec: GitHub issue #2, the map; its sub-issues are the tickets. How a ticket is chosen,
  claimed, filed, and closed is in `docs/agents/issue-tracker.md`. Read the ticket before
  implementing it.

## Agent skills

### Issue tracker

GitHub Issues on `voidfreud/mcpshape` via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the root. See `docs/agents/domain.md`.
