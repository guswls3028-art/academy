# 상시 개발 런타임

상시 개발 런타임은 운영 릴리스 후보를 실제 운영 자원에 넣기 전에 검토하는 첫 번째 차단
게이트다. 운영과 같은 API 인스턴스 유형·AMI와 동일한 digest-pinned API/Tools/AI 이미지를
사용하지만, 운영 ASG·ALB·DB 사용자·큐·R2 버킷에는 연결하지 않는다.

## 자원 경계

- EC2 이름은 `academy-v1-api-development`이며 외부 inbound가 없는 전용 보안그룹을 쓴다.
  접속은 Session Manager와 `connect-api-development.ps1`의 로컬 포트 포워딩으로만 한다.
- 인스턴스 역할 `academy-api-development-role`은 개발 환경 파라미터, API/Tools ECR pull,
  개발 전용 SQS 세 큐만 Allow 대상으로 삼는다. 관리형 SSM 정책의 광역 parameter
  Allow는 아래 explicit deny로 별도 제한해야 하며 실제 적용 여부는 readback한다.
- PostgreSQL은 기존 RDS 서버 안의 `academy_api_development` 데이터베이스와
  `academy_api_development_app` 역할을 쓴다. 이 역할은 운영 DB의 `CONNECT` 권한이 없다.
- 객체 저장소는 Cloudflare R2의 `academy-development-artifacts` 버킷만 사용한다.
  자격증명은 `/academy/r2/development/credentials` SecureString에 있고 운영 R2 버킷
  접근은 검증 단계에서 거부되어야 한다. AWS S3 버킷은 만들거나 사용하지 않는다.
- worker 설정도 `R2_STORAGE_BUCKET`·`R2_ADMIN_BUCKET`·`R2_REGION`을 worker env에서
  명시적으로 읽는다. API에는 개발 버킷이 적용됐지만 worker가 운영 기본 버킷명으로
  되돌아가 HWP 문항/해설 저장이 `AccessDenied`로 끝나는 경로를 허용하지 않는다.
- Redis/Valkey는 개발 EC2의 로컬 서비스다. 운영 Redis 엔드포인트를 공유하지 않는다.
- Tools worker와 AI worker는 같은 EC2 안의 별도 `academy-tools-development`,
  `academy-ai-development` 컨테이너로 실행되며 각각 개발 전용 큐만 소비한다.
  개발 AI worker는 운영 ASG scale-in 제어를 비활성화한다. 운영 Tools/AI ASG의
  평시 용량은 변경하지 않는다. API enqueue 경로도 `ACADEMY_RUNTIME_ENV`가
  `development` 또는 `preprod`이면 운영 worker ASG capacity ensure를 호출하지 않고
  격리 런타임의 로컬 worker 계약을 사용한다. 해당 인스턴스 역할의 운영 ASG 접근 거부는
  계속 필수 경계다.
- Video Batch는 아직 이 상시 개발 런타임의 구성 자원이 아니다. 개발 API·worker env의
  `VIDEO_BATCH_JOB_QUEUE`와 `VIDEO_BATCH_JOB_DEFINITION`은 빈 값으로 고정하고 settings와
  배포 readback에서도 이를 강제한다. 따라서 업로드 완료 후 Batch 제출은 누락 설정 오류로
  실패 폐쇄하며 운영 `academy-v1-video-batch-*` 큐·job definition으로 흘러가지 않는다.
- 운영 `CDN_HLS_SIGNING_SECRET`은 개발 env에 복사하거나 다른 개발 credential에서 파생하지
  않는다. env publication은 매 릴리스마다 암호학적 난수 32바이트로 개발 전용 키를 만들고,
  운영 키와 다름을 확인한 뒤 API와 worker에 같은 `CDN_HLS_SIGNING_SECRET`과 key id `v1`을
  넣는다. 두 versioned parameter의 exact equality는 값 노출 없이 readback한다. 이 키는 운영
  CDN에서 신뢰되지 않으며, frontend 실사용 카나리는 playback/session 응답으로 받은 exact
  signed URL만 메모리 HLS·poster fixture에 연결한다. 누락되면 unsigned URL을 허용하지 않고
  릴리스를 실패시킨다. API와 worker settings가 각각 canonical CDN·키 길이·key id를
  fail-closed로 검사하고, 개발 배포는 API·Tools·AI 컨테이너의 키 fingerprint가 모두 같은지도
  값 노출 없이 확인한다.
- 알림톡은 mock/dry-run, 자동 결제와 외부 알림 발송은 비활성화한다.

### SSM parameter 권한 경계

`AmazonSSMManagedInstanceCore`의 `GetParameter`/`GetParameters` 전체 자원 허용은
개발 전용 Allow 문만 추가해서는 제한되지 않는다. `Ensure-ApiDevelopmentIAM`은
`templates/iam/policy_api_development_parameter_boundary.json`의 explicit deny를
재구성한 `academy-api-development-runtime` inline policy에 합친다. 이 전체 bootstrap은
trust·managed attachment·profile도 수렴하므로 기존 host의 deny-only 교정에 사용하지
않는다. 아래 좁은 적용기만 기존 inline의 모든 필드·문장을 보존한다. API env, workers
env, 개발 DB credentials, 개발 QA password, 개발 R2 credentials의 정확한 다섯
parameter ARN만 개별 조회할 수 있고, 그 밖의 parameter 조회와 모든
`GetParametersByPath`/`GetParameterHistory`를 거부한다. 상위 경로 재귀 조회나
접두사가 비슷한 경로를 예외로 인정하지 않는다. SSM managed attachment와
agent의 control/data channel 권한은 유지한다.

이미 존재하는 역할에 이 deny가 적용됐다고 소스 변경만으로 판단하지 않는다.
`python scripts/v1/converge_frontend_development_qa.py`는 기존 inline/managed
grant 전체와 제안 deny의 양성/음성 IAM 시뮬레이션을 출력하는 **읽기 전용**
계획이다. 기본 실행과 `--frontend-role-plan`은 IAM 및 잠금 쓰기를 하지 않는다.
운영 parameter 값 조회로 거부를 시험하지 않는다.
IAM 시뮬레이션은 KMS key policy, 조직 SCP 등을 포함한 실제 복호화 성공/실패의
증거가 아니다. 검토된 최소 권한 수렴 및 실제 정책 readback 전에는 새 synthetic
QA를 실행하지 않는다. frontend 전용 OIDC 역할의 도입은 이 개발 host 역할 교정과
분리해 검토하며 backend production 역할의 신뢰나 권한을 넓히지 않는다.

#### host 단일 정책 적용과 공용 잠금

`converge_frontend_development_qa.py --apply-host-boundary`만 명시적 host 쓰기
모드다. 코드/테스트 완료는 AWS 적용 승인이 아니다. 별도로 승인된 exact 작업에서
새 계획의 `before_sha256`, `after_sha256`, `inventory_sha256`를 검토한 뒤
각각 아래 인자로 전달한다. 과거 계획에 inventory fingerprint가 없다면 다시
읽기 전용 계획을 만들며 임의로 hash를 채우지 않는다.

```powershell
python scripts/v1/converge_frontend_development_qa.py --aws-profile <approved-profile>
# 별도의 exact AWS 적용 승인 및 competing writer 배제 확인 후에만 실행:
python scripts/v1/converge_frontend_development_qa.py --aws-profile <approved-profile> --apply-host-boundary --expected-current-hash <before_sha256> --expected-proposed-hash <after_sha256> --expected-inventory-hash <inventory_sha256>
# 적용 후 재검증은 읽기 전용이며 잠금도 변경하지 않는다:
python scripts/v1/converge_frontend_development_qa.py --aws-profile <approved-profile> --verify-host-boundary --expected-proposed-hash <after_sha256> --expected-inventory-hash <inventory_sha256>
```

적용기는 기존 `deployment_lock.py`와 `academy-v1-video-job-lock`의
`videoId=__deployment_control_v2__`를 재사용한다. 새 잠금/테이블/권한을 만들지
않는다. account `809466760795`, region `ap-northeast-2`, role
`academy-api-development-role`, policy `academy-api-development-runtime`만 대상으로
한다. IAM과 잠금 helper는 같은 named profile 전용 subprocess 환경을 사용한다.
상충하는 정적 credential 환경변수는 자식 환경에서만 제거하며 값을 출력하지 않는다.
다른 table/region override 및 inherited `ACADEMY_DEPLOY_LOCK_OWNER` /
`ACADEMY_RUNTIME_ENV_LOCK_OWNER`는 거부한다. 공용 acquire는 재진입 불가이므로
같은 owner의 활성 잠금도 재획득하거나 부모 잠금으로 간주하지 않는다.

잠금 획득 후 policy와 inventory hash를 다시 검사한다. inventory에는 role ID/EC2
trust, permissions-boundary 부재, inline 1개, SSM managed grant 1개의 문서/버전,
profile ID/role binding, 연결된 유일한 running·active·종료 방지 개발 instance와
고정 scope 태그가 포함된다. inline의 기존 모든 필드·문장은 보존하고 검토한 두 Deny만
추가한다. 충돌/중복 Sid, 다른 grant/role/profile/instance, hash drift는 쓰기 전에
거부한다. 이미 적용된 policy는 다시 append하지 않고 별도 verifier를 사용한다.

쓰기 직전 lease renew/assert와 current hash를 재검사하고 정확한 `PutRolePolicy`
한 번만 호출한다. 그 외 IAM trust/attachment/profile, FE 역할, SSM, EC2/ASG/SQS
변경이나 전체 bootstrap/QA 실행은 없다. postverify는 policy/inventory 재조회와
**overlay 없는 현재 principal** 44개 action/resource 결과(allowed 19, explicitDeny
25, missing/duplicate/context 0)를 검사하고 simulation 뒤 policy/inventory를 다시
확인한다. 기존 plan의 `--policy-input-list` 제안 결과는 적용 증거로 사용하지 않는다.
자동 IAM 재시도와 자동 rollback은 없으며 전파 지연도 검증 실패로 처리한다.

기존 writer도 `initialize-api-development.ps1 → converge-api-development-prerequisites.ps1
→ Ensure-ApiDevelopmentIAM`의 host IAM 구간에서 같은 잠금을 직접 획득한다.
함수 자체는 실제 소유권을 각 IAM mutation 직전과 마지막 readback에서 검사하므로
잠금 없는 직접 호출도 거부한다. 기존 역할 및 최초 역할/profile 생성은 유지되며
PlanMode는 쓰기/잠금 없이 반환한다. 정상 host readback/소유권 검증 뒤 잠금을
반환한 다음 기존 OIDC·queue·DB 및 initializer의 publish/deploy 흐름을 계속한다.
이는 전체 initializer/SG/queue/DB/OIDC/standalone 개발 배포가 잠금으로 보호된다는
주장이 아니다. narrow Apply는 이 broad bootstrap을 호출하지 않는다.

획득 실패에는 release를 시도하지 않는다. 쓰기 전 실패이고 여전히 소유하면 반환하며,
쓰기 시도 이후 timeout/검증 실패는 한 번 더 소유권을 확인해 보류 상태와 만료 추정
시각을 보고한다. 소유권 상실이면 `ownership_unconfirmed`로 남기며 타인 item을
해제하지 않는다. release 실패도 성공으로 숨기지 않는다. Python 적용기의 JSON
checkpoint는 단계, owner, write-attempt 여부, 검증/잠금 상태를 구분하며 예외에
포함된 원문 CLI/비밀값은 출력하지 않는다. PowerShell bootstrap도 불확실한
획득/쓰기/소유권/반환을 구별하고 IAM native retry를 1회로 제한한다.

Python 적용은 600초 deadline의 checkpoint와 native 호출 30초 제한(직접 AWS
connect/read 5/15초, SDK retry 1회)을 사용한다. 기본 lease는 10,800초이며 보류는
TTL까지만 유효하다. 갑작스러운 프로세스 종료에는 checkpoint/finally가 실행되지
않을 수 있고 helper의 자식 프로세스 종료까지 보장하지 않는다. 이 잠금은 IAM의
fencing token이나 CAS가 아니다. 직접 CLI/console·구버전 writer·만료 후 살아 있는
프로세스의 check/write race를 제거하지 못하므로 승인된 작업 동안 경쟁 writer
배제와 수정된 writer 채택이 필요하다. old policy 복원은 광역 parameter 읽기를
재개하므로 자동 복구 대상이 아니며 별도 exact 승인/검증을 요구한다.

검증: `python -B -m unittest scripts.v1.test_frontend_development_qa -v`.
Backend Quality Gate의 기존 `Deployment contract tests` 단계도 같은 명령을 실행하며
0이 아닌 종료코드는 즉시 step 실패로 처리한다. 전체 pytest 수집과 별도인 회귀다.
기존 initializer/IAM/guard를 로컬 fake AWS로 실행하고, 실제 공용 잠금 알고리즘을
fake DynamoDB로 검사한다. 두 writer의 순서 교차·획득 실패·소유권 상실·commit 후
timeout·postverify 실패 및 기존 initializer의 존재/신규 경로를 검증한다. 이는 실제
AWS 경합·IAM 적용·SSM 세션·synthetic QA의 증거가 아니며 이들의 HOLD를 해제하지 않는다.

### frontend 동일 artifact real-use 진입점

`templates/iam/trust_frontend_development_qa.json`과
`policy_frontend_development_qa.json`은 별도 `academy-frontend-development-qa`
역할의 검토 대상 계약이다. frontend main-ref OIDC만 허용하며 backend production
역할과 기존 frontend R2 bootstrap 역할은 변경하지 않는다. 신규 역할/문서의 read-only
계획은 아래로 분리해 실행한다. frontend 역할/문서 Apply는 제공하지 않으며, 기존 같은 이름의
자원이 발견되면 덮어쓰지 않고 별도 검토를 요구한다.

```powershell
python scripts/v1/converge_frontend_development_qa.py --frontend-role-plan
```

`templates/ssm/frontend_development_qa.json`은 고정 `NonInteractiveCommands`
Session document다. Action은 Inspect/Setup/Cleanup뿐이고 tenant, release ID,
digest는 shell 문자/경로/SSM 참조를 허용하지 않는 strict pattern으로 제한한다.
`SyntheticLongVideo`는 `false`가 기본인 명시적 boolean 문자열이다. `true`인
실행에서만 scenario 명령의 `--synthetic-long-video` 분기를 사용하며 학생을 정확히
2명으로 고정한다. 이 fixture는 READY 영상 metadata 한 건
(`qa-fixtures/video-long/master.m3u8`, 900초)과 두 활성 수강의
`PROCTORED_CLASS` override만 만든다. R2 객체를 만들거나 운영 영상을 복제하지 않는다.
공식 frontend Playwright runner는 API가 반환한 정확한 signed path만 loopback 합성
HLS server에 매핑해야 하며, 이 매핑 없이 metadata 경로를 재생 성공 증거로 사용할 수
없다. 일반 Setup과 기존 caller는 기본 `false`라 영상 fixture가 생기지 않는다.
`NonInteractiveCommands` agent는 command 문자열에 shell을 암묵적으로 추가하지 않는다.
따라서 Linux command는 POSIX tokenization 결과가 정확히
`["/bin/sh", "-lc", <고정 script>]`가 되도록 shell과 단일 script 인자를 명시하며,
script의 첫 줄 `set -eu`는 executable이 아니라 그 shell 안에서 실행한다. 호출자가
command나 shell 인자를 전달하는 parameter는 없고 기존 고정 parameter pattern도
유지한다. 이 argv 경계와 embedded Python 구문은
`python -B -m unittest scripts.v1.test_frontend_development_qa -v`로 검증한다.
개발 settings, 현재 release/image, exact DB/user/R2, mock messaging, billing off,
빈 Video Batch, 개발 큐를 검증한다. Setup은 advisory lock 아래 기존 tenant/user
부재를 확인하며 원래 scenario 명령을 reset 없이 사용한다. 생성과 같은 DB transaction에
`OpsAuditLog(action=development.qa.setup)` 1행으로 exact tenant ID/code와 256-bit
run capability의 SHA-256 digest를 기록한다. 감사 기록 실패도 전체 생성 rollback이다.

계정·첫 수강 흐름도 운영과 같은 fail-closed 알림톡 계약을 사용한다. 개발 환경은
외부 발송이 불가능한 `development-mock-pfid`와 `SOLAPI_MOCK=true`를 함께 고정하고,
scenario Setup은 owner의 학생/학부모 가입 안내 및 비밀번호 복구 4개 설정에 exact
APPROVED mock 템플릿을 보장한다. 이미 유효한 개발 템플릿은 보존한다. 이 공용 mock
템플릿은 개발 런타임의 기준 데이터이므로 QA tenant cleanup 대상이 아니며 수신번호,
자격 증명, 운영 provider 식별자를 저장하지 않는다. 전용 개발 messaging worker가
개발 큐를 소비해 mock provider receipt를 영속화해야 계정 복구 실사용 검증이 통과한다.
빈 개발 DB에서는 같은 transaction이 `OWNER_TENANT_ID`에
`academy-development-owner`/`Academy Development Owner` 활성 tenant를 먼저 만들고
PostgreSQL tenant PK sequence를 전진시킨다. 그 ID나 code가 다른 tenant에 이미
사용됐거나 기존 owner identity가 다르면 데이터를 덮어쓰지 않고 fail-closed한다.

Setup 성공 응답의 positive `tenant_id`는 frontend runner가 메모리에만 보관하고,
Cleanup 및 그 직후 Inspect에 고정 문서의 `TenantId`로 그대로 전달한다. 최초 Inspect와
Setup은 기본값 `0`을 사용한다. Cleanup은 같은 advisory lock/transaction 아래 요청
`TenantId`와 exact tenant PK가 같고, 그 tenant에 연결된 성공 소유권 행이 정확히 1개이며
요청 capability digest가 일치할 때만 진행한다. 누락·중복·다른 run capability·다른
tenant ID는 거부한다. 이미 DB에서 부재하면 전달받은 positive `TenantId`의 R2 prefix까지
직접 0인지 읽고 destroy는 호출하지 않는다. 생성/정리가 겹쳐도 소유권 검사와 destroy
사이에 lock을 풀지 않는다. destroy는 user 삭제 전에 exact scenario user의
SimpleJWT outstanding token만 삭제한다. 학생 activity 감사 행은
`student_activity.login/screen_view/target_open`, exact target tenant, scenario
actor/target user, 현재 시각 이하, 그리고 최초 성공 `development.qa.setup` seal 시각
이후를 모두 만족할 때만 삭제한다. 일반 command 실행은 생성 transaction에 중복 없이
남긴 `development.qa.scenario` provenance seal을 같은 방식의 하한으로 사용하고, 고정
SSM 실행에서는 더 좁은 `development.qa.setup` seal을 우선한다. activity가 있는데 둘 다
없으면 시간 범위를 추정하지 않고 전체 destroy를 거부한다. 다른 tenant/user, 다른 actor,
선택된 seal 이전 감사 행은 보존한다. 두 PII-free provenance seal 자체도 tenant 삭제 후
FK가 NULL인 보안 증거로 남는다.

Inspect/Setup/Cleanup 출력은 tenant/user 수와 별도로 `outstanding_tokens`,
`activity_audits`, exact development R2 prefix의 `r2_objects`, exact `QA_TENANT`
환경을 가진 다른 container process의 `processes`, container port 18000 LISTEN의
`listeners`를 모두 numeric residue로 반환한다. Setup/Cleanup에서 하나라도 0이 아니면
고정 문서가 실패한다. Cleanup은 capability와 `TenantId` 검증을 마친 뒤 DB destroy보다
먼저 exact R2 prefix의 object만 삭제하고, 각 prefix를 다시 열거해 strong-consistency
0을 확인한다. R2 실패 시 DB tenant와 소유권 seal은 보존되어 같은 exact target으로
안전하게 재시도할 수 있다. R2는 `tenants/<id>/`, `excel/<id>/`,
`tenant-logos/<id>/`, `landing-public/reviews/<id>/`,
`matchup-showcase-snapshots/tenant_<id>/`만 열거·삭제하며 broad prefix나 다른 tenant
object는 건드리지 않는다. process/listener 수는 원격 development API container 경계다. runner 로컬
tunnel/process와 AWS Session tuple은 frontend 계약이 별도로 종료·증명한다. 이 변경은
스키마나 기존 데이터 migration을 만들지 않는다.
기존 QA tenant를 Inspect할 때도 tenant-scoped 감사·토큰 residue 조회는 하나의
`transaction.atomic()` 안에서 실행한다. 따라서 PostgreSQL의 transaction-local tenant
context가 조회 도중 풀리지 않으며, 외부 R2/process/listener readback은 DB transaction을
끝낸 뒤 계속 수행한다.

Cleanup과 positive `TenantId`를 받은 사후 Inspect의 성공·실패 출력은 요청 target
`tenant_id`를 그대로 echo한다. 사후 Inspect는 DB tenant가 이미 없어도 그 ID의 exact R2
prefix를 실제 열거하고 `r2_scope_proven=true`를 반환한다. `TenantId=0`인 최초 absent
Inspect는 R2 target을 추측하지 않고 `r2_scope_proven=false`로 구분한다. 실패 출력은
allowlist된 `failure_stage`, exception type, request `tenant_id`, 다섯 numeric residue만
포함하며 provider 오류 문자열, capability, 사용자 식별자, secret은 출력하지 않는다.

영상 분기를 켠 Setup은 별도 `video_state`에 `videos=1`,
`video_accesses=2`, `proctored_video_accesses=2`를 반환하고 progress/session/event와
PLAYER_ERROR/위반 수가 모두 0임을 검증한다. Inspect는 같은 키를 현재 숫자로만
반환한다. Cleanup과 이미 부재한 cleanup은 soft-delete 포함 영상, 권한, 진도,
재생 session/event, 활성 session, PLAYER_ERROR, 위반 event가 전부 0인
`video_residue`를 요구한다. 이름·전화·로그인 ID 같은 사용자 값은 이 readback에
포함하지 않는다.

이 경계는 생성 요청자가 보유한 capability의 증명이며 GitHub JWT의 run claim을 서버가
직접 검증한 것은 아니다. 원문 capability는 runner 메모리와 고정 SSM parameter로만
전달하고 artifact/CLI stdout/오류에 넣지 않는다. SSM control-plane 요청 기록이나 host
관리 권한은 별도 신뢰 경계이며 그런 기록에 대한 접근 권한을 frontend role에 추가하지
않는다. capability를 잃거나 기존 소유권 기록이 없으면 자동 재발급·채택·타 run cleanup을
하지 않고 별도 exact-target 복구 검토를 요구한다. 운영 행이나 비밀값을 복제하지
않는다. Port document는 remote8000/local18000으로 고정하며 host/shell 입력이 없다.

SSM Command API의 `GetCommandInvocation`은 resource-level 제한을 지원하지 않으므로
개발 명령 출력에만 한정된 권한이라고 주장할 수 없다. 새 역할은 SendCommand,
GetCommandInvocation/ListCommandInvocations/ListCommands를 명시적으로 거부하고
자기 Session의 결과만 받는다. StartSession은 두 exact document와 active-development
태그 인스턴스에 제한한다. 저장소 정책의 `StartFixedDocuments`는 두 승인 문서에
`BoolIfExists: {ssm:SessionDocumentAccessCheck: true}`를 사용한다. 승인 문서의
missing/true는 허용하고 명시적 false는 거부한다. 조건 전체를 삭제하거나 승인 문서를
추가하지 않는다. 인스턴스 statement는
기존 account/region ARN과 Name/ManagedBy/Environment/Lifecycle 네 태그를 그대로
검사한다. TerminateSession은 session의 caller tag와 aws:userid가 일치해야 한다.

instance의 strict Bool을 문서로 옮긴 것만으로는 실제 연결이 해결되지 않았으며,
공식 개발 run은 고정 문서의 `StartSession` AccessDenied에서도 중단됐다. 이 변경은
문서 statement의 연산자 한 키만 바꾼 저장소 계약이며 실제 해결 증거가 아니다.
CloudTrail은 실제 평가 key의 missing/false를 공개하지 않으므로 그 값을 추정하지 않는다.
저장소 테스트/PR/CI 성공은 live IAM 적용이나 SSM runtime red→green 증거가 아니다.
기존 역할이 있으면 planner는 같은 이름의 inline 정책 정확히 1개와 attached 정책 0개를
읽어 확인한다. 누락·중복·다른 inline·attached grant는 거부하고 정상 inventory여도
기존 역할의 자동 갱신 없이 별도 검토를 요구하며 중단한다. 별도 exact-main/정책 hash/
공용 잠금/승인 경계의 단발 적용과 readback 전에는 기존 live 정책을 수정하지 않는다.

planner의 45개 사례는 각 요청 context를 명시한다. 두 승인 문서와 기본 셸·승인외
문서·RemoteHost/일반 Port/Interactive/SSH의 exact ARN에 missing/false/true를 각각
평가한다. 승인 문서의 missing 기대값만 allow로 바뀌며 false 거부와 승인외 문서의
grant 부재는 유지한다. Amazon 소유 public document는 account 부분이 빈 ARN을 사용한다.
인스턴스 허용 사례에는 document key가 없으며 네 태그의 개별 불일치도 검사한다.
나머지 정책 key의 합성 positive controls는 서비스에서 관측한 값이 아니다. 생략한
document key를 공통값으로 다시 채우지 않는다. simulator가 보고한 missing key는
해당 사례가 의도적으로 생략한 document key만 허용하며, 다른 누락·이미 제공한 key의
누락 보고·기대와 다른 결정은 전체 계획 실패다. 로컬 fake는 요청 matrix와 판정 집계만
검사하며 IAM 판정기를 흉내 내거나 실제 세션 성공을 주장하지 않는다.

[AWS 공식 정책 예제](https://aws.amazon.com/blogs/security/how-to-enable-secure-seamless-single-sign-on-to-amazon-ec2-windows-instances-with-aws-sso/)는
StartSession에 `BoolIfExists`를 사용한다. 이는 Windows Fleet Manager 사례로,
이 저장소의 문서-only statement 배치가 검증됐다는 뜻은 아니다.
[현재 Quickstart](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-restrict-access-quickstart.html)의
node/document allowlist 예제에는 이 조건이 없으므로 필수 연산자라고 주장하지 않는다.
[IAM IfExists 규칙](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_condition_operators.html#Conditions_IfExists)에
따라 missing은 허용하되 false 거부를 유지하는 최소 변경이다.
[기본 문서 계약](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-default-session-document.html)은
DocumentName 생략 시 기본 셸 문서의 IAM 권한도 요구하며,
[사용자 지정 문서 계약](https://docs.aws.amazon.com/systems-manager/latest/userguide/getting-started-specify-session-document.html)은
지정 문서의 권한이 없으면 요청이 실패한다고 명시한다. 네 태그의 node grant만으로
문서 권한이 생기는 것은 아니다. exact2docs 밖의 grant가 없는 현재 inventory에서는
별도 shell Deny를 추가하지 않는다. 기존 Run Command Deny는 StartSession 거부 증거가 아니다.
ARN별 policy 검증은 생략 요청의 서비스 측 문서 선택이나 실제 shell 거부를 증명하지
않는다. 기본/foreign 문서의 StartSession 실호출은 이 릴리스 검증에 포함하지 않으며,
그 미실행을 공식 same-artifact flow의 추가 차단 조건으로 삼지 않는다. 실제 진행은
별도 승인 후 검토된 fixed Inspect/Setup/Cleanup과 공식 same-artifact QA로 제한한다.
실패하면 권한 확대나 guard 우회 없이 중단한다. 정책 연산자 변경은 기존 tenant/user/
소유권 데이터, trust, 문서의 명령·parameter·포트·수명 설정을 변경하지 않는다.

`ssmmessages:OpenDataChannel`도 resource-level ARN을 지원하지 않으므로 이 action만
Resource:*가 필요하다. 채널 인증은 StartSession의 session/caller 정보를 담은
TokenValue에 의존한다. 이를 IAM-level foreign-channel denial이나 실제 세션 성공
증거로 표현하지 않는다. TokenValue는 출력/공유하지 않는다. identity-policy simulation과
실제 OIDC/세션/복호화 검증을 구분하고, 검토된 변경의 실제 적용 전에는 synthetic QA를
계속 HOLD한다. 관련 AWS 정본은
[StartSession](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_StartSession.html)과
[Session schema](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-schema.html)다.

frontend runner/workflow와 원본 응답·CORS 보존, exact artifact, non-skipped 10-case,
cleanup0 승격 계약은 frontend `docs/DEPLOYMENT-OPERATIONS.md`가 소유한다.

#### 세션 제한과 장애 경계 (로컬 구현, 실제 AWS 동작 미검증)

고정 QA document의 `inputs`는 maxSessionDuration=5분/idleSessionTimeout=5분,
Port document는 25분/5분이다. 호출자가 timeout을 바꾸는 parameter는 없다. 이 값은
문서의 서비스 측 세션 제한 설정이며 신규 문서를 실제 적용하고 종료 readback하기
전에는 제한이 실제로 작동했다고 보고하지 않는다. IAM simulation은 이 설정이나
데이터 소유권을 검증하지 않는다. STS 1시간 만료 자체도 기존 SSM 세션 종료 보장이 아니다.

QA 원격 명령은 curl 각 10초, Docker inspect 15초(+kill 5초), Docker exec 210초
(+kill 5초), 내부 Python alarm 180초로 제한한다. DB transaction의 statement timeout은
150초/lock timeout은 5초이며 SSM secret read는 connect/read 각 10초, 최대 2회다.
외부 timeout이 Docker client를 종료하는 것과 내부 Python/DB가 중단되는 것은 별개이므로
내부 deadline과 DB timeout을 함께 둔다. 원격 host/프로세스의 비정상 정지까지 데이터
정리를 보장하는 장치는 아니다. 세션 최대 수명이 끝나도 tenant는 자동 삭제되지 않는다.

고정 문서의 소유권 함수 및 실제 `run()` cleanup 분기는
`scripts/v1/test_frontend_development_qa.py`에서 로컬 fake DB/command로 검사한다.
타 run/미소유/중복 record의 destroy 호출은 0, 자기 소유만 1, 부재는 0을 요구한다.
또한 token/activity/R2/process/listener residue가 모두 numeric 0이어야 한다. 실제
cleanup 명령 회귀는 scenario command 테스트에서 소유 user token과 seal 이후 activity만
삭제하고, 타 tenant token·다른 actor·seal 이전 activity·setup seal을 보존하는지
검증한다. 외부 QA tenant나 실제 DB 행을 삭제하는 회귀가 아니다.

## 최초 구성

Cloudflare R2 전용 토큰을 만든 뒤
`/academy/r2/development/credentials`에 아래 키를 SecureString JSON으로 저장한다.
값은 로그, 문서, GitHub 출력에 남기지 않는다.

```text
R2_ENDPOINT
R2_REGION
R2_ACCESS_KEY
R2_SECRET_KEY
R2_BUCKET
```

그 다음 루트가 아닌 전용 운영자 자격증명으로 한 번만 실행한다.

```powershell
pwsh scripts/v1/initialize-api-development.ps1 -AwsProfile <least-privilege-profile>
```

이 명령은 전용 IAM·보안그룹·큐·DB 역할/DB를 수렴하고, 마지막으로 검증 완료 운영
manifest의 API/Tools digest를 사용해 첫 개발 인스턴스를 만든다. 인스턴스는 사용자가
명시적으로 폐기하기 전까지 유지한다.

계정 루트 ARN은 `Assert-AwsMutationIdentity`에서 차단한다. CI와 일반 배포는
`academy-gha-ecr-build` GitHub OIDC 역할만 사용하며 장기 AWS access key를 요구하지 않는다.
개발 권한은 기존 운영 inline 정책과 분리된 고객 관리형
`academy-gha-development-deploy` 정책으로 관리하며
`converge-api-development-oidc.ps1`이 backend main-ref와 승인된 production
environment 두 subject만 허용하는 trust와 정책 readback을 강제한다. 환경 없는
development job은 main-ref subject를 사용하고, production environment가 붙은
lock/mutation job은 environment subject를 사용한다.

## 릴리스 순서

백엔드 릴리스는 아래 순서를 건너뛸 수 없다.

1. GitHub Actions가 run-unique 태그로 immutable 후보 이미지를 빌드한다.
2. `publish-api-development-env.ps1`이 운영 형태의 값을 복사하되 DB·큐·R2·발송/결제를
   개발 경계로 치환한다. 메시징은 dry-run 조기 종료를 쓰지 않고 durable outbox와
   전용 개발 SQS를 통과하며, API와 Messaging worker의 `SOLAPI_MOCK=true`가 외부
   공급자 호출과 비용을 차단한다.
3. `deploy-api-development.ps1`이 새 candidate 인스턴스를 만들고 migration, DB 역할,
   운영 DB 접근 거부, 개발 큐, 개발 R2와 운영 R2 접근 거부, Redis, `/healthz`,
   `/health`, 정확한 API/Tools/AI digest를 검증한다.
4. `run-api-development-smoke.ps1`이 합성 학생 XLSX 파싱, 1장 PPTX 생성·재열기,
   API와 AI worker 설정 경로 각각에서 개발 R2 객체 put/get/delete를 실행하고
   각 처리시간을 기록한다.
   현재 이 smoke는 Video Batch 제출 성공을 증명하지 않는다. Video Batch는 전용 개발
   queue/job definition/job role과 Batch에서 접근 가능한 개발 Redis가 준비되기 전까지
   비활성 상태여야 한다.
5. 모든 검증이 성공한 뒤에만 candidate를 active로 승격하고 이전 개발 인스턴스를
   종료한다. 실패하면 candidate만 종료하고 기존 active 인스턴스를 보존한다.
6. 이어서 별도 임시 preprod EC2 게이트가 통과해야 한다.
7. 그 뒤에만 운영 migration과 ASG/ALB 무중단 교체를 허용한다.

## 사용과 확인

```powershell
pwsh scripts/v1/connect-api-development.ps1 -AwsProfile <profile>
```

스크립트가 선택한 active 인스턴스의 API를 로컬 포트로 전달한다. 요청에는 반드시
`X-Tenant-Code`를 명시하며 기본 테넌트 추론은 없다.

사용자 로그인이 필요한 시각 QA는 운영 테넌트에 테스트 계정을 남기지 않고 이
런타임에서 수행한다. 검수 대상 frontend의 exact checkout을 로컬에서 빌드하거나
실행하고 API proxy를 위 loopback tunnel로 지정한다. 검수용 tenant·교사·학생은
`setup_ymath_realuse_scenario`처럼 production DB/R2에서 실행을 거부하는 명령으로만
만든다. 실제 학생·학부모·성적·연락처와 운영 비밀값은 복제하지 않는다.
이 명령은 `SOLAPI_MOCK=true`, `MESSAGING_DRY_RUN_TRIGGERS` 빈 값인 상시 개발
런타임에서만 `clinic_cancelled`의 승인된 공용 봉투 매핑을 확인하고, 일회용 QA
tenant에 tenant-owned content template과 활성 알림톡 config를 만든다. 학생+학부모
취소 접수 검증에는 합성 학생 연락처만 사용한다. 성공 판정은 취소 상태 저장과 두
durable outbox 생성, 전용 개발 SQS·mock worker 처리이며 실제 Solapi 호출은 0건이다.

검수는 desktop과 390px에서 로그인, 대상 화면 DOM, 상호작용, 새로고침 후 상태,
가로 overflow와 콘솔/API 오류를 확인한다. 종료 시 같은 명령의 `--destroy`로 정확한
`qa-*` tenant를 삭제하고 출력의 `remaining`이 모두 0인지 확인해야 한다. setup,
검수, cleanup 중 하나라도 실패하면 완료로 기록하지 않는다. 상세 Ymath 절차는
[Ymath 실자료 원본 전수 검증](runbooks/ymath-real-source-qa.md)을 따른다.

정상 상태의 최소 증거는 다음과 같다.

- active 개발 인스턴스가 정확히 1대이고 종료 방지가 켜져 있음
- 보안그룹 inbound 0개, ALB/운영 ASG 등록 0개
- API, Tools, AI 컨테이너가 동일 release ID의 digest-pinned 이미지로 실행 중
- 개발 DB 현재 사용자/DB 일치 및 운영 DB `CONNECT=false`
- 개발 SQS 세 큐 조회 성공
- API·AI worker의 개발 R2 객체 round-trip 성공, 운영 R2 버킷 접근 거부
- 합성 XLSX 파싱과 PPTX 생성·재열기 성공 및 처리시간 제한 통과
- `/healthz`, `/health`, 로컬 Redis/Valkey `PONG`
- Video Batch queue/job definition이 빈 값이고 운영 Batch 제출이 불가능함

## Video Batch 개발 canary 준비 계약 [PROPOSED]

전용 development Video Batch 자원이 준비될 때 추가할 canary는 production 이름을
거부하고 다음 체인을 한 번의 disposable job으로 증명해야 한다. 현재 구현 및 릴리스
workflow 연결은 없다.

1. 상시 개발 API 컨테이너의 instance role로 `SubmitJob`을 호출한다.
2. Batch job role로 `/academy/workers/development/...` SecureString을 읽는다.
3. worker가 `academy_api_development` DB/역할로 연결하고 개발 Redis set/get/delete를 한다.
4. `academy-development-*` R2 객체를 put/get/delete한다.
5. job `SUCCEEDED`와 R2 object 잔여 0을 확인한다. timeout job은 terminate 후 terminal
   상태를 확인하지 못하면 실패한다.

구현 entrypoint는 `scripts/v1/run-video-development-canary.ps1`로 고정하며 dedicated
queue/job definition, versioned workers env parameter, development bucket, expected DB
name/user와 release ID를 필수 인자로 받아야 한다. 전용 queue/job definition, 최소 권한
job role, Batch에서 접근 가능한 개발 Redis가 모두 구성되고 해당 도구가 실제로 통과하기
전에는 env의 Video Batch 두 값을 채우거나 workflow에 연결하지 않는다. 운영 queue/job
definition, 운영 worker env, 운영 R2를 대체재로 사용하지 않는다.

개발 검토 중 실패는 운영 배포 차단 사유다. 개발 게이트를 skipped/success 이외의 상태로
우회하거나 후보를 운영 인스턴스에서 먼저 시험하지 않는다.
