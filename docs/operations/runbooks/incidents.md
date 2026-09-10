# 장애 대응 런북

**Version:** V1.11.29 | **최종 수정:** 2026-08-20

> 모든 AWS 명령은 `scripts/v1/run-with-env.ps1 --` 접두사를 사용한다.
> 아래에서 `RUN_ENV`는 이 접두사의 줄임말이다:
> ```
> powershell -File scripts/v1/run-with-env.ps1 --
> ```

---

## 0. 사용자 오류 운영 알림

`Dev Alerts Cron`은 사용자 오류 신호를 5분마다, 결제·worker 등 전체 운영 룰을
매시 2분에 평가하도록 예약된다. GitHub 스케줄은 지연될 수 있으므로 실제 평가
주기는 workflow 실행시각과 `cron.check_dev_alerts` 감사 로그로 확인한다.

- API 사용자 경로의 HTTP 5xx
- 브라우저 React/window/unhandled-rejection 오류
- 공용 `문제 신고` 모달 접수
- 관리자·선생님 개발자 메뉴의 `[BUG]` 제보

운영 알림 채널은 Slack webhook 하나다. `DEV_ALERTS_WEBHOOK_URL`이 없으면 외부 발송은
하지 않고, 경고 발견 여부와 관계없이 명령을 실패 종료한다. 수신처 없이 평가만 하는
명시적 `--dry-run`은 허용하지만 외부 알림 정상 동작의 증거가 아니다. SMS/LMS 설정, 실발송 테스트,
CloudWatch transition 문자 발송 예외는 모두 제거되었다. 운영 장애용으로 승인된 공용
카카오 템플릿이 없으므로 이 경로를 임의의 알림톡으로 대체하지 않고 fail-closed한다.

같은 테넌트·경로·오류 유형은 15분 단위 fingerprint로 묶는다. Slack webhook이 성공을
반환하면 `OpsAuditLog(action=alerts.user_incident_slack)`에 fingerprint를 저장해
2일 보존 범위에서 반복 알림을 제외한다. Slack 실패나 webhook 미설정, `--dry-run`은
fingerprint를 소비하지 않는다. 폐기 전 SMS가 남긴
`OpsAuditLog(action=alerts.user_incident_sms)`는 기존 2일 중복 억제와 사고 이력
조회에만 읽으며, 새 SMS provider 호출이나 재조회·재시도에는 사용하지 않는다.

명령은 알 수 없는 `--rule`, 어느 룰이든 평가 오류, webhook 미설정·전송 실패를
종료 코드 1과 `cron.check_dev_alerts.result=failed`로 기록한다. 일부 룰 평가가
실패해도 나머지 정상 평가된 경고의 전송은 시도하며, 수락된 사용자 오류만
fingerprint를 소비한다. 실패한 전체 검사를 `All clear`로 출력하지 않는다.
자동화는 `--silent`를 사용한다. 이 모드는 정상 무경고 출력을 생략하고,
경고가 있으면 규칙 키와 행 건수만 콘솔에 남긴다. 문의 제목·본문·테넌트·행
상세는 CI/SSM 로그에 출력하지 않으며, `--dry-run`이나 수신처 누락 때도 같다.
설정된 Slack으로 전달하는 상세 내용과 fingerprint 기록은 그대로 유지한다.
직접 실행에서 상세 평가 결과가 필요하면 `--silent`를 생략한다. 이 옵션은
실패를 성공으로 바꾸지 않는다.
`--dry-run`도 검사 오류는 실패한다. 감사 기록 자체를 저장할 수 없어도 명령은
실패 종료하며, 예외 원문·webhook URL 대신 고정 사유와 예외 종류만 남긴다.

콘솔 비노출·전송 내용 보존·수신처 누락·dry-run·실패 감사 회귀는
`python -m pytest apps/core/tests/test_dev_alerts_command.py -q`로 검증한다.

무경고 실행은 수신처가 설정되어 있어도 불필요한 시험 메시지를 보내지 않는다.
따라서 성공 실행은 해당 검사 완료의 근거이며, Slack 실수신 확인과 같지 않다.
수신처 복구 후 재실행하면 아직 수락되지 않은 보존 기간 내 사용자 오류만 다시
검사한다. 제품 outbox 재생·재발송이나 기존 감사 이력 수정은 하지 않는다.

회귀 검증: `apps/core/tests/test_dev_alerts_command.py`와
`apps/core/tests/test_user_incident_monitoring.py`에서 수신처 누락, 평가 실패,
전송 실패, dry-run, 성공 후 중복 억제, 민감정보 비노출을 검증한다.

5xx 폭주가 장애 중 DB 부하를 증폭하지 않도록 같은 테넌트·경로·오류 유형은 API
프로세스별 60초에 1건만 bounded 비동기 큐로 감사 로그에 저장한다. 사용자 응답은
DB INSERT를 기다리지 않으며 PII 없는 동일 신호를 애플리케이션 로그에도 남긴다.
원시 요청 횟수는 CloudWatch/Sentry에서 확인한다. `academy-api-UserImpact`
composite alarm은 인프라 관측 신호로 유지하지만 `Dev Alerts Cron`이 이를 휴대전화
문자로 변환하지 않는다.

```powershell
# 발송 없이 사용자 오류 룰만 평가. 전체 운영 룰까지 보려면 full_rules=true 추가.
gh workflow run dev-alerts-cron.yml -f dry_run=true
Start-Sleep -Seconds 3
$runId = gh run list --workflow dev-alerts-cron.yml --event workflow_dispatch --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $runId --exit-status
```

진단 시 `OpsAuditLog`의 `user_incident.*`,
`alerts.user_incident_slack`, 과거 `alerts.user_incident_sms*`,
`cron.check_dev_alerts` action을 확인한다. 과거 SMS action은 감사 이력이며
재활성화 절차가 아니다.

---

## 0-A. 제품 알림톡 오수신·대량 발송

모르는 수신자 제보가 오면 먼저 원 업무 tenant를 `MESSAGING_DISABLED_TENANT_IDS`에,
정확한 제보 번호를 `MESSAGING_RECIPIENT_DENYLIST`에 넣고 API와 messaging worker가
새 값을 읽었는지 확인한다. 연락처 수정만으로 이미 생성된 outbox/SQS가 사라지지 않으므로
tenant hold와 recipient denylist를 모두 유지한 채 아래 read-only 진단을 실행한다.

tenant hold는 고객 설정이나 온보딩 승인에 쓰지 않는 긴급 사고 전용이다. 두 SSM JSON을
손으로 따로 편집하지 말고 아래 스크립트로 같은 값을 원자적 순서로 반영한다. 스크립트 성공은
SSM 갱신까지만 뜻한다. API와 messaging worker를 공식 런타임 절차로 각각 refresh한 뒤 두
프로세스의 exact hold set과 health를 readback해야 적용 완료다. 해제도 동일한 절차와 검증을
거치며, 고객이 직접 끈 `Tenant.messaging_is_active` 값은 변경하지 않는다.

```powershell
pwsh scripts/v1/set-messaging-tenant-hold.ps1 `
  -TenantId <tenant-id> `
  -Mode hold `
  -AwsProfile <approved-operator>

# 해제 전 아래 진단과 active queue/provider 확인을 끝낸 뒤에만 실행
pwsh scripts/v1/set-messaging-tenant-hold.ps1 `
  -TenantId <tenant-id> `
  -Mode release `
  -AwsProfile <approved-operator>
```

```powershell
python manage.py diagnose_messaging_incident `
  --tenant-id <tenant-id> `
  --recipient <exact-phone> `
  --origin-id <excel-or-batch-job-id> `
  --since-hours 72 `
  --provider
```

결과의 `outbox.statuses`, `delivery_log.statuses`, `linkage`, `provider.status_codes`를
함께 본다. 출력에는 번호·본문·계정 비밀·provider ID가 없으므로 incident evidence로
보존할 수 있다. provider 조회 범위는 KST 달력 날짜이며 외부 HTTP 요청 URL 로그도
억제된다. `outbox.sent`는 SQS 접수일 뿐 최종 전달 성공이 아니고,
`delivery_log.sent`도 provider 접수 성공이다. provider 결과가 `sending`/`ambiguous`이거나
조회 오류이면 중복 피해 방지를 위해 자동 재발송하지 않는다.

공급자 계정 일일 안전 한도는 KST 기준 기본 900건이다. 한도 도달 행은
`provider_daily_dispatch_quota_deferred`로 다음 날 00:05에 이월되므로 failed로 수동
재생성하지 않는다. 계약 한도가 바뀌면 `MESSAGING_PROVIDER_DAILY_DISPATCH_LIMIT`을
실제 provider 한도보다 안전 여유를 둔 값으로 조정하고 API runtime readback,
`operations/status.rate_limit_provider_daily`, 다음 due claim을 확인한다.

hold 해제 전에는 연락처 정본, active outbox, SQS visible/inflight/delayed, provider pending,
최근 tenant log, denylist readback을 모두 확인한다. 제보 번호 denylist는 incident 보존과
사업자 확인이 끝날 때까지 별도로 유지할 수 있다.

---

## 1. API 500 급증

### 증상
- 사용자가 "서버 오류" 화면을 보고 보고
- `/health` 또는 `/healthz` 실패
- CloudWatch에서 5xx 급증

### 즉시 확인 (30초)
```bash
# 헬스체크
curl -s https://api.hakwonplus.com/healthz
curl -s https://api.hakwonplus.com/health

# 최근 배포 확인
gh run list -w "v1-build-and-push-latest.yml" -L 3
```

### 즉시 조치 (5분)

**경우 A: /healthz 실패 (앱 자체 다운)**
```bash
# ASG 인스턴스 상태 확인
RUN_ENV aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names academy-v1-api-asg \
  --query "AutoScalingGroups[0].Instances[*].{Id:InstanceId,Health:HealthStatus,State:LifecycleState}" \
  --output table

# 인스턴스가 Unhealthy → ASG가 자동 교체함. 기다린다.
# 인스턴스가 없음 → ASG min 확인
RUN_ENV aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names academy-v1-api-asg \
  --query "AutoScalingGroups[0].{Min:MinSize,Desired:DesiredCapacity}" \
  --output table
```

**경우 B: /healthz 200, /health 실패 (DB 연결 문제)**
→ **§ 5. DB 장애** 참조

**경우 C: 최근 배포 직후 발생**
→ **§ 6. 배포 실패/롤백** 참조 (이미지 롤백)

### 복구 확인
```bash
curl -s https://api.hakwonplus.com/healthz   # 200
curl -s https://api.hakwonplus.com/health     # 200
```

### 에스컬레이션 기준
- 5분 내 자동 복구 안 되면 롤백 실행
- 롤백 후에도 복구 안 되면 AWS Support 케이스 오픈

---

## 2. 메시징 중복/미발송

### 증상
- 학부모가 같은 문자를 여러 번 수신
- 발송 예정 메시지가 미발송

### 즉시 확인 (30초)
```bash
# SQS 큐 깊이 확인
RUN_ENV aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT}/academy-v1-messaging-queue \
  --attribute-names ApproximateNumberOfMessages,ApproximateNumberOfMessagesNotVisible \
  --output table

# DLQ 확인
RUN_ENV aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT}/academy-v1-messaging-queue-dlq \
  --attribute-names ApproximateNumberOfMessages \
  --output text

# 워커 인스턴스 확인
RUN_ENV aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names academy-v1-messaging-worker-asg \
  --query "AutoScalingGroups[0].Instances[*].{Id:InstanceId,Health:HealthStatus,State:LifecycleState}" \
  --output table
```

### 즉시 조치 (5분)

**미발송 (큐에 메시지 쌓임):**
```bash
# 워커 살아있는지 확인 — 인스턴스 0개면:
RUN_ENV aws autoscaling set-desired-capacity \
  --auto-scaling-group-name academy-v1-messaging-worker-asg \
  --desired-capacity 1
```

**중복 발송:**
- SQS visibility timeout이 처리 시간보다 짧으면 중복 발생 가능
- 워커 로그에서 동일 message_id 처리 여부 확인
- 근본 원인 파악 전까지 **워커를 중지하지 말 것** (미발송이 중복보다 위험)

**DLQ에 메시지 있음:**
- DLQ 메시지 내용 확인 후 원인 파악
- 단순 일시 오류면 DLQ에서 원래 큐로 재전송 (AWS 콘솔 > SQS > DLQ redrive)

### 복구 확인
```bash
# 큐 깊이 0으로 수렴 확인
RUN_ENV aws sqs get-queue-attributes \
  --queue-url https://sqs.ap-northeast-2.amazonaws.com/{ACCOUNT}/academy-v1-messaging-queue \
  --attribute-names ApproximateNumberOfMessages --output text
```

### 에스컬레이션 기준
- DLQ 메시지 10개 이상 누적
- 30분 이상 큐 깊이 감소하지 않음

---

## 3. 영상 인코딩 실패

### 증상
- 관리자가 업로드한 영상이 "처리 중" 상태에서 멈춤
- 영상 상태가 FAILED

### 즉시 확인 (30초)
```bash
# Django 관리 명령으로 stuck 영상 확인
RUN_ENV aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:Name,Values=academy-v1-api" \
  --parameters 'commands=["docker exec academy-api python manage.py scan_stuck_video_jobs"]' \
  --output text --query "Command.CommandId"

# 위 명령 결과 확인 (CommandId 사용)
RUN_ENV aws ssm get-command-invocation \
  --command-id {COMMAND_ID} \
  --instance-id {INSTANCE_ID} \
  --query "StandardOutputContent" --output text
```

### 즉시 조치 (5분)

**PENDING 상태에서 멈춘 영상 (stuck):**
```bash
# stuck 영상 복구 (PENDING → NEW로 리셋)
RUN_ENV aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:Name,Values=academy-v1-api" \
  --parameters 'commands=["docker exec academy-api python manage.py recover_stuck_videos"]' \
  --output text
```

**FAILED 상태 영상 재시도:**
```bash
# 실패 영상 재인큐
RUN_ENV aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:Name,Values=academy-v1-api" \
  --parameters 'commands=["docker exec academy-api python manage.py enqueue_uploaded_videos --include-failed"]' \
  --output text
```

**비디오 데몬 워커 자체가 죽은 경우:**
- 비디오 워커는 전용 인스턴스에서 동작 (ASG 아님, 별도 관리)
- SSH 접속하여 docker 컨테이너 상태 확인

### 복구 확인
- 관리자 페이지에서 영상 상태가 COMPLETED로 변경 확인
- 학생 앱에서 영상 재생 가능 확인

### 에스컬레이션 기준
- 같은 영상이 3회 이상 실패
- 30분 이상 경과 후에도 PENDING 상태

---

## 4. 비용 급증

### 증상
- AWS Billing 알림 수신
- 예상 비용 대비 비정상적 증가

### 즉시 확인 (30초)
```bash
# EC2 실행 중인 인스턴스 수 확인
RUN_ENV aws ec2 describe-instances \
  --filters "Name=instance-state-name,Values=running" "Name=tag-key,Values=Name" \
  --query "Reservations[*].Instances[*].{Name:Tags[?Key=='Name']|[0].Value,Type:InstanceType,State:State.Name}" \
  --output table

# ASG desired capacity 확인
RUN_ENV aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names academy-v1-api-asg academy-v1-messaging-worker-asg academy-v1-ai-worker-asg \
  --query "AutoScalingGroups[*].{Name:AutoScalingGroupName,Min:MinSize,Desired:DesiredCapacity,Max:MaxSize,Running:length(Instances)}" \
  --output table
```

### 즉시 조치 (5분)

**ASG가 max까지 스케일아웃된 경우:**
```bash
# 원인 파악: 최근 스케일링 활동 확인
RUN_ENV aws autoscaling describe-scaling-activities \
  --auto-scaling-group-name academy-v1-api-asg \
  --max-items 5 --output table
```
- CPU/메모리 부하 원인 파악 후 해결
- 의도하지 않은 스케일아웃이면 desired를 min으로 재설정

**Batch 작업이 과다 실행:**
```bash
# 활성 Batch 작업 확인
RUN_ENV aws batch list-jobs --job-queue academy-v1-video-batch-queue --job-status RUNNING --output table
```

**ECR 이미지 과다 누적:**
```bash
# 이미지 수 확인
RUN_ENV aws ecr describe-repositories \
  --query "repositories[*].{Name:repositoryName}" --output table

# 특정 repo 이미지 수
RUN_ENV aws ecr list-images --repository-name academy-api \
  --query "length(imageIds)" --output text
```
- lifecycle policy 적용 여부 확인 → 미적용이면 즉시 적용

### 복구 확인
- 인스턴스 수가 정상 수준으로 복귀
- 다음 날 billing 추이 확인

### 에스컬레이션 기준
- 일일 비용이 평소 2배 이상
- 원인 불명의 인스턴스 증가

---

## 5. DB 장애

### 증상
- `/healthz` 200이지만 `/health` 실패 (DB 연결 불가)
- API 응답에서 "database" 관련 에러
- 모든 데이터 조회 실패

### 즉시 확인 (30초)
```bash
# 헬스 엔드포인트로 DB 상태 확인
curl -s https://api.hakwonplus.com/health

# RDS 상태 확인
RUN_ENV aws rds describe-db-instances \
  --query "DBInstances[*].{Id:DBInstanceIdentifier,Status:DBInstanceStatus,Class:DBInstanceClass}" \
  --output table

# 연결 예산 경보 확인
RUN_ENV aws cloudwatch describe-alarms \
  --alarm-names academy-rds-DatabaseConnectionsHigh \
  --query "MetricAlarms[0].{State:StateValue,Reason:StateReason}" \
  --output table
```

### 즉시 조치 (5분)

**RDS 상태가 "available"이 아닌 경우:**
- `modifying` → 진행 중인 변경 완료 대기
- `backing-up` → 자동 백업 중. 일시적. 대기
- `storage-full` → RDS 스토리지 즉시 증설:
  ```bash
  RUN_ENV aws rds modify-db-instance \
    --db-instance-identifier {DB_INSTANCE_ID} \
    --allocated-storage {NEW_SIZE_GB} \
    --apply-immediately
  ```

**RDS 정상인데 연결 실패:**
- Security Group 규칙 변경 여부 확인
- API 인스턴스의 네트워크(VPC/서브넷) 확인
- 최근 인프라 변경이 있었는지 확인
- 로그에 `remaining connection slots are reserved`가 있으면 RDS를
  재부팅하지 말고 먼저 API 컨테이너의 PostgreSQL 소켓 점유와
  `DB_CONN_MAX_AGE` readback을 확인한다.
- 단일 API 컨테이너가 연결을 포화시키고 `DB_CONN_MAX_AGE`가 `0`이
  아니면 `/academy/api/env`의 해당 키만 `0`으로 변경하고
  `pwsh scripts/v1/refresh-api-env.ps1 -AwsProfile default`로
  롤백 보호 교체를 실행한다. SecureString 전체 값은 터미널이나
  보고서에 출력하지 않는다.
- SSM readback, `/healthz`, database-backed `/health`, API 컨테이너의
  PostgreSQL 소켓 감소를 모두 확인한다. 설정이 이미 `0`인데 포화가
  반복되면 관리자 연결로 `pg_stat_activity`의 사용자·클라이언트·상태
  분포를 수집하고 점유 서비스를 격리한다.

**RDS 재시작 필요 시:**
```bash
RUN_ENV aws rds reboot-db-instance \
  --db-instance-identifier {DB_INSTANCE_ID}
```
> 주의: 재시작은 1-5분 다운타임 발생. 최후의 수단.

### 복구 확인
```bash
curl -s https://api.hakwonplus.com/health   # 200 + "database": "connected"
```

`/healthz`는 DB를 확인하지 않으므로 단독 복구 증거로 사용하지 않는다.
`academy-rds-DatabaseConnectionsHigh`는 설정된 평가 기준에 따라 `OK`로
복귀할 때까지 감시한다.

### 에스컬레이션 기준
- RDS 상태가 10분 이상 비정상
- storage-full이 반복 발생
- 데이터 손실 의심

---

## 6. 배포 실패/롤백

### 증상
- GitHub Actions 워크플로우 실패
- 배포 후 `/healthz` 또는 `/health` 실패
- 배포 후 기능 장애

### 즉시 확인 (30초)
```bash
# CI/CD 실행 상태
gh run list -w "v1-build-and-push-latest.yml" -L 3

# 실패한 run 로그
gh run view --log-failed

# 현재 헬스 확인
curl -s https://api.hakwonplus.com/healthz
curl -s https://api.hakwonplus.com/health
```

### 즉시 조치 (5분)

**경우 A: 빌드/푸시 실패 (이미지가 바뀌지 않음)**
- 코드 수정 후 다시 push. 기존 서비스에 영향 없음.

**경우 B: Migration 실패**
- 배포가 자동 중단됨 (deploy-api가 migration에 의존)
- migration 오류 수정 후 다시 push

**경우 C: 배포 후 서비스 장애 → 롤백 필요**

API와 Messaging은 신규 결제·메시징 상태를 구버전 바이너리가 오해할 수 있고
ASG 교체 중 writer를 완전히 멈출 수 없어 image-only rollback을 실행 전에
`STATEFUL_IMAGE_ROLLBACK_BLOCKED`로 차단한다. 원하는 소스를
revert/cherry-pick한 새 커밋으로 빌드·배포하는 roll-forward가 복구 절차다.
AI/Tools/Video처럼 state-machine 호환성 경계가 분리된 서비스만 아래 digest
rollback 절차를 사용한다.

```powershell
# API 예시는 정책을 출력하고 mutation 없이 차단된다.
pwsh scripts/v1/rollback-api.ps1 -AwsProfile default

# 지원 서비스는 -Sha 생략 시 바로 이전 digest, 명시 시 tag -> digest를 검증한다.
pwsh scripts/v1/rollback-ai.ps1 -Sha sha-XXXXXXXX -AwsProfile default
```

롤백 스크립트는 `:latest`를 읽거나 재태깅하지 않는다. 실제 LT/Batch digest에서 출발해 이전 digest를 선택하고, ASG refresh의 `Successful` 종결과 모든 InService 컨테이너 digest를 검증하지 못하면 실패한다.

- API/Messaging: image rollback 차단, 새 immutable image roll-forward
- AI: `rollback-ai.ps1`
- Tools: `rollback-tools.ps1`
- Video Batch: `rollback-video.ps1` (8개 필수 job definition과 CE를 함께 검증)

**Migration rollback:** 기본 금지. 신규 상태값이나 데이터 backfill을 구버전 schema로
되돌리면 데이터 손실과 old/new binary 혼재가 발생할 수 있다. migration별로 검증된
reverse contract, writer quiesce, RDS snapshot, dry-run 결과가 모두 있는 별도 runbook이
없는 경우 schema도 수정 migration을 새로 배포하는 roll-forward로 복구한다. 일반
incident 대응에서 임의의 `manage.py migrate {이전 번호}`를 실행하지 않는다.

### 복구 확인
```bash
# ASG refresh 완료 대기 (5-10분)
RUN_ENV aws autoscaling describe-instance-refreshes \
  --auto-scaling-group-name academy-v1-api-asg \
  --query "InstanceRefreshes[0].{Status:Status,Progress:PercentageComplete}" \
  --output table

# 헬스 확인
curl -s https://api.hakwonplus.com/healthz   # 200
curl -s https://api.hakwonplus.com/health     # 200
```

### 에스컬레이션 기준
- 롤백 후에도 서비스 복구 안 됨
- ASG instance refresh가 실패 또는 10분 이상 진행 없음
