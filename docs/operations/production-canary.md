# Production Canary

Read-only production canary for release closure and preventive operations.

## Backend DB Invariants

Run inside the API container:

```powershell
python manage.py production_canary --tenant-id 1 --tenant-code hakwonplus --indent 2
```

The command fails on critical user-facing risks:

- enabled autosend configs without an implemented trigger or approved effective template
- READY videos missing HLS output
- stale active video jobs (`QUEUED`, `RUNNING`, or `RETRY_WAIT`)
- `Video.current_job` pointing at a different tenant or video row
- old UPLOADED or PROCESSING videos without a same-tenant active transcode job
- explicit E2E, AUDIT, or CHAOS residue in the production tenant
- recursive message-template copy chains (at least 10 identical copies across
  five or more `복사 -` depths) and strict timestamped staff audit fixtures
- auto billing enabled without a matching live Toss client/server key pair
- auto billing enabled without encrypted billing-key writes and a primary KEK

## Explicit E2E residue cleanup

`production_e2e_residue_absent` is release-blocking. Do not suppress the
finding or delete rows directly. First enumerate the exact tenant-scoped
targets:

```powershell
python manage.py cleanup_e2e_residue --tenant-id 1 --dry-run --limit 100
```

The dry-run prints every matched ID, relationship summary, total, and an
exact-target confirmation token. Review the list and then execute only against
that unchanged target set:

```powershell
python manage.py cleanup_e2e_residue --tenant-id 1 --execute --confirm-token <dry-run-token>
```

The execute path fails closed when the token is stale, a matched student is
still active, a message template is a default or is referenced by an
auto-send configuration, a matched staff row has an account, profile photo,
payroll history, or linked teacher row, a Matchup document contains a
manual/owner-pinned problem, or a Matchup report contains authored state.
Recursive template cleanup requires at least ten byte-for-byte equivalent
copies spanning five copy-prefix depths; it preserves every unprefixed root
and smaller user-created copy sets. Timestamp-marked E2E
lectures are eligible only when every child session has the same strict
automation marker. This includes the legacy OMR real-use fingerprint
`[E2E-OMR[-PURPOSE]-YYYYMMDDhhmmss]`; the 14-digit timestamp and uppercase
automation label are both required, so `[E2E-OMR]` or date-only names never
match. Lowercase automation labels are accepted only with a 10-digit-or-longer
execution timestamp, such as `[E2E-cut-fix-1778777196]`. Three legacy fixed
community titles are exact-match allowlisted; similar natural-language titles
remain protected. A marked lecture or isolated marked session is deleted
only after the ordinary product deletion guard proves that no enrollment,
attendance, progress, clinic, video, or other user history remains. If a child
exam exists, the exam itself must be in the same strict residue target set;
otherwise cleanup fails before external-storage or database mutation. Matched
exam results, submissions, clinic links, and their tenant/exam-scoped OMR upload
batches and items are removed before the lecture guard is retried. The dry-run
and confirmation token include the exact submission, batch, and item IDs. The
execute path refuses a batch item that points outside that exact exam submission
set. Original and aligned OMR scan objects must use the owning
`tenants/<tenant>/ai/submissions/<submission>/` prefix, must not be shared by a
different submission, and are deleted with absence readback before their DB
rows. An otherwise orphaned score-edit draft is the sole non-exam
blocker the cleanup may remove before retrying that guard. Soft-deleted students
use the official
permanent lifecycle service so their tenant membership and orphaned account
are handled consistently. Community attachments and Matchup
inventory/source/crop/page objects are removed from R2 and read back as absent
before the database transaction removes their rows.

After execution, rerun the dry-run and the PostDeploy production canary. Both
must report zero residue. Never broaden the matcher to natural-language names
such as `테스트학생`; only timestamped automation fingerprints are eligible.

Warnings are emitted for operational debt that should be reviewed but may be accepted temporarily:

- overdue or failed messaging jobs
- stale messaging worker heartbeat (Messaging has a warm baseline; stale heartbeat is acceptable only when the wrapper proves an intentional maintenance window or temporary ASG scale-down)
- READY videos missing thumbnails
- READY videos still tied to an active transcode job
- recent DEAD video jobs
- fee management feature flag enabled without explicit allowlist
- billing date gaps, due AUTO_CARD invoices while auto billing is off, old pending transactions

Use `--fail-on-warning` for conservative release sealing.

`READY videos missing thumbnails` checks whether the database
`thumbnail_r2_key` invariant is present. It does not prove that the serializer
returns `thumbnail_url`, that the signed CDN object is reachable, or that a
frontend card decoded and rendered the image. For a reported display failure,
continue with the layered procedure in
[video-batch.md](runbooks/video-batch.md#51-thumbnail-not-visible-triage).

## One-Command Wrapper

From the backend repository:

```powershell
pwsh -File scripts/v1/run-production-canary.ps1 -AwsProfile default
pwsh -File scripts/v1/run-production-canary.ps1 -Mode PostDeploy -AwsProfile default -WriteReport
```

The wrapper checks:

- public API and frontend HTTP edge
- API plain HTTP redirects to HTTPS before reaching Django
- tenant-scoped program API health (`2xx` only)
- API and worker ASGs
- ALB target health
- RDS and Redis state
- SQS queue and DLQ depth, failing closed if AWS CLI/SQS permissions/queue lookup fail
- CloudWatch service alarms
- remote Django checks on the live `academy-api` container
- video Batch queue/compute environment validity in `PostDeploy` and `Deep`

`PostDeploy` and `Deep` modes treat warnings as release-blocking and run remote Django checks on every healthy InService API instance.

Use `-Json` when automation needs machine-readable output. In JSON mode the wrapper suppresses progress logs and writes only one JSON payload to stdout.

This script is read-only. It does not mutate AWS resources or production data, aside from normal SSM command history.
