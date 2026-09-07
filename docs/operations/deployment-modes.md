# 배포 방식 개요

**기준:** 실제 스크립트·워크플로우. 문서는 실행 방식과 일치하도록 유지한다.
**최종 갱신:** 2026-09-07

---

## 0. 프론트엔드 배포

프론트엔드(`frontend/` 레포)는 백엔드와 완전히 독립된 배포 파이프라인을 가진다.

| 항목 | 내용 |
|------|------|
| **트리거** | `git push origin main` (frontend 레포) |
| **배포 대상** | GitHub Actions가 검증한 bundle을 Wrangler direct upload로 Cloudflare Pages에 배포 |
| **스크립트** | backend 배포 스크립트로 프론트를 배포하지 않는다. |

---

## 1. 백엔드 배포 구조

- **이미지 빌드·ECR 푸시:** GitHub Actions만 수행 (`.github/workflows/v1-build-and-push-latest.yml`). 로컬/EC2 빌드 금지.
- **API 서버 반영 경로:**

| 경로 | 트리거 | 서버 반영 방식 | 속도 |
|------|--------|----------------|------|
| **CI 자동 배포** | main push → GitHub Actions | build-and-push → 상시 격리 development(실사용 smoke) → 임시 격리 preprod(전용 DB migration+health) → 운영 migration → deploy-api/messaging/ai/tools/video → verify-deployment | ~20~40분 |
| **CI development 전용 검증** | `workflow_dispatch`에서 `deployment_scope=development_only` | 최신 `main`의 descendant만 API/Tools 후보 빌드 → 상시 격리 development 검증 → 종료 | 운영 배포 없음 |
| **수동 인프라 수렴** | clean·최신 `main`에서 `pwsh scripts/v1/deploy.ps1 -AwsProfile <approved-operator>` | 검증·승격된 digest → persistent development → 격리 preprod → 운영 env 승격 → API/worker/Batch/EventBridge/ALB 반영 | 30~45분 |

- **env·이미지 소스:** 운영은 SSM `/academy/api/env` → `/opt/api.env`, development는 버전 고정 `/academy/api/development/env`·`/academy/workers/development/env`, `academy_api_development` DB와 개발 전용 큐/R2를 사용한다. development Video Batch 전용 자원은 아직 없으므로 두 env의 `VIDEO_BATCH_JOB_QUEUE`·`VIDEO_BATCH_JOB_DEFINITION`은 빈 값으로 강제해 운영 Batch 제출을 실패 폐쇄한다. preprod는 릴리스마다 운영 env에서 새 Advanced SecureString 버전을 만들되 `/academy/api/preprod/db-credentials`의 전용 `academy_api_preprod_app` 역할로 DB 사용자·비밀번호를 교체한다. 동시에 Django·tenant-binding secret을 preprod 전용으로 파생하고 SOLAPI, Toss/billing, 외부 AI, VAPID, 정적 AWS credential을 제거한다. CDN playback은 `/academy/r2/preprod/credentials`의 production key와 다른 bucket-scoped read-only R2 key로만 검증한다. 이미지는 완전 성공 `docs/reports/release-manifest.latest.json`의 digest만 사용한다.
- **API 역할 불변조건:** `/academy/api/env`의 `DJANGO_SETTINGS_MODULE`은 `apps.api.config.settings.prod`, `/academy/workers/env`는 `apps.api.config.settings.worker`여야 한다. 누락·교차 오염·API env 조회 실패 시 배포를 중단하며 workers env에서 API env를 합성하지 않는다.
- **격리 불변조건:** development와 preprod EC2는 운영 ASG/ALB에 등록하지 않는다. development는 inbound 없는 전용 보안그룹·instance profile·DB/큐/R2를 사용한다. preprod는 전용 instance profile, 정확한 SSM parameter version·릴리스 ID, 별도 DB·전용 DB 역할을 사용하며 그 역할의 운영 DB `CONNECT`가 거부되는지 실연결로 증명한다.

---

## 2. CI 자동 배포 (push=서버 반영)

main에 push하면 자동으로 서버 반영까지 완료된다:

1. GitHub Actions `v1-build-and-push-latest.yml` 트리거
2. 직전 운영 릴리스와 대기열이 끝난 뒤 `origin/main`에서 그 릴리스가 승격한 성공 manifest를 캡처해 run 전용 artifact로 고정한다. 따라서 push 시점 checkout에 들어 있던 오래된 manifest 때문에 불필요한 전체 이미지 재빌드가 발생하지 않으며, 변경 감지·base 재사용·후보 조립은 모두 같은 캡처본을 사용한다. 캡처본이 없거나 완전 성공 형식이 아니면 선택 빌드로 추정하지 않고 실패 폐쇄한다. 이어 lint, expand/contract migration guard와 smoke가 통과한 뒤 base digest를 먼저 빌드/해결한다. guard는 같은 app의 이전 migration까지 역추적해 다른 옵션이 동일한 `CharField.max_length` 증가만 expand-safe로 인정한다. 길이 축소, 옵션 변경, 과거 필드 상태를 증명할 수 없는 `AlterField`는 계속 contract로 실패 폐쇄한다. PR 품질 게이트는 `ACADEMY_MIGRATION_PHASE = "contract"`와 구체적인 `ACADEMY_MIGRATION_REASON`이 있는 변경을 검토할 수 있지만 DB를 변경하지 않는다. 병합 뒤 일반 main push는 같은 contract migration을 계속 실패 폐쇄하며, 선행 expand 배포 완료 또는 구버전 API 호환성이 증명된 정확한 main SHA만 `workflow_dispatch`의 `allow_contract_migrations=true`로 실행할 수 있다. 변경 감지 결과에 따라 필요한 API, Video, Messaging, AI, Tools linux/arm64 이미지는 격리 matrix runner에서 병렬 빌드해 ECR run-unique `:sha-*` 후보로 푸시하고, 단일 fan-in job이 여섯 digest를 정확히 조립한다. 각 worker Dockerfile은 실제 entrypoint import를 build-time에 검증한다. 모든 ECR repo는 scan-on-push이며 재사용 digest도 scan 결과가 없으면 명시적으로 scan을 시작한다. 완료되지 않은 scan과 승인되지 않은 critical은 실패 폐쇄한다. 예외는 [container-image-security.md](container-image-security.md)의 exact·expiring 계약만 허용하며 high은 경고와 remediation 대상으로 남긴다. `:latest`는 이 시점에 움직이지 않는다.
3. `verify-api-development` job → API/Tools 변경 여부와 무관하게 모든 release candidate에서 같은 manifest의 API/Tools digest를 상시 격리 development에 blue/green 방식으로 배포한다. 전용 DB migration, 운영 DB·R2 접근 거부, 개발 큐/R2/Redis, `/healthz`, `/health`, 이미지 identity와 합성 XLSX/PPT/R2 실사용 smoke가 모두 통과해야 candidate를 active로 승격한다.
   `workflow_dispatch`의 `deployment_scope=development_only`는 current `origin/main`을 선조로 둔 후보만 허용하고 API/Tools만 선택 빌드한다. `run_wrong_note_canary=true`이면 실제 HTTP 생성 요청, development Tools SQS worker, R2 PDF 다운로드·reload, stale retry/terminal callback/중복 재배달/tenant denial과 exact cleanup까지 실행한다. 이 scope에서는 `ci-build.latest.md`를 main에 push하지 않으며 `verify-api-preprod`가 구조적으로 skip되어 migration과 모든 production deploy job에 도달할 수 없다.
4. `verify-api-preprod` job → development를 통과한 API digest로 릴리스 고정 env 버전을 만들고 임시 격리 EC2 1대를 기동해 별도 DB에 migration을 적용한다. prod settings, DB 이름·전용 역할, 운영 DB CONNECT 거부, env version·release ID, `/healthz`, DB 포함 `/health`, 실제 CDN chain을 모두 확인한 뒤 종료한다.
5. preprod 성공 후에만 `run-migrations`가 운영 DB migration을 실행한다.
6. 모든 `deploy-api`, `deploy-messaging`, `deploy-ai`, `deploy-tools`, `deploy-video` job은 같은 development·preprod 성공 결과를 공통 선행조건으로 사용한다.
7. API Launch Template pin과 ASG rolling refresh를 실행한다. API refresh는
   `MinHealthyPercentage=100`, `MaxHealthyPercentage=200`으로 후보를 먼저
   기동하며 `min`/`desired`를 선증설하지 않는다. `desired == max`일 때만 max
   ceiling을 한 슬롯 임시 확장하고 종료 경로에서 원복·readback한다. AI/Tools worker는
   먼저 SSOT min/desired=1 warm baseline과 안정 digest로 수렴한 뒤 후보를 pin하고,
   `MinHealthyPercentage=100`, `MaxHealthyPercentage=200`으로 교체한다. baseline이
   healthy하지 않으면 후보 refresh 전에 실패한다. EC2의 `Healthy/InService`는
   UserData·Docker·컨테이너 준비 완료를 뜻하지 않으므로 pre-pin runtime inventory는
   기존 컨테이너 digest를 읽을 수 있을 때까지 최대 600초간 기다린다. 각 실패는 SSM
   status·response code·stderr를 남기며, 끝까지 준비되지 않으면 Launch Template을
   변경하기 전에 실패 폐쇄한다. development, preprod
   또는 임시 서버 cleanup 실패 시 어떤 운영 서비스도 변경하지 않는다.
8. 새 인스턴스 기동 → UserData로 ECR pull + 운영 SSM env 역할 검증 + docker run
9. `verify-deployment` job → API health, ASG 상태, tenant maintenance flag, 실제 digest 확인 + API 변경 시 학생 영상 playback chain smoke. 검증 직전 자동 확장된 worker는 EC2 `Healthy/InService` 이후에도 SSM과 Docker가 준비되는 시간이 필요하므로 실제 digest readback을 최대 18회, 10초 간격으로 재시도한다. 끝까지 컨테이너가 준비되지 않거나 후보 digest가 아니면 실패 폐쇄한다. 학생 계정 secret이 없으면 skip하지 않고 실패한다.
10. 모든 owning deploy job이 `success` 또는 의도된 `skipped`이고 이후 검증도
    모두 성공한 뒤에만 여섯 저장소의 `:latest`를 검증된 digest로 옮겨 exact
    readback하고 `release-manifest.latest.json`을 승격한다. 한 서비스라도
    `failure`/`cancelled`이면 일부 runtime 검사가 생략됐더라도
    manifest와 alias 승격 전에 실패 폐쇄한다. 이미 같은 digest인 `:latest`는
    성공한 no-op으로 처리한다. failed-job 재실행은 새 `run_attempt` 소유자로
    공용 production mutation lock을 갱신하거나 조건부 재획득한 뒤 검증·승격을
    계속하므로, 이전 attempt의 lock 해제 뒤에도 잠금 없이 compatibility
    alias를 변경하지 않는다.

**IAM:** 일반 CI는 장기 access key가 아니라 backend `main` ref와 승인된 GitHub `production` environment subject만 정확히 신뢰하는 GitHub OIDC 역할 `academy-gha-ecr-build`을 사용한다. 환경 없는 build/development/preprod job은 main-ref subject를, production environment로 보호되는 job은 environment subject를 사용한다. production inline policy와 별도 관리형 development policy `academy-gha-development-deploy`를 저장소가 함께 소유하며, attached policy inventory가 정확히 그 하나인지 readback한다. development EC2는 `academy-api-development-role`, preprod EC2는 `academy-api-preprod-canary-role`을 사용한다. production mutation은 GitHub `production` environment 승인 뒤 시작한다. 사용자가 해당 run의 배포나 계속 진행을 명시한 경우 operator는 별도 재확인 없이 공식 API로 environment review를 제출하고 승인 readback 뒤 계속한다. 상세 저장소 설정과 정확한 run 범위 규칙은 [github-governance.md](github-governance.md)를 따른다.

---

## 3. 수동 인프라 수렴 (scripts/v1/deploy.ps1)

- **목적:** 인프라 변경(Launch Template, UserData, ASG, ALB, SSM, Batch 등)을 반영할 때.
- **실행:** `check-credentials.ps1`을 통과한 승인된 profile로 `pwsh scripts/v1/deploy.ps1 -AwsProfile <approved-operator>`
- **동작:** clean·최신 `origin/main`/성공 manifest 선조 관계 확인 → lock → Bootstrap/Ensure → 운영 env 후보 준비 → persistent development 실사용 검증 → fail-closed preprod/별도 DB 검증 → 운영 env 원자 승격 → 런타임 반영 → After-Deploy Verification
- **언제 써야 하는지:**
  - Launch Template, UserData, ASG, ALB, SSM 파라미터 등 인프라 설정 변경 시
  - 출시 전/후, 안정 반영이 필요할 때
  - "서버 상태를 정석 경로로 통째로 맞추고 싶을 때"

새 애플리케이션 digest를 처음 승격하는 용도로 사용하지 않는다. 새 후보는
먼저 GitHub Actions OIDC 경로의 persistent development와 isolated
preproduction을 통과해야 한다. account-root는 명시적으로 승인된 수동
작업에서만 경고와 함께 허용되며 게이트 우회 권한을 뜻하지 않는다.

**상세:** [formal-deploy.md](formal-deploy.md)

---

## 4. 주의사항

- **문서와 스크립트 불일치 금지.** 배포 설명은 실제 `scripts/v1/deploy.ps1`, `.github/workflows/v1-build-and-push-latest.yml` 기준으로만 기술한다.
- 수동 production mutation은 dirty tree, detached HEAD, `main` 이외 branch, `origin/main`보다 앞서거나 뒤진 checkout에서 시작하지 않는다. `assert-production-source-freshness.ps1` 실패는 우회하지 않는다.
- **멀티테넌트:** 어떤 배포 경로를 쓰든 tenant fallback·default tenant·tenant 없는 query·cross-tenant 노출은 금지.
- env는 SSM→/opt/api.env만 사용하며, development, preprod와 운영 parameter를 분리한다.
- 운영 API 서버에 후보 이미지나 후보 env를 먼저 적용하지 않는다. 상시 development 검토와 `run-api-preprod-canary.ps1`의 임시 격리 검증이 모두 성공한 뒤에만 운영 API/worker Launch Template·ASG, Batch job definition, EventBridge, ALB 또는 운영 컨테이너 변경이 허용된다.
- 운영 env만 변경된 경우에도 컨테이너를 제자리 재시작하지 않는다. env parameter version이 포함된 Launch Template로 ASG rolling refresh한다.

---

## 5. 검증 방법

| 목적 | 방법 |
|------|------|
| 배포 후 API·인프라 상태 | `run-production-canary.ps1 -Mode PostDeploy -AwsProfile default -WriteReport` 후 `run-deploy-verification.ps1 -AwsProfile default`. 프런트 정적 R2 검사는 `front.r2StaticBucket`이 설정된 배포에서만 필수이며, 비어 있으면 Cloudflare Pages 전용 구성이므로 선택 항목 미설정으로 기록한다. 영상 R2는 API·워커 카나리에서 별도로 검증한다. |
| 학생 영상 재생 경로 좁은 회귀 | `python scripts/post_deploy_smoke/video_playback_chain.py`. 명시적 `E2E_VIDEO_ID`가 없으면 등록 강의·회차를 순회해 영상이 실제로 있는 첫 회차를 사용하고, 모두 비어 있으면 테넌트 공용 영상 세션을 검증한다. 로그에는 학생 자격 증명 값 대신 구성 여부만 남긴다. |
| 성공 릴리스 digest와 서버 이미지 일치 | `release-manifest.latest.json`의 digest와 Launch Template, 실제 InService 컨테이너, Video Batch job definition을 `deploy-api-and-verify-workers.ps1`로 비교. |
| API health | API 공개 URL로 `/healthz`, `/health` 200 확인. |

---

## 6. 장애 시 확인 포인트

- `scripts/v1/deploy.ps1` stderr, `API_PREPROD_CANARY_PASS` 유무, ASG/ALB/Batch 상태, SSM `/academy/api/env` 존재·형식·prod settings module.
- CI deploy-* 또는 verify-deployment job 실패 시: GitHub Actions 로그 확인 → IAM 권한/ASG/ALB/Batch 상태 확인.
- health check 실패 시 `docker logs academy-api`.

---

## 7. 관련 문서

| 문서 | 내용 |
|------|------|
| `docs/operations/formal-deploy.md` | 수동 정식 배포 상세: 목적, 실행 방식, 검증, 주의. |
| `docs/operations/배포.md` | 인프라 부트스트랩 (RDS/SQS/EC2/IAM 처음부터). |
| `.github/workflows/v1-build-and-push-latest.yml` | CI 빌드·ECR·마이그레이션·서비스별 deploy·검증 흐름. |

---

## 8. 멀티테넌트 관련 금지 사항 (배포와 무관하게 적용)

- tenant fallback, default tenant, host 보정, tenant 추정 금지.
- tenant를 식별할 수 없는 상태에서 검증 성공으로 처리 금지.
- tenant context 없는 query, cross-tenant 조회 가능성, tenant 필터 누락 금지.
- env는 SSM→/opt/api.env만 사용. 운영 편의로 tenant isolation 약화 금지.
