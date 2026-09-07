# Domain docs

How a session uses this repo's domain documentation.

## Read before exploring

- `CONTEXT.md`: the glossary. Every term has a definition and an Avoid list; both bind everything
  a session writes: code, tests, issues, pull requests, commit subjects, and these docs.
- `docs/adr/`: one file per hard-to-reverse decision. Read the ADRs that touch the area you are
  about to work in: 0001 (unmodified FastMCP behind the Adapter), 0002 (a Proxy serves one
  Upstream), 0003 (Client-agnostic core with Client Profiles).

## Use the glossary's vocabulary

Name a concept the way `CONTEXT.md` does, in an issue title, a commit subject, a test name, or a
docstring, and never with a word from its Avoid list. A concept the glossary lacks is a gap:
raise it with `/domain-modeling` rather than inventing a name for it.

## Flag ADR conflicts

Output that contradicts an ADR says so instead of silently overriding it:

> _Contradicts ADR 0002 (a Proxy serves one Upstream), but worth reopening because…_

An ADR's decision is never edited; a decision that must change gets a new ADR that supersedes it.
