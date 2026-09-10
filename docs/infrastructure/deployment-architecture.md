# V1.1.0 Deployment Architecture

**Version:** V1.1.0
**Date:** 2026-03-14 (checked 2026-08-05)
**SSOT Status:** Active

## 1. Service Decomposition

| Service | ECR Repository | ASG | Container Name | Purpose |
|---------|---------------|-----|----------------|---------|
| API | academy-api | academy-v1-api-asg | academy-api | Django REST API (Gunicorn) |
| Messaging Worker | academy-messaging-worker | academy-v1-messaging-worker-asg | academy-messaging-worker | SQS message processing |
| AI Worker | academy-ai-worker-cpu | academy-v1-ai-worker-asg | academy-ai-worker-cpu | AI task processing |
| Tools Worker | academy-tools-worker | academy-v1-tools-worker-asg | academy-tools-worker | deterministic document/PDF/PPT/spreadsheet conversion jobs |
| Video Worker | academy-video-worker | AWS Batch CE (`academy-v1-video-batch-ce-200gb`, c6g.4xlarge primary) | — | 영상 인코딩. 1 video = 1 Batch job. VCPU=8 / MEM=16GB / timeout=6h |
| Base | academy-base | — | — | Shared base image for all services |

**Note (2026-05-10, checked 2026-06-23):** Daemon mode 폐기. 모든 영상 인코딩은 AWS Batch standard queue/jobdef(`academy-v1-video-batch-queue`, `academy-v1-video-batch-jobdef`)로 1-shot 처리한다. long path는 폐기되었고, 실패/중단 복구용 ops 작업은 별도 ops queue/jobdefs(`academy-v1-video-ops-*`)로 관리한다. 현재 jobdef timeout은 6h이며, 실패/중단 케이스는 recover/reconcile/scan_stuck 계열이 재시도한다. ffmpeg는 `c6g.4xlarge` VCPU=8 + R2 병렬 업로드로 처리한다.

## 1.1 Public API Edge

- `api.hakwonplus.com`은 Cloudflare 프록시가 아니라 DNS-only CNAME으로 `academy-v1-api-alb`에 직접 연결한다.
- Public HTTPS는 ALB 443 listener가 ACM 인증서 `api.hakwonplus.com`으로 종료하고, listener 기본 action은 `academy-v1-api-tg` forward다.
- ALB 80 listener는 `HTTPS:443`으로 redirect한다. 운영 사용자/테스트 기준 API URL은 `https://api.hakwonplus.com`이며, plain HTTP가 Django까지 도달하면 drift로 본다.
- Cloudflare zone SSL mode는 Strict로 유지한다. API 레코드를 다시 proxied로 돌릴 때는 ALB HTTPS 443과 origin 검증을 먼저 확인한다.
- 운영 Django는 `172.30.0.0/16`만 신뢰 프록시로 인정한다. ALB 기본 append 형식의 X-Forwarded-For를 오른쪽부터 검사해 외부 요청이 넣은 선행 값을 무시하며, 감사 로그·공개 폼·내부 API IP 정책·로그인 제한이 같은 resolver를 사용한다.
- 로그인 제한은 LocMemCache가 아니라 RDS의 HMAC 버킷을 사용한다. 실제 IP는 분당 60회, tenant+로그인 계정은 5분당 10회로 API 인스턴스와 배포 재시작을 가로질러 공유하며 계정/IP 원문은 저장하지 않는다. 버킷 소비는 `POST /api/v1/token/`에만 적용하고, GET 등 지원하지 않는 메서드는 버킷 쓰기 전에 `405`로 거부한다.
- SSOT 및 재현 스크립트: `docs/ssot/params.yaml`의 `api.acmCertificateArn`/`api.httpsSslPolicy`, `scripts/v1/resources/alb.ps1`의 `Ensure-Listener`/`Ensure-HttpsListener`.

## 2. CI/CD Pipeline Architecture

```
git push main
    |
    v
[detect-changes] ─── analyze git diff ──> outputs: build_api, build_video,
    |                                               build_messaging, build_ai,
    |                                               build_tools, force_full
    v
[run-lint] ─── ruff + submission lifecycle + refactor boundary deploy gates
    |
    v
[run-tests] ─── smoke tests deploy gate
    |
    v
[prepare-build] ─── build/resolve immutable academy-base digest
    |
    v
[build-runtime-images] ─── 5-service matrix, changed images build in parallel
    |                       └── each entrypoint import + ECR immutable image
    v
[build-and-push] ─── assemble exact six-image candidate manifest
    |                 └── scan complete + accepted Critical only + exact known High set
    |
    v
[verify-api-development] ─── persistent production-shaped EC2
    |                         + dedicated IAM/DB/queues/R2 + local Redis
    |                         └── migrate + production denial checks
    |                             + /healthz + /health + Excel/PPT/R2 smoke
    |                             + blue/green promote, then retire prior host
    |
    v
[verify-api-preprod] ─── dedicated IAM + exact versioned SSM + dedicated DB/R2 read roles
    |                     └── migrate + DB/role/prod-CONNECT denial
    |                         + production provider/R2 key denial
    |                         + release identity + /healthz + /health + CDN
    |                         + isolated /health p50/p95/p99 load baseline, then terminate
    |                              |
    |                              v
    |── (if API changed) ──> [run-migrations] ─── production DB migrate
    |
    |── (if API changed) ──> [deploy-api] ─── pin LT to digest ──> ASG instance refresh
    |
    |── (if messaging changed) ──> [deploy-messaging] ─── pin LT to digest ──> ASG refresh
    |
    |── (if AI changed) ──> [deploy-ai] ─── pin LT to digest ──> ASG refresh
    |
    |── (if tools changed) ──> [deploy-tools] ─── pin LT to digest ──> ASG refresh
    |
    |── (if video changed) ──> [deploy-video] ─── Batch job definition revisions with SHA image
    |
    v
[verify-deployment] ─── healthz 200 + health 200 + ASG healthy instances
    |                    + tenant maintenance flag guard
    |                    + API-change student video playback chain smoke ──> PASS/FAIL
    |                    + promote verified digests to compatibility :latest
    |
    v
[notify-on-failure] ─── failure-only notification
```

## 3. Selective Build Logic

### Change Detection Rules

| Trigger Files | Builds |
|--------------|--------|
| `.dockerignore`, `docker/Dockerfile.base`, `requirements/{constraints,common}.txt`, `libs/`, `academy/`, `manage.py` | ALL images (force_full) |
| Worker 공통 import: `apps/{shared,support,core,infrastructure}/`, `apps/api/common/`, `apps/api/config/settings/worker.py` | ALL images (force_full) |
| Python package import roots: `apps/__init__.py`, `apps/{api,domains,worker}/__init__.py`, `apps/api/config[/settings]/__init__.py` | ALL images (force_full) |
| Django startup import: `apps/domains/*/{models.py,models/,apps.py,signals.py,signals/,__init__.py}` | ALL images (force_full) |
| `apps/`, runtime `scripts/` except `scripts/codex/`, `docker/api/`, `requirements/api.txt` | API |
| `apps/worker/video_worker/`, `apps/support/video/`, `apps/domains/video/`, `apps/api/config/settings/worker.py`, `docker/video-worker/`, `requirements/worker-video.txt` | Video Worker |
| Legacy aggregate `requirements/requirements.txt` | API + Video Worker only; it is not a base/AI/Messaging/Tools input |
| `apps/worker/messaging_worker/`, `apps/support/messaging/`, `apps/domains/messaging/`, `apps/api/config/settings/worker.py`, `docker/messaging-worker/`, `requirements/worker-messaging.txt` | Messaging Worker |
| `apps/worker/ai_worker/`, `apps/worker/omr/`, `apps/domains/`, `apps/support/ai/`, `apps/api/config/settings/(worker|base).py`, `models/`, runtime `scripts/` except `scripts/codex/`, `academy/`, `libs/queue/`, `docker/ai-worker*`, `requirements/worker-ai*` | AI Worker |
| `apps/worker/tools_worker/`, `apps/domains/tools/`, `apps/domains/ai/queueing/`, PDF 오답노트 서비스/정답 포맷터/한글 폰트, `apps/support/ai/services/sqs_queue.py`, `academy/(application/use_cases/tools|domain/tools|adapters/tools|framework/workers|adapters/queue/sqs)/`, `docker/tools-worker/`, `requirements/worker-tools.txt` | Tools Worker |

`force_full` is a correctness boundary for code imported by more than one runtime. It builds all six images, including `academy-base`; service-specific paths retain selective builds. `workflow_dispatch` always performs a full build/deploy. Every worker Dockerfile imports its actual runtime entrypoint during the immutable build, so a candidate with a missing module or incompatible import cannot reach production deployment. Every release, including worker-only selective releases, still runs the persistent API/Tools development gate before preprod.
Change predicates use the `changed_matches` here-string helper instead of `echo | grep -q`; this avoids a `pipefail`/SIGPIPE false negative on large multi-commit push ranges.
Push change detection derives each service's diff base from that image's source commit in the last complete verified release manifest, not from `github.event.before`. Therefore a failed workflow followed by a small hotfix still includes earlier unshipped API/worker changes. Missing, non-ancestor, or malformed image source evidence fails safe to a full build.

Evidence-only pushes under `docs/reports/**` and updates to
`docs/ssot/runtime-current.md` do not start the production release workflow.
Local worktree lifecycle tooling under `scripts/codex/**` is also excluded from
the push trigger and from accumulated API/AI image diffs; it is exercised by
the backend quality gate but cannot request production approval or AWS work.
Other non-runtime changes may still run lint and smoke checks, but the
production environment approval, shared mutation lock, AWS readbacks, image
builds, and deploy jobs are all skipped unless change detection selects at
least one runtime image or `force_full`.

`prepare-build` resolves one digest-pinned base before service work starts.
`build-runtime-images` then fans API, Video, Messaging, AI, and Tools out to
isolated matrix runners. Unchanged matrix entries terminate before checkout or
OIDC setup. `build-and-push` is the single fan-in owner: it assembles all six
digests, waits for the existing ECR security scan policy, uploads the exact
candidate artifact, and only then unblocks development/preproduction. This
preserves the release and lock gates while making full-build wall time follow
the slowest service instead of the sum of five service builds.

서비스 Dockerfile은 OS/Python 의존성을 앱 소스보다 먼저 설치하고, 앱 소스와
builder 산출물은 최종 소유권을 지정한 `COPY --chown`으로 복사한다. 따라서 일반
코드 변경은 의존성 레이어를 무효화하지 않으며, 별도 `RUN chown -R`이 같은 파일을
새 레이어에 다시 기록하지 않는다. 이 순서는
`tests/test_release_performance_contract.py`가 회귀를 차단한다.

런타임 EC2 역할의 worker-scale inline policy가 바뀌는 릴리스는 main push
전에 운영 권한으로 `pwsh scripts/v1/deploy.ps1 -AwsProfile default`를
실행해 정책과 GitHub Actions readback 권한을 먼저 수렴한다. CI에는
런타임 역할 정책 쓰기 권한을 주지 않으며, `iam:GetRolePolicy` 결과가
저장소 SSOT와 완전 일치하지 않으면 이미지 빌드 전에 실패한다.

### Build Output

Each image is tagged with:
- `:latest` — compatibility alias only; moved only after every production
  verification gate passes and exact digest readback succeeds; never
  deployment evidence
- `:sha-XXXXXXXX` — immutable source identity, first 8 chars of git commit SHA

Service builds resolve `academy-base` to a digest before `FROM`. Migration, API/Messaging/AI/Tools runtime, and all Video Batch job definitions resolve the run-unique SHA tag to `repo@sha256:...`. `deploy-api-and-verify-workers.ps1` verifies the last complete successful release manifest, waits for terminal refresh success, then compares its digests with Launch Template userdata, actual InService containers, and every active Video Batch job definition.

## 4. Selective Deploy Logic

Deploy jobs only run if the corresponding service was built:

```
deploy-api:       if build_api == 'true' || force_full == 'true'
deploy-messaging: if build_messaging == 'true' || force_full == 'true'
deploy-ai:        if build_ai == 'true' || force_full == 'true'
deploy-tools:     if build_tools == 'true' || force_full == 'true'
deploy-video:     if build_video == 'true' || force_full == 'true'
```

Dependencies:
- `deploy-api` waits for `run-migrations` to succeed (or be skipped)
- `deploy-messaging`, `deploy-ai`, `deploy-tools`, and `deploy-video` also wait for `run-migrations` success or an explicit skip; a failed migration blocks every runtime deploy
- `verify-deployment` waits for all deploy jobs
- `deploy-video` is included in the same workflow and runs when the video worker image changes
- every AWS OIDC credential step has a 180-second action timeout, so a stalled
  credential exchange fails into the workflow's compensation and lock-release
  path instead of holding the production mutation lane indefinitely

## 5. Zero-Downtime API Strategy

### ASG Instance Refresh

- **MinHealthyPercentage: 100%** (API) — 새 인스턴스가 healthy가 될 때까지 기존 인스턴스 유지. 502 gap 0건 보장.
- **MaxHealthyPercentage: 200%** (API) — ASG Instance Refresh가 교체 인스턴스를 먼저 기동할 수 있는 범위.
- **MinHealthyPercentage: 0%** (workers) — workers tolerate brief downtime during replacement (no HTTP traffic)
- **SkipMatching: false** (API) — launch template 변경 없어도 실제 인스턴스 교체 수행
- **InstanceWarmup: 300s** (API), **120s** (workers) — API는 ECR pull/컨테이너 기동 편차를 흡수
- **HealthCheckType: ELB** (API) — 앱 크래시 시 ALB가 감지 → ASG 자동 교체. **EC2** (workers) — ALB 없음.
- **HealthCheckGracePeriod: 300s** (API) / **60s** (workers) — 새 인스턴스 부팅 중 조기 종료 방지
- **ALB deregistration delay: 30s** — in-flight 연결 drain 후 즉시 정리
- Instance Refresh가 새 인스턴스를 먼저 기동하고 ALB health와 300초 warmup을
  통과한 뒤 기존 인스턴스를 drain/종료한다. CI가 구버전 인스턴스를 미리 한 대
  더 늘린 뒤 그 인스턴스까지 다시 교체하는 이중 scale-up은 수행하지 않는다.
- 평상시 API capacity는 SSOT `min=1 desired=1 max=3`이다. CI는 `min`과
  `desired`를 바꾸지 않는다. burst로 `desired == max`인 경우에만 교체 슬롯 한
  개를 위해 `max`를 `desired + 1`로 잠시 높이고, 성공·실패/보상 경로 뒤 정확히
  원래 값으로 복구·readback한다.
- API runtime scale-out/scale-in은 ASG target tracking(`ASGAverageCPUUtilization`, target 55%)이 담당한다.
- API Launch Template의 `DEPLOYMENT_ID`는 wall-clock 시간이 아니라 고정된 이미지 digest(또는 immutable SHA tag)에서 파생한다. 같은 이미지와 설정을 다시 배포하면 새 Launch Template version이나 불필요한 instance refresh를 만들지 않는다.
- API readiness 대기는 HTTP ALB DNS의 HTTPS redirect를 실패로 오인하지 않도록 공개 `https://api.hakwonplus.com/health`를 사용한다. ALB target health는 별도 AWS readback으로 확인한다.

### Deployment Sequence

1. The deploy job resolves its `sha-*` tag to an ECR digest and creates a new Launch Template version containing that digest. On the one-time legacy cutover, it first snapshots the actual running container digest into an immutable baseline version.
2. The ASG tracks `$Latest` after that guarded cutover. Instance Refresh uses
   `MinHealthyPercentage=100` and `MaxHealthyPercentage=200` to launch a new
   candidate instance before terminating an old one without changing desired
   capacity.
3. UserData installs Docker, logs in to ECR, pulls `repo@sha256:...`, fetches SSM env, and starts the container. `/academy/api/env` 동기화는 `DJANGO_SETTINGS_MODULE=apps.api.config.settings.prod`를 강제하고 atomic file replacement 후 재시작한다. worker 런타임만 `apps.api.config.settings.worker`를 사용하며, SSM command 또는 `docker run` 실패는 배포 실패로 전파한다.
   The API container disables Gunicorn's unused control socket; runtime operations use the guarded ASG/SSM deployment path, and the non-root app directory is not treated as a writable control-socket location.
4. ALB health check passes on the new instance.
5. The old instance is drained and terminated.

The API Docker container writes `academy-api/<instance-id>` streams to
`/academy/api` with 30-day retention. UserData obtains that identity through
IMDSv2 and fails before container start when production logging would be
untraceable. The shared instance role can write only the exact API/AI/Tools
log groups; it cannot create arbitrary groups.

Before a release builds or refreshes any runtime, `verify-runtime-iam` compares
the live `academy-ec2-role` worker-scale, worker-SQS, and container-log inline
policies with the checked-in exact JSON policies. The worker-SQS policy keeps
consume/send permissions on the three main queues and grants only
`GetQueueUrl`/`GetQueueAttributes` on the Messaging DLQ so fail-closed account
recovery can prove both queues are empty without receiving, deleting, or
redriving a parked message. It also requires `/academy/api`,
`/academy/ai-worker`, and `/academy/tools-worker` to exist with 30-day
retention. The deploy OIDC role remains read-only for runtime IAM; an operator
must run `converge-runtime-worker-sqs-iam.ps1 -Apply` from an exact fresh main
checkout when that single policy drifts; the command holds the shared mutation
lock and performs exact post-write readback without refreshing a runtime or
building an image. Other IAM drift uses its owning infrastructure convergence
path. This keeps a stale policy or missing log group from being discovered only
after a replacement instance has already started.

## 6. Worker Deployment Strategy

Workers use the same ASG instance refresh mechanism as API but with:
- Shorter warmup (120s vs 300s) — workers don't serve HTTP traffic
- No ALB health check — workers are background processors
- **MinHealthyPercentage=100%, MaxHealthyPercentage=200%** — each warm worker replacement launches before termination. SQS visibility timeout remains the message-loss guard.

Runtime scaling is split by worker:

- **AI** keeps SSOT min/desired=1/1, then uses AWS/SQS CloudWatch scale-out alarms (`ai-worker-queue-high`, `ai-worker-queue-age-high`) plus API wake-up for burst capacity. Idle scale-in is worker-owned after live SQS depth is empty and returns only to the warm baseline; `ai-worker-queue-low` is observability-only. The container uses Docker's `awslogs` driver with an instance-specific stream under `/academy/ai-worker`, so segmentation logs survive replacements for the configured 30-day retention. The shared EC2 role can write only that exact log group. The stream identity is read through an IMDSv2 token; a missing token or instance ID fails the boot before an untraceable container can start.
- Problem Studio image transcription defaults to the `global.amazon.nova-2-lite-v1:0` Bedrock inference profile when no OpenAI key is configured. Nova 2 Lite exposes no Seoul in-region or APAC/JP geo inference option from `ap-northeast-2`, so this profile may route encrypted inference traffic to any AWS commercial destination listed for the global profile. The shared EC2 instance role receives only `bedrock:InvokeModel` on that profile and its exact foundation model through `policy_workers_bedrock_problem_transcription.json`; `Ensure-EC2InstanceProfileSSM` converges the inline policy. Unit count, tenant quota, output tokens, request time, tenant-prefixed temporary storage, terminal archive deletion, UI confirmation, and privacy disclosure remain bounded or explicit in the application layer. CloudTrail `additionalEventData.inferenceRegion` is the operational source for the actual destination of a request.
- **Messaging** runs with ASG min/desired=1 warm baseline and AWS/SQS CloudWatch alarms for StepScaling up to SSOT max capacity. Account recovery and Alimtalk delivery are user-facing wait paths, so the worker is not allowed to cold-start from zero during normal operation. Enqueue-side capacity checks are coalesced for short same-process bursts so bulk delivery cannot throttle the Auto Scaling control plane; SQS alarms remain the durable fallback. Scale-in requires visible+in-flight+delayed backlog to stay 0 and then returns only to the warm baseline.
- **Tools** keeps ASG min/desired=1/1 so Excel/PPT/PDF work has no instance cold-start, and AWS/SQS CloudWatch alarms add burst capacity for deterministic conversion queues. Enqueue-side capacity checks are coalesced for short same-process bursts so bulk document jobs do not throttle the Auto Scaling control plane. Scale-in uses the same visible+in-flight+delayed backlog guard and cannot go below ASG min. Its Docker container writes instance-scoped streams to `/academy/tools-worker` with the same 30-day retention and fail-closed IMDSv2 identity rule as AI.
- **Video** is not an ASG worker. It is AWS Batch only.

### Worker UserData Flow

The worker launch templates contain UserData that executes on each new instance boot:

```bash
#!/bin/bash
# 1. Wait for network and read the instance identity with an IMDSv2 token
# 2. Install Docker (dnf/yum)
# 3. ECR login + pull digest-pinned image (5 retries, 15s apart)
# 4. Fetch SSM /academy/workers/env (base64 JSON -> KEY=VALUE env file)
# 5. For AI/Tools, attach the owning /academy/* awslogs driver and instance stream
# 6. docker run -d --restart unless-stopped --name <worker> -e DJANGO_SETTINGS_MODULE=... --env-file /opt/workers.env <image>
```

This UserData is implemented in `scripts/v1/resources/worker_userdata.ps1`; AI and Tools pass their exact log groups through `asg_ai.ps1` and `asg_tools.ps1`, while Messaging keeps its warm-instance local logging boundary.

## 7. Migration Strategy

### Execution

- Migrations run in GitHub Actions before API instance refresh.
- Immediately before migration, the workflow atomically refreshes `/opt/api.env` from SSM `/academy/api/env`; it does not reuse a stale instance env file.
- The workflow resolves the newly built SHA tag to a digest, pulls that exact image, and runs `python manage.py migrate --no-input` in a one-shot Docker container using that refreshed production env.
- Only runs when API or shared code changed
- Must succeed before API ASG refresh starts

### Backward Compatibility Requirement

Since migrations can succeed before every old API instance has drained:
- **Allowed:** Add nullable/default columns, add tables, add indexes
- **Not allowed in single release:** Drop columns, rename columns, remove tables, change column types
- For breaking schema changes, use a two-release process:
  1. Release N: Add new column (both old and new code work)
  2. Release N+1: Drop old column (old code no longer in production)

### Failure Handling

If migration fails:
- The SSM command returns non-zero exit code
- `run-migrations` job fails
- `deploy-api` is skipped (depends on migration success)
- API and all worker deploy jobs are blocked; no Launch Template, Batch job definition, or instance refresh mutation proceeds
- Fix the migration and push again

## 8. Rollback Strategy

### Image-Based Rollback

Every build produces immutable SHA-tagged images. To rollback:

1. **Identify the last good SHA tag:**
   ```bash
   aws ecr describe-images --repository-name academy-api \
     --query 'sort_by(imageDetails,&imagePushedAt)[*].{tags:imageTags,pushed:imagePushedAt}' \
     --output table
   ```

2. **Choose the recovery path:**
   ```powershell
   # Stateful services fail closed; rebuild desired source as a new release.
   pwsh scripts/v1/rollback-api.ps1 -Sha sha-XXXXXXXX
   pwsh scripts/v1/rollback-messaging.ps1 -Sha sha-XXXXXXXX

   # Runtime-isolated services support digest rollback.
   pwsh scripts/v1/rollback-ai.ps1 -Sha sha-XXXXXXXX
   pwsh scripts/v1/rollback-tools.ps1 -Sha sha-XXXXXXXX
   ```

API and Messaging persist state-machine values that an older image may not understand. A point-in-time DB/queue preflight cannot prevent live writers from creating such a value while old and new instances overlap. Until releases publish a machine-verifiable compatibility epoch and deployment can quiesce every writer, their wrappers stop before AWS mutation with `STATEFUL_IMAGE_ROLLBACK_BLOCKED`; recovery is a new immutable roll-forward build from the desired reverted/cherry-picked source.

For supported runtime-isolated services, the rollback scripts resolve the SHA tag to its digest, capture the prior Launch Template/default/actual runtime state, create and verify the `$Latest` Launch Template version, and only then start the ASG instance refresh. A pin, refresh, or digest-verification failure creates a compensating version from the captured prior version and verifies the restored runtime. Re-tagging `:latest` is compatibility-only and does not change a digest-pinned runtime.

With `-Sha` omitted, ASG rollback derives the current digest from the Launch Template rather than the mutable alias, then selects the newest image pushed before that runtime. It waits for terminal `Successful`, treats `RollbackSuccessful` as deployment failure, and reads every healthy InService container `RepoDigests`; desired-zero groups are proven against the candidate Launch Template digest. Tools uses `rollback-tools.ps1`; Batch video uses `rollback-video.ps1`, which updates and reads back all eight required job definitions, preserves durable job-definition options, and requires both compute environments to remain `VALID/ENABLED`.

### Successful release manifest

`docs/reports/ci-build.latest.md` is build evidence only. The build job also produces a six-image candidate from exact run-unique SHA digests plus unchanged digests from the preceding successful release. Only after ASG health, actual container digest, all Video Batch job definitions, and compute environment gates pass does CI promote `docs/reports/release-manifest.latest.json` with `complete=true` and `status=successful`. Manual `scripts/v1/deploy.ps1` resolves images exclusively from that manifest, so a partially pushed failed build cannot be mixed into a later manual release.

All production mutation entrypoints share one atomic DynamoDB lock in the SSOT table `academy-v1-video-job-lock`: CI build/deploy, weekly ECR/Batch cleanup, manual deploy/rollback, and every supported standalone API/worker runtime-environment writer. The active `__deployment_control_v2__` item is acquired conditionally, renewed only by its current unexpired owner, and released only by that owner. Delegating entrypoints pass the exact owner to child writers, which assert ownership instead of acquiring a second lock; deploy-owned SSM publishers assert the live owner immediately before each publish or rollback. The retired `__deployment_control__` key is permanently sealed so reruns of historical workflow definitions cannot bypass the current release-freshness guard. After acquiring the v2 lock, CI compares the candidate commit with the last successful release-manifest commit and proceeds only for the same commit or a descendant; stale and divergent candidates fail before any image or infrastructure mutation. ECR cleanup additionally protects every digest in the last complete/successful six-image manifest (including `academy-base`). A newly InService runtime may take up to ten bounded minutes to finish Docker/container bootstrap; cleanup retries its actual `RepoDigest` inventory and re-reads the exact healthy InService membership before and after every SSM attempt. A timeout or membership drift fails closed before image deletion, and Launch Templates or manifests never substitute for missing actual runtime evidence. Incomplete Video job-definition inventory, partial deletions, or verification warnings also fail nonzero.

On a fresh environment, the lock table itself is the sole allowed pre-lock bootstrap mutation. `scripts/v1/deploy.ps1` and `scripts/v1/converge-release-prerequisites.ps1` idempotently create/read it and validate the exact `videoId` string HASH schema, PAY_PER_REQUEST billing, ACTIVE state, and TTL before normal lock acquisition. Default/strict manual deploy also exits nonzero when post-deploy ASG, ALB, Batch CE, or queue verification fails; only an explicit `-RelaxedValidation` diagnostic run may finish with verification warnings.

On the first immutable-release cutover, manual deploy intentionally fails until that manifest exists. With all four existing runtime Launch Templates present, run `pwsh scripts/v1/converge-release-prerequisites.ps1 -AwsProfile default`; it converges and reads back only GitHub Actions IAM and ECR mutability, without changing LT, ASG, or Batch runtime state. The role can create versions only on those four templates; its `RunInstances` dry-run resources are derived from their actual AMI, security groups, ASG subnets, and instance profile, while PassRole stays restricted to the exact EC2/Batch roles. Then run one full `workflow_dispatch`; its verified six-image rollout bootstraps the first complete successful manifest. Selective builds are allowed only after that bootstrap.

All six ECR repositories use `IMMUTABLE_WITH_EXCLUSION` with one `WILDCARD=latest` exclusion and `scanOnPush=true`. CI and bootstrap both configure and read back that exact policy. Newly built digests must reach a completed ECR basic scan before development/preprod; a reused digest without findings is explicitly scanned. Every unaccepted critical finding blocks the release. The only accepted findings are exact repository/CVE/package/version entries in `docs/ssot/ecr-critical-risk-acceptance.json`; each has a vendor reference, reachability rationale, and hard expiration, and any identity drift fails closed. High도 `docs/ssot/ecr-high-risk-baseline.json` schema 3의 repository/CVE/package/version exact 집합과 상한이 모두 일치해야 한다. 모든 High는 `acceptedHighFindings`에 exact Debian tracker, 도달 가능성 근거와 hard expiration을 기록하며 metadata 없는 known 목록은 허용하지 않는다. 만료나 identity drift, 같은 수의 미검토 High 대체, package version 변경, 수정으로 기존 항목이 사라졌는데 기준선을 낮추지 않은 경우는 모두 scan 전에 실패 폐쇄한다. 시스템 FFmpeg는 변환을 소유하는 Video Batch 이미지에만 설치하며, AI는 빌드 시 검증한 OpenCV wheel의 비디오 지원을 사용한다. Weekly cleanup inventories every ASG-level and running-instance Launch Template version, every desired InService container's actual `RepoDigests` through SSM, and every ACTIVE Batch job definition before deletion. It protects referenced parent and child manifests even when they fall outside the newest-ten retention window, and aborts all deletion if any required runtime cannot be inventoried exactly.

Structural drift checks compare the API ASG's effective `$Latest` Launch Template version with the successful release manifest. The legacy `$Default` version is intentionally retained as historical state during the immutable cutover and is not runtime drift when the ASG is correctly pinned to `$Latest`.

### Migration Rollback

Migration reversal is prohibited as a generic incident action. Syntactic Django
reversibility does not prove that a previous binary understands backfilled data or
new state-machine values. Use a corrective migration and immutable roll-forward.
Reverse migration is allowed only when a migration-specific runbook proves the
reverse contract, all writers are quiesced, an RDS snapshot exists, and restore
verification has been rehearsed; there is intentionally no generic command here.

## 9. ECR Lifecycle Policy

ECR repositories should have lifecycle policies to prevent unbounded image accumulation:

```json
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Remove untagged images after 1 day",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 1
      },
      "action": { "type": "expire" }
    },
    {
      "rulePriority": 2,
      "description": "Keep last 10 sha-tagged images",
      "selection": {
        "tagStatus": "tagged",
        "tagPrefixList": ["sha-"],
        "countType": "imageCountMoreThan",
        "countNumber": 10
      },
      "action": { "type": "expire" }
    },
    {
      "rulePriority": 3,
      "description": "Keep last 5 release/deploy tags",
      "selection": {
        "tagStatus": "tagged",
        "tagPrefixList": ["latest", "v", "prod", "main", "deploy"],
        "countType": "imageCountMoreThan",
        "countNumber": 5
      },
      "action": { "type": "expire" }
    }
  ]
}
```

This keeps 10 rollback points and aggressively cleans untagged manifests. See `INFRASTRUCTURE-OPTIMIZATION.md` Section 4 for full ECR operational safety design including manifest-aware cleanup strategy.

## 10. Health Check Design

| Endpoint | Purpose | Checks | Used By |
|----------|---------|--------|---------|
| `/healthz` | Liveness probe | App is running, can respond | ALB health check, Docker HEALTHCHECK, deploy verification |
| `/health` | Readiness probe | App + database connection | Deploy verification, smoke tests |
| `/readyz` | Readiness check (same as /health) | App + database connection | Registered at `urls.py` lines 16-18 |

- ALB and Docker container health use `/healthz` for liveness decisions (lightweight, no DB)
- Deploy verification checks BOTH endpoints
- `/health` failure with `/healthz` success indicates DB connectivity issue (not an app crash)

## 11. Workflow File Location

`backend/.github/workflows/v1-build-and-push-latest.yml`

## 12. Related Files

| File | Purpose |
|------|---------|
| `.github/workflows/v1-build-and-push-latest.yml` | CI build, migration, API/messaging/AI/tools/video deploy, verification |
| `scripts/v1/resources/worker_userdata.ps1` | Worker UserData generation (Docker + ECR + SSM) |
| `scripts/v1/resources/asg_ai.ps1` | AI ASG + launch template management |
| `scripts/v1/resources/asg_messaging.ps1` | Messaging ASG + launch template management |
| `scripts/v1/resources/asg_tools.ps1` | Tools ASG + launch template management |
| `scripts/v1/resources/batch.ps1` | Video Batch CE/queue/job definition management |
| `scripts/v1/resources/api.ps1` | API ASG + launch template management |
| `scripts/v1/deploy.ps1` | Manual/bootstrap deployment (not used in CI/CD) |
| `scripts/v1/assert-production-source-freshness.ps1` | Manual production clean/latest-main and successful-manifest guard |
| `scripts/v1/converge-github-governance.ps1` | Read-only audit or explicit repository ruleset/Actions/environment convergence |
