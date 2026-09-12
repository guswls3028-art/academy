# Academy Backend — Codex Instructions

This file is self-contained for Codex sessions started at the backend Git root.

## Sources of truth

- Documentation entry: `docs/README.md`
- Product/config policy: `docs/ssot/`
- Architecture: `docs/architecture/`
- Domain behavior: `docs/domain/`
- Operations and deployment: `docs/operations/`, `docs/infrastructure/`
- Current release: `docs/releases/README.md` `CURRENT`
- Executable deployment truth: `.github/workflows/` and `scripts/v1/`

Executable artifacts and current code/tests outrank prose. Plans, reports,
release history, and agent guidance are not current runtime evidence.

## Durable feature records

Every added, changed, removed, or replaced behavior must update its owning
current-state document in the same task. Use `docs/domain/` for product rules
and flows, `docs/architecture/` for system boundaries, `docs/infrastructure/`
for runtime topology, and `docs/operations/` for operator procedures. If no
owner exists, create one and link it from `docs/README.md`.

The record must recover purpose, actors and entry points, end-to-end flow,
invariants and permissions, data/API/event ownership, failure and retry
behavior, cross-repository dependencies, and focused verification. For a
removal or replacement, also record why, migration/compatibility behavior,
and the fate of existing data. Keep planned work in `docs/refactor/`; promote
implemented behavior into the current-state owner.

## Backend boundaries

- Resolve tenant at the request/job boundary and scope every business query.
  Missing or ambiguous tenant context hard-fails; no default tenant, hostname
  override, or cross-tenant fallback.
- Preserve manual, user-authored, approved content and protected references.
  Automated analysis enters proposal/review flow where one exists.
- Before delete, reset, recut, bulk rewrite, or storage cleanup, enumerate
  exact tenant/object targets and counts, exclude user-created data, confirm
  the operation is inside the assigned task, and verify post-state. Ask only
  when the exact target or scope cannot be resolved safely.
- Keep business decisions inside the owning domain/application boundary.
- Video, Messaging, AI, and Tools workers have separate queues and mutable
  state. Video encoding is AWS Batch only.
- Preserve correlation IDs and structured production logging on changed paths.

Messaging work must follow `docs/ssot/messaging-policy.md`; exact approved
owner templates are required and SMS/LMS fallback is forbidden. Matchup work
must follow `docs/domain/matchup.md` and preserve manual cuts and approvals.
Account/student work follows `docs/domain/parent-account.md`,
`docs/domain/student-core.md`, and `docs/domain/student-lifecycle.md`.

## Successful use and stabilization

Every generally available feature must retain an authorized user's ordinary
successful journey. A guard, disabled entry, support notice, swallowed error,
or empty-success fallback does not complete a bug fix. Only a feature labeled
Beta before entry may expose a documented incomplete path. Keep legitimate
tenant, permission, data, and messaging boundaries while repairing false
rejection or the failing operation.

Trace the action through its API, persistence, worker, and consuming screens.
Verify success, reload/downstream state, and visible failure with recovery;
denial tests or green CI alone are insufficient. For assigned structural cleanup,
check callers and compatibility before removing duplicate or wasteful code.
Use `docs/operations/change-risk-and-release-bundle.md` for audit evidence.
Read only relevant skills; do not duplicate their procedures or rerun passing
checks without changed inputs, a failure, or an unresolved risk.

## Verification

Run focused tests first, then as applicable:

```powershell
python manage.py check --settings apps.api.config.settings.test
python manage.py makemigrations --check --dry-run --settings apps.api.config.settings.test
python -m ruff check apps/ academy/
python scripts/lint/check_submission_lifecycle_boundary.py
python scripts/lint/refactor_boundary_snapshot.py --strict-touched
python -m pytest tests/test_smoke.py -v --tb=short -x
```

Finish with `git diff --check` and `git status --short`. Stage explicit files
only. Preserve pre-existing changes.

## Standing task authority

Unless the user explicitly limits the task to local-only, no-deploy,
draft/PR-only, or read-only work, an assigned implementation, change, or build
includes its normal in-scope commit, push, PR, merge, messaging, deployment,
production verification, and residue cleanup. Do not stop merely because
GitHub publication or production deployment was not requested as a separate
step; the implementation assignment itself authorizes the owning end-to-end
workflow. Release, operations, and cleanup assignments carry the same standing
authority. This authority does not expand task scope, resolve an ambiguous
destructive target, waive tenant or user-data protection, bypass a release
window explicitly applicable to this change or continuity gate, or make an external approval true without platform
readback. When the user explicitly instructs Codex to deploy, release, apply to
production, or continue an in-scope rollout, that instruction also authorizes
Codex to submit the exact rollout's GitHub `production` environment approval
through the official authenticated API without asking for a second
confirmation. The platform must record the approval before mutation; never
remove or bypass the protection, approve an unrelated run, or claim approval
from the instruction alone. If GitHub rejects the review or no eligible
authenticated reviewer is available, preserve the error and report the
technical blocker without asking the user to repeat the same authorization.

When the user says `모든권한`, `모든권한 있음`, `모든권한o`, or an equivalent
phrase, treat it as standing authorization for all otherwise-authorized,
in-scope actions until the user explicitly narrows or revokes it. Continue the
earliest unfinished assigned task before ancillary follow-up, and do not ask
again merely to reconfirm implementation, publication, release continuation,
optimization, monitoring, verification, or cleanup. This vocabulary does not
override a higher-priority safety or platform policy that explicitly requires
an action-time confirmation, nor does it resolve an unknown destructive target
or supply authorization that the external platform has not recorded.

## Concurrent task isolation

Keep canonical `C:\academy\backend` and `C:\academy\frontend` on clean `main`.
For any change, create a uniquely owned worktree from current `origin/main`
with `scripts/codex/session-worktree.ps1 -Action Start`; never share a
worktree or edit a foreign dirty tree. One task is the release owner and all
other tasks stop at an exact committed SHA plus CI evidence. A task closes only
after its branch is merged or fully patch-equivalent and its worktree is clean;
`-Action Close` refuses dirty, foreign, and uniquely unmerged worktrees. Use
`-Action Sync` only after active tasks and releases finish. The full lifecycle
and WIP handoff rules are in `docs/operations/concurrent-codex-sessions.md`.

Compatible patches have no default 04:00 deployment wait. The release owner
may promote promptly after existing gates prove old/new API and DB compatibility,
healthy rolling capacity, and no forced reload or interruption of active playback
and editing. Historical time windows and HOLDs apply only when still explicitly
in scope; unresolved interruption risk or incompatible changes need a separate
change window. See `docs/operations/deployment-modes.md` for the timing policy.
Verify affected boundaries and reuse applicable unchanged evidence; unrelated
frontend findings do not automatically hold a backend-only compatible patch.

## Mandatory preproduction and zero-downtime delivery

Current executable entry points are
`.github/workflows/quality-gate.yml`,
`.github/workflows/v1-build-and-push-latest.yml`, `scripts/v1/deploy.ps1`,
`scripts/v1/deploy-api-development.ps1`,
`scripts/v1/run-api-preprod-canary.ps1`, and `scripts/v1/verify.ps1`.

For every backend production release:

1. Build an immutable digest-pinned candidate; do not move `latest` yet.
2. Verify it on the persistent production-shaped development runtime with
   dedicated IAM, DB, queues, R2, Redis, and inbound-free SSM networking.
3. Require migration, production-resource denial, `/healthz`, `/health`,
   image identity, and synthetic Excel/PPT/R2 real-use smoke.
4. Publish a release-bound, versioned preprod env using the dedicated preprod
   DB role and credential. Replace production signing secrets and remove
   messaging, billing, external-AI, VAPID, and static AWS credentials before
   running the isolated preprod EC2 gate; CDN playback uses a separate
   `/academy/r2/preprod/credentials` read-only R2 key, never the production key.
5. Require migration, prod settings, exact DB name and role, denial of
   production DB CONNECT, exact env version/release ID, health, image identity,
   and signed CDN playback. Confirm termination.
6. Only then mutate production DB/runtime. Use backward-compatible
   expand/contract migrations while old and new instances overlap.
7. Replace through ASG/ALB health-gated rolling refresh with old instances
   retained until replacements are healthy.
8. Verify runtime digests, ALB/ASG, workers/queues, Batch, and affected user
   flows. Only after all verification may compatibility `latest` aliases and
   the successful release manifest be promoted.

Ordinary automation uses the repository GitHub OIDC role. Assigned manual
production work may use an already configured AWS account-root or
Cloudflare master credential, but must never print/copy its value or weaken
the development, preproduction, migration, rolling-health, or post-deploy
gates. Manual production mutation additionally requires a clean, exact latest
`main` checkout and a complete successful release manifest ancestor; never
bypass `assert-production-source-freshness.ps1`.
