# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies. Every issue in this repo is a child ticket of the spec issue (#2) and goes through the ticket operations below: linked, edged, labelled.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this automatically when run inside a clone.

## Pull requests

One pull request per wave: one issue, or a group of issues, with their review commits. The rule is the brief's, under Engineering; this is how a pull request looks here.

- **Branch**: one per wave, off `main`, named `<type>/<slug>`. Brought up to date by rebasing onto `main`, never by merging `main` in. Every commit on it is a Conventional Commit, `type: subject`, and none of them is a merge commit. CI refuses a pull request whose branch name, title, or commits break this (the commit rules are the brief's).
- **Title**: Conventional Commit form, `type: subject`, the subject in the glossary's words.
- **Body**: the pull request template (`.github/pull_request_template.md`), every section kept in its order, then `Closes #<n>` for every ticket the wave completes, none for a wave that changes only rules or docs; CI refuses a pull request that changes `src/` or `tests/` without one (the rule is the brief's). A reader who sees only the pull request knows what was built and how it was checked.
- **Merge**: only once every CI check is green, with `gh pr merge <n> --merge --subject "<type>: <subject> (#<n>)"`, which is also what GitHub's merge button writes, since the repository's default merge subject is the pull request title. The branch is deleted on merge.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(`/triage` reads this flag; set it to `yes` only if external pull requests are ever treated as feature requests.)_

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Ticket operations

Used by any session that creates, claims, files, or closes an issue. The spec issue (#2) is the parent; its GitHub sub-issues are the tickets.

- **Ticket**: an issue linked to the spec issue as a GitHub sub-issue: `gh api --method POST repos/<owner>/<repo>/issues/2/sub_issues -F sub_issue_id=<db-id>`, where `<db-id>` is the new issue's numeric database id (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, not the `#number` or `node_id`). Labels: a triage label from `docs/agents/triage-labels.md` (`ready-for-agent` once fully specified), plus `bug` or `enhancement` on a follow-up filed while landing a wave. The spec issue carries no label. Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's native issue dependencies, visible in the UI. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, the blocker's database id as above. GitHub reports `issue_dependencies_summary.blocked_by`, open blockers only, the live gate. A ticket is unblocked when every blocker is closed.
- **Frontier query**: the spec issue's open sub-issues in their order (`gh api repos/<owner>/<repo>/issues/2/sub_issues --jq '.[] | select(.state=="open") | .number'`), dropping any with an open blocker (`gh api repos/<owner>/<repo>/issues/<n> --jq .issue_dependencies_summary.blocked_by` above 0) or an assignee; the first that remains wins. `gh issue view` lists a ticket's blockers whether open or closed, so it is not the gate; the summary field is.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **File**: follow-ups observed while landing a wave (the rule is the brief's, under Engineering): created, linked, edged, and labelled as above, before the wave's pull request merges.
- **Resolve**: the wave's pull request body says `Closes #<n>` for every ticket it completes, and merging it green is what closes them (the rule is the brief's, under Engineering). A closed issue unblocks its dependents in the frontier query, so a ticket is never closed by hand before its merge. After the merge, `gh issue comment <n> --body "<answer>"` naming the commits it landed in.
