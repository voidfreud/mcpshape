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
  everything under `.github/`, `release-please-config.json`, and `.release-please-manifest.json`.
  `CONTEXT.md`, the design brief, the facts file, and the README are not rule files, so a wave
  changes them together with its code, or with its rule files.

Names and wording, in tickets, pull requests, code, and every Markdown file, follow `CONTEXT.md`.

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
- A `decision` ticket is resolved by the maintainer's decision, written as a comment by whoever
  records it, and closed; or it is relabelled `ready` and rewritten to be landable.
- A `wish` ticket is closed when it is declined or superseded, or relabelled when it gets a plan.
- A claim is released by unassigning: when a ticket drops out of a wave, or when it is found
  wrong while landing, in which case it is also relabelled `decision` with a comment saying why.
- A ticket assigned with no open pull request naming it is a stale claim; the next session asks
  the maintainer before taking it over.
- Follow-ups are filed as tickets before the wave's pull request merges, blockers linked. A
  follow-up is never left as a comment, a TODO, or a memory.
- A pull request from outside gets a ticket filed by the maintainer and assigned to its author;
  it then lands like any wave, or is closed with a comment saying why.

## Landing a wave

1. **Claim** every ticket of the wave. This is the session's first write to GitHub.
2. **Branch** from `main`: `<type>/<slug>`, the type from the list below, the slug lowercase
   letters, digits, and hyphens, at most 40 characters. One branch per wave.
3. **Implement** on the branch. No rule constrains the commits on the branch, their messages or their number; the
   pull request is what lands. Before review, when the wave changed code, `pyproject.toml`,
   `uv.lock`, `.python-version`, or CI's own workflow, the line below is green locally; a wave
   that changed anything else has nothing to run. Under the same condition, since no other
   workflow runs them, CI runs the same line with `HYPOTHESIS_PROFILE=ci`, more examples, then
   builds the package, installs the wheel, runs `--help` on it, and runs the tests again on
   Python 3.14. A release pull request runs the lock check and the build alone, its code being
   `main`'s. A `test` check that ran nothing reports green. The `rules` job runs on every pull
   request, the README check and the uv it installs for it included:
   `uv lock --check && uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run vulture src tests --min-confidence 80 && uv run pytest`
4. **Review** on the branch, before the pull request opens, by a reviewer that did not write
   the code, on the whole diff:
   - spec: each ticket's acceptance criteria, met or not, with evidence; and nothing built that
     no ticket or finding asked for;
   - standards: this file, `CONTEXT.md` (terms and Avoid lists, in names and in text), and,
     as `CLAUDE.md` names them, the design brief (a decision the diff breaks, or changes without
     editing the brief), the facts file (a fact the diff rests on that it does not date), and
     the README (a command the diff removed or changed); duplication;
   - rules, when a rule file changed: every rule once, no two in conflict, none ambiguous, none
     unenforced that could be, and the skills that execute this file still agree with it;
   - adversarial, when code changed: every way the diff can be made to do what a ticket or the
     brief says it must not, each with the inputs or the sequence that does it, the criterion or
     decision it contradicts, and the test that would pin it; a finding without that sequence is
     not one and is dropped, and a confirmed one, reproduced by the session, lands with its test
     in the same wave, or as a follow-up when a ticket of the wave rules that out.
   Every finding is fixed on the branch or answered; none is deferred. The session landing the
   wave reads the riskiest file itself, whoever wrote it.
5. **File follow-ups** for everything seen and not fixed.
6. **Pull request**: title `<type>: <subject>`, the type the branch's, at most 72 characters, no
   trailing period. Body
   from the template, every section in order, then one `Closes #<n>` line per ticket; a wave that
   closes no ticket has none, and neither the title nor any other line puts a closing word before
   a ticket reference, any `#<n>`, since the squash commit is the title and the body and GitHub
   closes on that pattern wherever it stands. Rule files and code never change in one pull
   request.
7. **Checks**: wait for the CI run of the pushed head as its own step after the push has
   succeeded. A red check is fixed on the branch and pushed. When `main` has moved, the branch
   is brought up to date with `gh pr update-branch <n>`; a session never force-pushes.
8. **Merge** by squash once every check is green, by the maintainer, or by the session once the
   maintainer has said so, at the start of the wave or after its report: `gh pr merge <n> --squash`.
   The squash commit's subject is the title and its body is the pull request body. The branch
   is deleted.
9. **Close**: the merge closes the tickets. If a parent's last sub-issue just closed, close the
   parent. Nothing else is written to any ticket.

Types: `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style`,
`test`.

## Bots

Two bots open pull requests of their own. A bot's pull request is not a wave: nothing is
claimed, branched, reviewed, or filed for it, and it closes no ticket. The `rules` job exempts
its branch, `dependabot/*` or `release-please--*`, from the branch, type, body-section,
closing-word, `Closes`, and release-please-file checks and from nothing else; every other check
applies, the `test` job scoping itself as step 3 says, and it merges green and up to date like
any pull request, which the workflows below keep it. They alone write to a bot's branch.

- Dependabot, weekly and grouped: a minor or patch update merges by itself once its checks are
  green; a major update opens as its own pull request and stays open until the maintainer
  decides. On every push to `main`, every open Dependabot pull request is asked to rebase, so
  its checks rerun on the new `main`.
- release-please keeps one release pull request open whenever an unreleased commit of a type its
  changelog shows exists, `feat`, `fix`, `perf`, `revert`, or `docs`, holding the version bump
  and the changelog; commits of the other types ship inside the next such release. The version
  is what those commits compute, unless the maintainer names one: a rules wave sets `release-as`
  in the release config, and a wave removes it once that release exists, or when the maintainer
  withdraws the name. The changelog begins at the commit the config names as `bootstrap-sha`,
  the one that put the workflow in this file, read only until the first release exists; what
  came before is in the tracker. On every push to `main`, the `Release` workflow merges `main`
  into the release pull request when it is behind, since release-please rebuilds it only when
  the changelog changes; a merge that conflicts fails the run and says so, and the next rebuild
  resolves it. When release-please opened or updated the pull request, the workflow also
  refreshes the lock on its branch, since the lock records the version, pushing with the bot
  token so the checks rerun. The maintainer merges the pull request, or tells a session to. The
  merge creates the tag `vX.Y.Z` and the GitHub release, and the `Publish` workflow ships that
  release to PyPI, then bumps the formula of the Homebrew tap `voidfreud/homebrew-mcpshape` to
  that release: the `url` and `sha256` of the source distribution as PyPI records them, pushed
  to the tap with the bot token; the formula's resources are changed by hand, with the lock
  change that causes them. The failed job of a `Publish` run is rerun from the Actions page,
  since its upload does not repeat. `CHANGELOG.md`,
  `.release-please-manifest.json`, and the version in `pyproject.toml` are written by
  release-please alone, and its two `autorelease:` labels are its own, on its pull requests
  only.

The bots' workflows and the tap bump run with the `BOT_TOKEN` secret, a fine-grained token
with contents and pull requests read and write on this repository and contents read and write
on the tap, since anything done with GitHub's own token triggers no workflow: a pull request
it opened gets no checks, and a merge it performed runs nothing on `main`. Without the secret
the bots' workflows do nothing and fail nothing, and `Publish` ships to PyPI and skips the tap.

## Sessions and agents

- One session lands a wave and is responsible for it. Subagents work in the same working tree,
  or in detached worktrees created from the wave's branch, never from `main`; the lead folds
  their commits onto the wave's branch. No per-thread branch is pushed; no per-ticket pull
  request is opened.
- Nothing is done on GitHub outside the steps above. Anything else is asked first.
- The skills `/ticket`, `/land`, `/review`, and `/scaffold` are the executable form of this
  file. They live in the maintainer's global skills directory, not in this repository, and hold
  steps, not rules; where a skill and this file differ, this file wins and the skill is fixed in
  the same wave.

## Records

The pull request is the record of a wave: what changed, how it was tested, what review found.
GitHub links each closed ticket to it. Nothing is written a second time on the ticket or
anywhere else. A dated fact is recorded in the facts file `CLAUDE.md` names; a decision that
changes what the design brief holds is written into the brief by the wave that changes it.
The README shows only what exists. A tag `baseline-<date>` marks a state the maintainer wants
to find again; a tag `vX.Y.Z` is a release.

## Enforced

| Rule | Where |
| --- | --- |
| Pull requests only; squash only; linear history; every check green and up to date; no bypass | ruleset `protect-main` |
| Squash subject is the title, body is the body; branch deleted on merge | repository settings |
| Branch is `<type>/<slug>`; title is `type: subject` with the branch's type, at most 72 characters, no trailing period; a bot's pull request is exempt from the branch, type, body-section, closing-word, `Closes`, and release-please-file checks | CI, job `rules` |
| Body has the template's sections in order; `Closes #<n>` when code changed, and no closing word before a ticket reference in the title or elsewhere; every `Closes` ticket is open, `ready`, assigned to the author, and not a parent; rule files and code never in one pull request; release-please's files written by release-please alone | CI, job `rules` |
| Every command the README shows exists | CI, job `rules` |
| Ticket sections and labels, checked on every open and edit, one comment until they pass | CI, workflow `Issue` |
| Lint, format, types, lock file, dead code; tests on Python 3.12 and 3.14; the package builds, its wheel installs, and `--help` runs on it; green having run nothing when nothing they test changed | CI, job `test` |
| Dependabot's minor and patch updates merge by themselves when green, and every open one is asked to rebase on each push to `main` | CI, workflow `Bots` |
| `release-as`, when set, names a version above the last release | CI, job `rules` |
| The release pull request exists and is brought up to date with `main` on every push to it; merging it tags, releases, and publishes | CI, workflows `Release` and `Publish` |
| The tap's formula names the release `Publish` shipped | CI, workflow `Publish` |
