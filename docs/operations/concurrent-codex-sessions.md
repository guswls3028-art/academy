# Concurrent Codex session workflow

This is the owning Academy contract for local work that spans multiple Codex
tasks. It prevents shared dirty trees, accidental cross-session staging, stale
deployment sources, and abandoned worktrees without weakening the production
continuity gates.

## Standing task authority

Unless the user explicitly limits the task to local-only, no-deploy,
draft/PR-only, or read-only work, an assigned implementation, change, or build
includes the normal in-scope commit, push, PR, merge, messaging, deployment,
production verification, and residue cleanup steps. GitHub publication and
production deployment do not require a separate request. Release, operations,
and cleanup assignments carry the same standing authority. Do not pause for a
second approval at each step; record the exact source SHA, target, checks, and
readback instead. This does not broaden the task, make an ambiguous destructive
target safe, waive tenant or user-data protection, bypass an explicitly applicable change window or
continuity gate, or make an external approval true without platform readback.
An explicit instruction to deploy, release, apply to production, or continue a
specific rollout includes authority to submit that rollout's GitHub
`production` environment approval through the official authenticated API; do
not pause for a second confirmation. Require GitHub to record the approval
before mutation, and never remove the protection, approve another queued run,
or infer approval from the instruction alone. If GitHub rejects the review or
the configured identity is ineligible, preserve the error and report that
technical blocker without asking the user to repeat the same authorization.

Compatible patches do not wait for a default 04:00 slot. Use the current
[deployment timing and continuity policy](deployment-modes.md) and retain
technical holds until their actual release conditions pass. Historical task
windows and old automation snapshots are not current release evidence.

## Ownership model

- `C:\academy\backend` and `C:\academy\frontend` are canonical readback roots.
  Keep them on clean `main`; feature work does not edit them directly.
- Every task owns a unique lowercase session slug, branch, and worktree path.
  Cross-repository work uses the paired paths under
  `C:\academy\_worktrees\sessions\<session>\backend|frontend`.
- A worktree belongs to exactly one task. Other tasks may inspect it read-only
  but never stage, commit, reset, clean, rebase, or run generators in it.
- Exactly one task is the release owner. Other tasks stop at a reviewed branch
  or PR plus CI evidence and hand the exact commit SHA to the release owner.

The Git branch and registered worktree are the ownership record. Do not create
a parallel session registry, memory file, or shared scratch branch.

## Start a task

Choose a slug that combines the feature and a short task identifier, for
example `attendance-019fb78c`. From outside a target worktree run:

```powershell
pwsh C:\academy\backend\scripts\codex\session-worktree.ps1 `
  -Action Start `
  -Session attendance-019fb78c `
  -Repository both
```

`Start` fetches `origin/main` and creates a unique branch and worktree for each
selected repository. Use `backend`, `frontend`, or `both` to match the actual
scope. If a foreign dirty tree already exists, leave it untouched and still
start from current `origin/main` in the owned worktree.

Before editing, record the emitted base SHA and confirm the intended paths with:

```powershell
pwsh C:\academy\backend\scripts\codex\session-worktree.ps1 -Action Inspect
```

## Work and integration

- Run commands, generators, local servers, and tests only from the owned path.
  Use an explicit unique local port and point E2E at that server.
- Keep a cross-repository feature on paired branches with the same session slug.
- Before handoff, fetch and integrate current `origin/main` in the owned branch,
  resolve conflicts there, run focused checks, and report both exact SHAs.
- Never deploy a local-only commit, a dirty tree, a detached HEAD, or a branch
  that is not merged into current `origin/main`.
- A release owner records the chain `origin/main SHA -> candidate image digest
  or frontend version -> live runtime revision`. Backend deployment must also
  pass `scripts/v1/assert-production-source-freshness.ps1`; frontend deployment
  remains owned by `.github/workflows/quality-gate.yml` and its exact
  `GITHUB_SHA` readback.

## Finish or hand off

A task is not complete merely because existing changes were “preserved.” Pick
one explicit terminal state:

1. **Merged:** branch is contained in `origin/main`, or every remaining commit
   is patch-equivalent to `origin/main`; the worktree is clean and the session
   worktree and local branch are removed.
2. **Review pending:** clean committed branch/PR, named owner, exact SHA, and a
   stated merge or discard decision. It remains a worktree until resolved.
3. **Intentional WIP:** a named `wip/` branch with a recovery commit, owner, and
   next decision. It is never a deployment source. Raw uncommitted WIP is not a
   durable handoff state.

For the merged state, the close command performs a full preflight and refuses
dirty, foreign, or unmerged worktrees before deleting anything. A squash or
cherry-pick merge is accepted only when `git cherry origin/main HEAD` contains
no `+` commit; any unique commit preserves the branch:

```powershell
pwsh C:\academy\backend\scripts\codex\session-worktree.ps1 `
  -Action Close `
  -Session attendance-019fb78c `
  -Repository both
```

After active sessions and releases finish, fast-forward clean canonical roots:

```powershell
pwsh C:\academy\backend\scripts\codex\session-worktree.ps1 -Action Sync
```

`Sync` refuses dirty, non-`main`, or divergent canonical roots. It never resets,
rebases, force-checks out, or deletes user work.

`Close` validates integration against freshly fetched `origin/main`, not the
possibly stale canonical checkout. After that fail-closed preflight succeeds it
removes the exact session branch even when concurrent work intentionally keeps
the canonical `main` behind the remote.

## Verification

```powershell
pwsh scripts/codex/test-session-worktree.ps1
pwsh scripts/codex/session-worktree.ps1 -Action Inspect
git diff --check
git status --short
```

Final reporting lists canonical backend/frontend SHAs, the deployed revision or
digest when release work occurred, every remaining noncanonical worktree, and
the owner/decision for each unmerged or dirty branch.
Cross-repository product delivery also uses the fail-closed
[change-risk and release-bundle contract](change-risk-and-release-bundle.md) to
read the two official runs, pending approvals, backend manifest and lock, and
frontend live revision without inventing a shared Git transaction.
