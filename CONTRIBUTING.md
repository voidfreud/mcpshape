# Contributing

How work lands in this repository, from ticket to merge. Every rule is stated here once. The
rules a machine can check are enforced by CI, the `main` ruleset, and the repository settings,
and the table at the end says which. These rules change only by a pull request that edits this
file; a remark in a session changes nothing.

## Words

- **Ticket**: a GitHub issue that describes one unit of work, a bug or an enhancement.
- **Parent**: a ticket that has sub-issues; they are the tickets of one feature. Its body is
  the plan, edited in place.
- **Wave**: one pull request's worth of work: the tickets it closes, if any. Usually one;
  several only when they touch the same code and would conflict if landed apart.
- **Landing**: taking a wave from claim through the closing of its tickets.
- **Claim**: assigning a ticket to yourself. An open, unassigned ticket is unclaimed.
- **Follow-up**: a ticket filed for something seen while landing a wave that the wave does not fix.
- **Frontier**: the tickets that can be landed now: open, `ready`, unblocked, unassigned, not a parent.
- **Code**: anything under `src/` or `tests/`. **Rule files**: `CONTRIBUTING.md`, `CLAUDE.md`,
  and everything under `.github/`.

Names and wording, in tickets, pull requests, code, and docs, follow `CONTEXT.md`.

## Tickets

- A ticket carries exactly one category label and, while open, exactly one state label.
  - Category: `bug` (something is wrong) or `enhancement` (something new or better). A parent's
    category is its feature's.
  - State: `ready` (fully specified, anyone can land it; on a parent, the plan is settled and its
    sub-issues can be landed), `decision` (waits on the maintainer), `wish` (wanted; no plan).
  - A ticket enters as `decision`, unless the filer could land it from the body alone, then
    `ready`. The issue forms enter `decision`.
- The title is one sentence stating the behaviour: for a bug, what is wrong; for an
  enhancement, what will be true.
- The body has two sections, in this order and with these headings, as the issue forms write them:
  - `### What`: the behaviour from the user's side. For a bug, what happens and what should.
    For a parent, the feature and its plan.
  - `### Acceptance criteria`: checkboxes a reviewer can tick. For a parent, the one line
    `Every sub-issue closed.`; GitHub lists the sub-issues itself.
- Blocking is GitHub's native blocked-by relation, added when the ticket is filed and shown on
  it. A ticket is unblocked when every blocker is closed. A follow-up that blocks a ticket of the
  wave being landed is fixed in that wave, or the ticket is dropped from the wave.
- A milestone names the release a ticket is for. A ticket with no milestone is not scheduled.
- A `ready` ticket is closed by the merge that lands it; a `ready` parent by the session that
  lands its last sub-issue. Nothing else closes either, and a ticket closed any other way is
  reopened.
- A `decision` ticket is resolved by the maintainer: the decision is written as a comment and the
  ticket is closed, or it is relabelled `ready` and rewritten to be landable.
- A `wish` ticket is closed when it is declined or superseded, or relabelled when it gets a plan.
- A claim is released by unassigning: when a ticket drops out of a wave, or when it is found
  wrong while landing, in which case it is also relabelled `decision` with a comment saying why.
- A ticket assigned with no open pull request naming it is a stale claim; the next session asks
  the maintainer before taking it over.
- Follow-ups are filed as tickets before the wave's pull request merges, blockers linked.
  Nothing is left in a comment, a TODO, or a memory.
- A pull request from outside gets a ticket filed by the maintainer and assigned to its author;
  it then lands like any wave, or is closed with a comment saying why.

## Landing a wave

1. **Claim** every ticket of the wave. This is the session's first write to GitHub.
2. **Branch** from `main`: `<type>/<slug>`, the type from the list below, the slug lowercase
   letters, digits, and hyphens, at most 40 characters. One branch per wave.
3. **Implement** on the branch. Tests drive the system through the seam `docs/DESIGN.md`
   describes. No rule constrains the commits on the branch, their messages or their number; the
   pull request is what lands. Before review, the commands CI's `test` job runs are green locally:
   `uv lock --check && uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run vulture src tests --min-confidence 80 && uv run pytest`
4. **Review** on the branch, before the pull request opens, by a reviewer that did not write
   the code, on the whole diff:
   - spec: each ticket's acceptance criteria, met or not, with evidence; and nothing built that
     no ticket asked for;
   - standards: this file, `CONTEXT.md` (terms and Avoid lists, in names and in text),
     `docs/DESIGN.md` and the ADRs, the test seam, duplication;
   - rules, when a rule file changed: every rule once, no two in conflict, none ambiguous, none
     unenforced that could be.
   Every finding is fixed on the branch or answered; none is deferred. The session landing the
   wave reads the riskiest file itself, whoever wrote it.
5. **File follow-ups** for everything seen and not fixed.
6. **Pull request**: title `<type>: <subject>`, at most 72 characters, no trailing period. Body
   from the template, every section in order, then one `Closes #<n>` line per ticket; a wave that
   closes no ticket has none. Rule files and code never change in one pull request.
7. **Checks**: wait for CI as its own step after the push has succeeded, with
   `gh pr checks <n> --watch`. A red check is fixed on the branch and pushed. When `main` has
   moved, the branch is brought up to date with `gh pr update-branch <n>`; nothing is ever
   force-pushed.
8. **Merge** by squash once every check is green: `gh pr merge <n> --squash`. The squash
   commit's subject is the title and its body is the pull request body. The branch is deleted.
9. **Close**: the merge closes the tickets. If a parent's last sub-issue just closed, close the
   parent. Nothing else is written to any ticket.

Types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`,
`test`.

## Sessions and agents

- One session lands a wave and is responsible for it. Subagents work in the same working tree,
  or in detached worktrees created from the wave's branch, never from `main`; the lead folds
  their commits onto the wave's branch. No per-thread branch is pushed; no per-ticket pull
  request is opened.
- Nothing is done on GitHub outside the steps above. Anything else is asked first.
- The skills `/ticket`, `/land`, `/review`, and `/scaffold` are the executable form of this
  file. They live in the maintainer's global skills directory, not in this repository, and hold
  steps, not rules; where a skill and this file differ, this file wins and the skill is fixed.

## Records

The pull request is the record of a wave: what changed, how it was tested, what review found.
GitHub links each closed ticket to it. Nothing is written a second time on the ticket or
anywhere else. A dated fact about a Client or FastMCP goes in `docs/clients.md`; a
hard-to-reverse design decision gets an ADR in `docs/adr/`.

## Enforced

| Rule | Where |
| --- | --- |
| Pull requests only; squash only; linear history; every check green and up to date; no bypass | ruleset `protect-main` |
| Squash subject is the title, body is the body; branch deleted on merge | repository settings |
| Branch is `<type>/<slug>`; title is `type: subject`, at most 72 characters, no trailing period | CI, job `rules` |
| Body has the template's sections in order; `Closes #<n>` when code changed; every `Closes` ticket is open, `ready`, assigned to the author, and not a parent; rule files and code never in one pull request | CI, job `rules` |
| Ticket sections and labels, checked on every open and edit, one comment until they pass | CI, workflow `Issue` |
| Lint, format, types, tests, lock file, dead code | CI, job `test` |
