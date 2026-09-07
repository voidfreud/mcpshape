---
status: accepted
date: 2026-09-07
---

# Client-agnostic core, with per-Client Profiles for limits and conveniences

Claude Code is the first Client we care about and the one with the most documented limits, so the obvious path is to design around it. We decided not to. The core of mcpshape (Upstreams, Proxies, Overrides, Caps, Hooks, Virtual Tools, the Daemon, the file formats) knows nothing about any particular Client and speaks only MCP. Everything Client-specific lives in a Client Profile: the Client's known limits (name lengths, description and instruction truncation, output size, tool counts), its config file location and format, its transport support, and any conveniences it offers. `proxy install` and `doctor` consult the Profile of the Client named on the command line, `upstream scan` searches every Profile's locations, and default Caps come from a Profile too. Claude Code, and later others, may get more conveniences through a richer Profile, never through special cases in the core.

## Considered options

- **Design around Claude Code, generalize later.** Rejected: its limits would leak into the core as magic numbers and the model would be wrong for every other Client, and each has different limits.
- **Ignore Client differences entirely.** Rejected: the whole point is fitting under each Client's limits, and those differ by an order of magnitude between Clients.

## Consequences

- A new Client is supported by adding a Profile, not by touching the core.
- Limits in Profiles are dated facts that go stale; nothing enforces them silently.
- Every user-visible default that depends on a Client (the default Cap for instructions, for example) must trace to a Profile entry with a source.
