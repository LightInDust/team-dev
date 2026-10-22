# Maintaining team-dev itself

This guidance applies to the skill's own source repository, not to business repositories handled by a team-dev run. The upstream repository is [LightInDust/team-dev](https://github.com/LightInDust/team-dev). Confirm the actual checkout, remote, branch and local changes before operating on it; a fork may have a different publishing destination.

## Trace a reported failure

Use the repository tree and relevant history alongside the failed run's preserved evidence. Establish which installed file versions produced the run before assuming the current repository reproduces it. Private installed copies can differ from the portable repository in documentation and local packaging; compare the relevant code and hashes explicitly.

Useful read-only inspections include:

```bash
git ls-tree -r --name-only <commit>
git log --oneline --graph --all
git log --follow -- path/to/file
git show <commit> -- path/to/file
git blame <commit> -- path/to/file
git diff <known-good>..<affected> -- path/to/file
```

Fetch the intended remote when current history is needed, preserving dirty work. Narrow history by the failing component, test or observable behavior; a nearby commit or blame attribution is a hypothesis, not proof of causation. Reproduce the affected boundary and retain the failing case before fixing it. Use an isolated checkout for old-version comparisons when needed; do not reset the live installed skill or rewrite a prepared run. Keep private runtime evidence outside Git, referring to it through local records rather than publishing raw logs.

## Publish a completed improvement

When the repository owner has given standing authorization, commit and push substantial validated improvements and fixes as part of their closeout without asking for the same permission again. Examples include behavior changes, execution/recovery fixes, acceptance or audit corrections, and material workflow improvements. Otherwise follow the current user's publication authorization. This document itself grants no permission to push another user's repository or a business target.

1. Reconcile the intended source change with the installed version and the repository. Preserve unrelated edits, working provider/proxy credentials and frozen timeout policies unless the current task expressly changes them.
2. Complete the checks and independent review appropriate to the change. Behavioral fixes need meaningful regression coverage; documentation-only edits need accurate instructions and valid links, not a ritual full runtime rerun. Do not publish a known failing fix as completed.
3. Review the exact staged diff and new commit metadata for disclosure. Exclude credentials, private endpoints/configuration, personal paths and server names, run history, raw logs, caches and private review artifacts. Retain clearly synthetic fixtures and necessary public protocol constants. Use the owner's public handle and privacy-preserving commit email.
4. Make a focused commit describing the problem, resulting behavior and relevant validation. A commit is a version marker, not evidence that a live provider, remote target or business task passed. Keep private verification receipts locally and bind them to the reviewed source or commit.
5. Push the authorized branch normally, preserving existing history. If the remote has advanced, inspect and reconcile the new state; do not force-push or erase other work. Revalidate affected changes after conflict resolution. Verify the remote branch points to the intended commit and report the commit ID. If push fails, report the local commit and the actual blocker rather than claiming publication succeeded.

Keep published source, locally installed files and run-specific snapshots distinct. Publishing does not automatically install changes into other sessions or migrate their configurations. The helper still does not commit or push target code; repository maintenance publication is a coordinator action within the owner's authorization.
