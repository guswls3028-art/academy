# scripts/v1 — 정식 배포·검증 (풀셋팅 v1)

> **ACTIVE:** 이 문서는 현재 유효합니다.
>  
> **Authoritative docs:** `docs/ssot/params.yaml`, `docs/infrastructure/`, `docs/README.md`
>  
> **Alias policy:** `docs/ssot/path-alias-policy.md`
>  
> **경고:** `docs/v1/...` 경로 표기는 stale 별칭일 수 있으므로 사용하지 않습니다.

**딸깍 6단계** (새 PC에서 그대로 재현):

1. **bootstrap** — `pwsh scripts/v1/bootstrap.ps1`  
   AWS CLI, 인증, region, `docs/ssot/params.yaml` 존재 확인.

2. **deploy -Plan** — `pwsh scripts/v1/deploy.ps1 -Plan -AwsProfile default`
   AWS 변경 없이 표/리포트만 출력. Drift·Evidence 확인.

3. **PruneLegacy 후보 미리보기** — `pwsh scripts/v1/deploy.ps1 -Plan -PruneLegacy -AwsProfile default`
   이 스택에서 명시적으로 폐기한 allowlist 리소스만 조회해 후보를 표시하며 삭제하지 않는다.

4. **deploy** — `pwsh scripts/v1/deploy.ps1 -AwsProfile default`
   현재 SSOT를 Ensure한다.

5. **deploy 재실행 (No-op)** — `pwsh scripts/v1/deploy.ps1 -AwsProfile default`
   변경 없이 완료되는지 확인. 출력에 "Idempotent: No changes required" 확인.

6. **Evidence 확인** — `docs/reports/` 및 deploy stdout의 Evidence 테이블.

---

## 자동 검증

한 번에 위 6단계를 실행. `verify.ps1`은 PruneLegacy 후보를 미리보기만 하며 삭제하지 않는다:

```powershell
pwsh scripts/v1/verify.ps1 -AwsProfile default
```

실패 시 즉시 중단되고, 실패 지점·명령·로그 경로를 출력. 로그는 `logs/v1/YYYYMMDD-HHMMSS-verify.log`.
production backend deploy/worker 변경 후에는 `run-production-canary.ps1 -Mode PostDeploy -AwsProfile default -WriteReport`와 `run-deploy-verification.ps1 -AwsProfile default`를 이어서 실행한다.

### 공용 Alimtalk sender 정합화

API와 Messaging worker의 공용 Solapi 설정을 공급자의 유일 ACTIVE sender와
비교하는 기본 실행은 read-only다. 번호·credential·복호화 SSM 문서는 출력하거나
로컬 파일에 저장하지 않는다.

```powershell
python scripts/v1/reconcile_common_alimtalk_sender.py
python scripts/v1/reconcile_common_alimtalk_sender.py --apply
```

`--apply`는 clean exact latest `main`, complete successful release manifest,
main queue·DLQ 0, API/worker credential 동치를 요구한다. shared production
mutation lock 아래 두 SSM parameter의 sender key만 변경하고 Messaging→API만
MinHealthy=100/MaxHealthy=200으로 갱신한다. 모든 InService runtime의 일회성
HMAC readback, provider singleton 재조회, API health, queue 0이 끝나야 lock을
반환한다. refresh 전 실패는 exact SSM rollback, refresh 시작 뒤 실패는 lock을
유지한 forward-convergence 상태다.

### 공용 Alimtalk owner tenant 명시화

API와 Messaging worker가 호환 기본값으로 해석하는 공용 owner tenant를 두 SSM
문서에 명시하는 기본 실행은 read-only다. owner 값·credential·sender·복호화 SSM
문서는 입력받거나 출력하지 않는다.

```powershell
python scripts/v1/reconcile_common_alimtalk_owner_tenant.py
python scripts/v1/reconcile_common_alimtalk_owner_tenant.py --apply
```

이 도구는 고정 owner 1의 absent/exact 상태만 허용하고 누락된
`OWNER_TENANT_ID` 한 key만 worker→API 순서로 추가한다. 다른 key/value는 모두
exact 보존하며 sender 정합화는 별도 `reconcile_common_alimtalk_sender.py`가 계속
소유한다. `--apply`는 source freshness, 성공 manifest, main queue·DLQ 0과 shared
production mutation lock을 요구한다. exact version/raw/KMS readback 뒤
Messaging→API terminal refresh, InService HMAC, API health, queue 0을 확인하고
lock을 반환한다. 이미 두 SSM 문서가 명시돼도 runtime HMAC·health·queue를
생략하지 않으며 stale/missing runtime은 같은 refresh로 수렴한다. 숫자·null·다른
문자열은 명시값으로 인정하지 않는다. refresh 전 부분 실패는 lock owner를 다시
증명한 뒤 원문을 rollback하고, write 뒤 lock loss에서는 unowned rollback을 하지
않는다. refresh 시작 뒤 실패 또는 concurrency/rollback 불확실성은 lock을 유지한다.

### 분기별 RDS 복구훈련

RDS 생성·삭제는 GitHub Actions에서 금지한다. 승인된 운영자 환경에서 최신 자동
snapshot을 격리된 소형 인스턴스로 복구하고, 현재 migration·핵심 count·pgvector를
검증한 뒤 exact RunId 태그 대상만 자동 삭제한다.

```powershell
pwsh scripts/v1/run-rds-restore-drill.ps1 -AwsProfile default -Plan
pwsh scripts/v1/run-rds-restore-drill.ps1 -AwsProfile default
```

기본 evidence는 workspace의 `_artifacts/rds-restore-drill/`에 저장된다. 성공
조건은 검증 PASS뿐 아니라 임시 DB의 삭제 완료와 사후 부재 확인까지 포함한다.
세부 RTO/RPO와 사고 시 전환 절차는
[`disaster-recovery.md`](../../docs/operations/runbooks/disaster-recovery.md)를 따른다.

---

## legacy deploy cron 정리

구 hot/rapid deploy cron은 정식 배포 경로가 아니다. 서버에 과거 cron 잔재가 의심될 때만 cleanup 전용 스크립트를 사용한다.

```powershell
pwsh scripts/v1/disable-legacy-deploy-crons.ps1 -Action Status -AwsProfile default
pwsh scripts/v1/disable-legacy-deploy-crons.ps1 -Action Off -AwsProfile default
```

---

## params.yaml

- **위치**: `docs/ssot/params.yaml`
- **수정**: 환경별 값(리전, 계정, VPC 등)만 변경. 스크립트는 이 파일만 참조.
- **API ASG 용량**: 평시 min/desired=1, max=3. 배포는
  MinHealthy=100/MaxHealthy=200의 launch-before-terminate refresh가
  min/desired를 유지한 채 후보를 먼저 기동한다. `desired == max`일 때만 max
  ceiling을 한 슬롯 임시 확장·복구하며, CPU target tracking이 평상시 desired를
  소유한다.
- **상시 development gate**: 최초 1회
  `initialize-api-development.ps1 -AwsProfile <least-privilege-profile>`로
  전용 IAM/DB/SQS/SG와 개발 전용 R2 SecureString을 수렴하고 마지막 검증 release로 active 서버를
  기동한다. CI는 이후 worker-only release를 포함한 모든 후보에서 API/Tools digest를
  `deploy-api-development.ps1`의 blue/green 검증에 통과시킨 뒤에만 임시
  preprod로 진행한다. 수동 `deploy.ps1`도 같은 gate를 실행한다.
- **오답노트 actual-worker canary**:
  `run-wrong-note-development-canary.ps1`은 active development의 exact API/Tools
  digest를 확인한 뒤 일회성 QA 테넌트에서 HTTP 생성, 실제 Tools SQS 소비,
  R2 PDF download/reload, stale retry·중복 재배달·tenant mismatch를 검증한다.
  중복 메시지는 worker log의 `callback_ok=true message_deleted=true`까지 확인하며
  종료 시 tenant/user/AI job/audit/R2 잔여가 모두 0이어야 한다. GitHub에서는
  `deployment_scope=development_only`, `run_wrong_note_canary=true`로만 실행하고
  preprod와 production job은 구조적으로 skip한다.
- **development 접속**:
  `connect-api-development.ps1 -AwsProfile <profile>`은 active instance를
  정확히 1대로 확인한 뒤 localhost:18000 SSM tunnel만 연다. public
  listener/ALB는 만들지 않는다.
- **frontend release QA 권한 계획과 별도 host-only Apply**:
  `python scripts/v1/converge_frontend_development_qa.py`는 개발 host parameter deny의
  전체 grant inventory/시뮬레이션만 수행한다. `--frontend-role-plan`은 별도 frontend
  main OIDC 역할과 고정 NonInteractiveCommands/Port document의 생성 전 계획을
  출력한다. 두 계획은 운영 parameter 값을 읽거나 IAM/SSM 자원을 변경하지 않는다.
  별도 `--apply-host-boundary`만 승인된 기존 개발 host inline 정책에 두 Deny를 추가한다.
  이 모드는 검토된 세 hash와 공용 잠금·readback을 요구하며 frontend 역할/SSM 문서를
  만들지 않는다. `--frontend-role-plan`에는 Apply가 없다.
  실제 권한 수렴/readback 전에는 synthetic QA를 실행하지 않으며 frontend release는
  동일 artifact의 10-case real-use와 exact tenant/user cleanup0 없이는 차단한다.
  역할별 범위와 simulation 한계는
  `docs/operations/persistent-development-runtime.md`가 소유한다.
- **Tools development warm path**: Excel/PPT는 AI queue가 아니라 Tools queue로
  라우팅한다. 운영 Tools ASG도 `t4g.small` min/desired=1 warm baseline을
  유지하고, persistent development host는 운영과 격리된 별도 Tools
  container/process로 Excel/PPT/R2 실사용 smoke를 검증한다.
- **Video development fail-closed**: 전용 development Batch queue/job definition,
  job role, Batch 접근 가능 Redis가 아직 없으므로 development API·worker env의
  `VIDEO_BATCH_JOB_QUEUE`와 `VIDEO_BATCH_JOB_DEFINITION`은 빈 값이다. 운영 Batch를
  개발 대체재로 쓰지 않는다.
- **AI/Tools production warm path**: release는
  `converge-worker-warm-baseline.ps1`로 기존 안정 digest의 healthy worker를
  최소 1대 확보한 뒤 새 digest를 pin한다. refresh는
  MinHealthy=100/MaxHealthy=200으로 launch-before-terminate하며, burst 중인
  desired는 baseline으로 강제 축소하지 않는다.
- **ECR 이미지**: 6개 repo는 `IMMUTABLE_WITH_EXCLUSION`, 단 하나의 `latest` wildcard exclusion, `scanOnPush=true`를 exact readback한다. CI는 신규·재사용 build digest의 scan 완료와 승인되지 않은 critical=0을 요구한다. 예외는 `docs/ssot/ecr-critical-risk-acceptance.json`의 exact·expiring 항목만 허용하고, preprod·운영 검증 성공 후에만 여섯 digest를 `latest`로 승격한다.
- **런타임 불변성**: migration과 API/Messaging/AI/Tools Launch Template, Video Batch 8개 job definition은 모두 해당 빌드 tag를 `repo@sha256:...`로 해석해 사용한다. `latest`는 호환성 alias일 뿐 증거가 아니다.
- **격리 DB 경계**: `converge-api-preprod-database.ps1`이
  `/academy/api/preprod/db-credentials`와 `academy_api_preprod_app` 역할을
  수렴한다. `publish-api-preprod-env.ps1`은 매 release마다 새 preprod SSM
  version을 만들 때 production signing secret과 messaging/billing/external-AI/
  VAPID/static-AWS credential을 제거하고 production R2 key를
  `/academy/r2/preprod/credentials`의 bucket-scoped read-only key로 교체한다.
  publisher는 SSM 쓰기 직후의 짧은 전파 지연을 제한된 exact-version
  readback retry로 흡수하되, 시간 안에 읽히지 않으면 실패한다. canary는 그
  exact version/release ID와 운영 DB `CONNECT=false`를 검증한다.
- **expand/contract gate**:
  `scripts/lint/check_expand_contract_migrations.py`가 기존 migration 의미 변경과
  파괴적 operation을 차단한다. contract phase는 명시 metadata와
  `workflow_dispatch` 승인 없이는 실행되지 않는다.
- **성공 릴리스 manifest**: CI는 이번 빌드 digest와 직전 성공 릴리스의 변경 없는 digest로 candidate를 만들고, 실제 ASG 컨테이너·Batch jobdef·CE 검증 후에만 `docs/reports/release-manifest.latest.json`을 `complete=true,status=successful`로 승격한다. `scripts/v1/deploy.ps1`은 ECR의 newest image가 아니라 이 manifest만 사용한다.
- **ECR cleanup fail-closed**: cleanup은 ASG/LT와 실제 desired InService 컨테이너 `RepoDigests`, 정확히 8개인 Video ACTIVE job definition, 마지막 complete/successful 6-image manifest(공통 base 포함)를 먼저 보호한다. autoscaling 직후 `InService` 인스턴스의 Docker/container bootstrap이 아직 끝나지 않았으면 실제 digest를 최대 10분 동안 제한 재시도하고, 각 SSM 시도 전후에 exact healthy InService membership이 최초 snapshot과 같은지 다시 읽는다. 그 안에도 inventory가 완결되지 않거나 membership drift·부분 삭제 실패·verify 경고가 하나라도 있으면 LT/manifest를 실제 runtime의 대체 증거로 쓰지 않고 이미지를 삭제하지 않은 채 nonzero로 종료한다.
- **비용/낭비 감사**: `run-cost-waste-audit.ps1`은 운영 ASG, exact `Name`+`ManagedBy` 태그의 SSM-only persistent development 인스턴스, 동적으로 생성되는 Batch ASG 태그를 정상 관리 대상으로 구분한다. 개발 인스턴스는 정확히 1대가 아니면 drift로 기록하고 Redis 등급은 30/90일 최저치를 함께 보며, ECR/Batch cleanup dry-run 자체가 실패하면 0개 정리 대상으로 오인하지 않고 진단과 함께 차단한다. 격리 worktree에서 Python 의존성 경로가 다르면 `-PythonExecutable <venv-python>`을 명시한다.
- **ASG pin + 보상**: `pin-asg-image.ps1`은 `$Default`·`$Latest`·숫자 version을 모두 해석한다. 태그 기반 legacy template은 현재 인스턴스의 실제 `RepoDigest`로 먼저 불변 baseline version을 만들고 ASG를 `$Latest`로 전환한 뒤 candidate digest를 적용한다. 이전 LT/default/실제 runtime digest를 state에 기록하며, pin·refresh·runtime 검증 실패 시 이전 version을 빈 override로 정확히 복제한 새 보상 version을 만들고 refresh/runtime을 다시 검증한다. 비용 baseline 복귀나 autoscaling 직후에는 healthy InService 수와 현재 desired가 수렴할 때까지 기다린다. EC2가 Healthy이고 SSM이 Online이어도 대형 worker 이미지는 아직 pull 중일 수 있으므로 실제 컨테이너 digest를 최대 10분 동안 제한적으로 재시도한다. 그 안에도 digest를 확인할 수 없으면 mutation 전에 실패하고, desired=0 ASG도 candidate LT digest를 직접 검증한다.
- **공통 운영 mutation 락**: 정식 CI, 주간 ECR/Batch cleanup, 수동 deploy/rollback과 `/academy/api/env`·`/academy/workers/env` standalone writer는 SSOT DynamoDB table `academy-v1-video-job-lock`의 한 조건부 lock key를 공유한다. standalone PowerShell entrypoint는 `core/runtime-env-lock.ps1`을 사용하고, 자식 writer는 owner를 상속해 중첩 acquire 없이 refresh terminal success/readback까지 같은 scope를 유지한다. 긴 refresh는 poll마다 TTL을 renew하며 실패·취소·timeout·readback 오류는 lock을 남겨 forward-convergence를 요구한다. acquire/assert/renew/release는 owner와 TTL 조건을 검사하므로 동시 실행·만료 후 잘못된 release를 허용하지 않는다. 전체 SecureString 변환에 필요한 임시 파일은 process-owned 임시 디렉터리에서만 사용하고 성공·실패 모두 즉시 삭제한다.
- **수동 source freshness**: production `deploy.ps1`은 lock table 접근 전에
  clean `main`, exact latest `origin/main`, complete/successful manifest ancestor를
  요구한다. feature branch·dirty tree·stale checkout은
  `assert-production-source-freshness.ps1`에서 실패한다.
- **런타임 freshness 증거**: `deploy-api-and-verify-workers.ps1`은 성공 release manifest와 API/Messaging/AI/Tools LT 및 실제 InService 컨테이너 `RepoDigests`, 모든 Video Batch active job definition을 비교한다. refresh는 terminal `Successful`까지 기다리며 실패·취소·timeout을 실패 처리한다.
- **selective-build 안전 경계**: `.dockerignore`, `docs/ssot/params.yaml`, `academy/`, `libs/`, `manage.py`, 공통 requirements, `apps/{shared,support,core,infrastructure}/`, `apps/api/common/`, worker settings 및 Django startup model/app/signal 변경은 모든 이미지를 빌드한다. Video가 import하는 messaging selector/service/scheduler, Messaging이 import하는 video Redis status cache, Tools가 import하는 AI callbacks/job_types와 오답노트 PDF 서비스·정답 포맷터·한글 폰트도 각각 consumer image를 재빌드한다.

### 안전 롤백

`api`와 `messaging`은 이전 이미지가 신규 DB 상태값을 이해한다는 호환성
epoch과 전체 writer quiesce를 아직 제공하지 않는다. 따라서 아래 두 stateful
rollback wrapper는 AWS 변경 전에 `STATEFUL_IMAGE_ROLLBACK_BLOCKED`로
fail-closed 한다. 원하는 소스를 revert/cherry-pick한 새 커밋을 immutable
이미지로 빌드·배포하는 roll-forward만 허용한다. 시점성 DB/queue 0건 확인만으로는
ASG 교체 중 새 상태가 생기지 않음을 증명할 수 없어 우회 플래그를 두지 않는다.

```powershell
# stateful: 정책을 확인하고 mutation 없이 fail-closed
pwsh scripts/v1/rollback-api.ps1 -AwsProfile default
pwsh scripts/v1/rollback-messaging.ps1 -AwsProfile default
# stateless/runtime-isolated: digest rollback 지원
pwsh scripts/v1/rollback-ai.ps1 -AwsProfile default
pwsh scripts/v1/rollback-tools.ps1 -AwsProfile default
pwsh scripts/v1/rollback-video.ps1 -AwsProfile default
```

지원되는 stateless ASG rollback은 `-Sha sha-...`로 명시할 수도 있다. ASG 스크립트는 실제 LT digest보다 이전 이미지를 선택하고 warm baseline(`MinHealthy=100`, `MaxHealthy=200`) refresh, terminal success, 실제 컨테이너 digest/health를 검증하며 실패 시 원래 runtime으로 보상한다. Video 스크립트는 8개 job definition과 CE를 원자적 검증 단위로 취급하고 기존 parameters/tags/propagateTags/schedulingPriority/consumable-resource 속성을 보존한다.

### Immutable image 전환 전 IAM bootstrap

GitHub Actions 역할은 정확히 4개 SSOT Launch Template ID에 대해서만 `ec2:CreateLaunchTemplateVersion`을 허용한다. ASG가 template version을 채택할 때 AWS가 수행하는 권한 dry-run을 위해 `ec2:RunInstances`도 그 4개 template과 실제 AMI·보안그룹·서브넷·인스턴스/볼륨/네트워크 인터페이스에만 한정하며, `iam:PassRole`은 단일 EC2 instance role과 두 Batch task role에만 허용한다. 이 변경을 main에 push하기 전에 기존 운영 권한으로 아래 배포를 한 번 실행한다. 배포 스크립트가 Launch Template을 먼저 보장한 뒤 같은 실행에서 IAM을 canonical policy로 덮어쓰고 readback한다.

```powershell
pwsh scripts/v1/deploy.ps1 -AwsProfile default
```

IAM bootstrap 없이 workflow가 먼저 실행되면 image-pin 단계가 실패하고 instance refresh는 시작되지 않는다.

API/worker의 ASG wake 대상처럼 런타임 EC2 역할 inline policy가 바뀌는
릴리스도 main push 전에 위 수동 배포를 먼저 실행한다. 수동 배포가
`academy-api-ai-worker-scale`과 GitHub Actions 역할을 수렴·readback한다.
CI에는 런타임 역할 정책 쓰기 권한을 주지 않으며, 정확한
`iam:GetRolePolicy` readback이 SSOT와 다르면 이미지 빌드 전에 실패한다.

`academy-workers-sqs` 단일 정책 drift는 전체 이미지 배포 대신 exact fresh
`main`에서 아래 좁은 수렴 경로를 사용한다. 기본 실행은 read-only plan이고,
`-Apply`만 shared production-mutation lock을 획득해 checked-in policy를 쓰고
즉시 full-policy readback한다. 이 경로는 ASG refresh, Batch job definition,
queue message receive/delete/redrive를 수행하지 않는다.

```powershell
pwsh scripts/v1/converge-runtime-worker-sqs-iam.ps1 -AwsProfile default
pwsh scripts/v1/converge-runtime-worker-sqs-iam.ps1 -AwsProfile default -Apply
```

신규 환경에서는 공통 production-mutation lock table이 아직 없을 수 있다. `scripts/v1/deploy.ps1`과 `scripts/v1/converge-release-prerequisites.ps1`은 다른 mutation보다 먼저 이 테이블 하나만 조건부/idempotent 생성하고 key schema(`videoId` HASH string), PAY_PER_REQUEST, TTL을 검증한 뒤 락을 획득한다. 그 외 리소스 생성·갱신은 락 이후에만 수행한다. 운영 mutation은 strict 전체 경로만 허용하고 사후 ASG·ALB·Batch 검증 실패를 nonzero로 종료한다. `-RelaxedValidation`, purge/prune, 검증·대기 생략은 Plan 또는 비운영 진단/복구에만 사용할 수 있다.

최초 전환 시 `docs/reports/release-manifest.latest.json`이 아직 없으면 수동 `scripts/v1/deploy.ps1`은 의도적으로 실패한다. 기존 운영 Launch Template 4개가 존재하는지 확인한 뒤 `pwsh scripts/v1/converge-release-prerequisites.ps1 -AwsProfile default`로 ECR latest-only mutability와 GHA IAM을 수렴·readback한다. 이 전용 스크립트는 LT/ASG/Batch 런타임을 변경하지 않는다. 이어 GitHub Actions의 `workflow_dispatch`를 한 번 실행해 6개 이미지를 모두 빌드·배포·검증하고 최초 complete/successful manifest를 생성한다. 이후부터 selective build와 수동 deploy가 그 manifest를 기준으로 동작한다.

---

## 주의

상시 개발 런타임의 최초 구성, 경계, 접속, 개발→preprod→운영 순서는
`docs/operations/persistent-development-runtime.md`가 정본이다. AWS 계정 루트 키로
mutation 스크립트를 실행할 수 없으며 일반 CI 배포는 GitHub OIDC 역할을 사용한다.

- **PruneLegacy**: 계정 전체나 `academy-*` 이름을 스캔하지 않는다. `core/ssot.ps1`의 명시적 폐기 allowlist와 일치하는 리소스만 후보가 되며, 실행 전 `-Plan -PruneLegacy`로 후보를 확인한다. 실제 삭제는 `-PruneLegacy`를 별도 수동 실행할 때만 발생한다.

---

## 새 셸에서 인증 에러 날 때

- **원인**: `$env:AWS_ACCESS_KEY_ID` 등은 현재 터미널 세션에만 적용되므로 새 프로세스에는 인증이 없을 수 있다.
- **진단**: `pwsh scripts/v1/aws-diagnose.ps1` — credential source·에러 메시지 확인.
- **해결**  
  - **방법 1**: `aws configure` 로 default 프로파일 저장 → 어떤 셸에서든 동작.  
  - **방법 2**: named 프로파일(또는 SSO) 사용 시 `pwsh scripts/v1/deploy.ps1 -Env prod -AwsProfile prod`, `pwsh scripts/v1/verify.ps1 -AwsProfile prod`.
- **상세**: [docs/infrastructure/deployment-architecture.md](../../docs/infrastructure/deployment-architecture.md)
