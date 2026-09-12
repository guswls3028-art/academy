# 변경 위험 라우팅·교차 저장소 릴리스 증거 계약

**상태:** 현재 실행 계약
**실행 정본:** `scripts/codex/get-change-risk-plan.ps1`,
`scripts/codex/assert-production-release-bundle.ps1`, 각 저장소의 Git/GitHub
Actions와 backend release manifest

## 1. 목적과 비목적

반복 장애를 막기 위해 변경 범위에서 필요한 기존 검증을 빠짐없이 선택하고,
backend와 frontend를 함께 바꾼 제품 작업의 최종 운영 증거를 exact SHA 단위로
묶는다.

프론트엔드 배포·rollback 소유 계약은
[Frontend DEPLOYMENT-OPERATIONS](https://github.com/guswls3028-art/academy-frontend/blob/main/docs/DEPLOYMENT-OPERATIONS.md)에
두고, 이 문서는 두 공식 release evidence를 함께 읽는 경계만 소유한다.

이 계약은 별도 작업 큐, 병렬 agent registry, 수동 상태 파일을 만들지 않는다.
Git branch/worktree, GitHub run과 `pending_deployments`, DynamoDB production
mutation lock, backend의 검증된 release manifest, frontend의 운영
`version.json`에서 현재 상태를 읽는다. 두 저장소는 독립 Git 저장소이므로 하나의
transaction이나 하나의 통합 SHA가 존재한다고 표현하지 않는다.

## 2. 변경 위험 라우팅

`get-change-risk-plan.ps1`은 지정한 base ref부터 현재 branch와 미커밋 변경까지의
경로를 읽고 필요한 기존 게이트를 계획한다. `-RunLocalGates`를 주면 계획된 로컬
게이트를 그대로 실행한다.

```powershell
pwsh scripts/codex/get-change-risk-plan.ps1 `
  -BackendRoot C:\academy\_worktrees\sessions\<session>\backend `
  -FrontendRoot C:\academy\_worktrees\sessions\<session>\frontend

pwsh scripts/codex/get-change-risk-plan.ps1 `
  -BackendRoot C:\academy\_worktrees\sessions\<session>\backend `
  -FrontendRoot C:\academy\_worktrees\sessions\<session>\frontend `
  -RunLocalGates
```

| 감지 위험 | 최소 증거 |
|-----------|-----------|
| 문서만 변경 | owning 문서 정합성, 링크/경로, `git diff --check` |
| backend 제품 코드 | 실패 재현 회귀, Django/core gates, PostgreSQL tenant CI |
| backend/frontend runtime·build 설정 | 각 저장소 core·배포 계약, 양쪽 변경 시 production release bundle |
| migration | expand/contract guard와 구·신 runtime 공존 설명 |
| worker/queue | producer→outbox/queue→worker→최종 상태·DLQ readback |
| frontend 사용자 화면 | type/lint/build, PR E2E, desktop·390px live readback |
| 배포/governance | 기존 `scripts/v1/test-*-contract.ps1`와 frontend governance guard |
| backend+frontend 제품 계약 | 일시적 교차 버전 호환성과 최종 production release bundle |

라우터는 제품별 focused test 이름을 추측하지 않는다. 담당 작업은 실제 실패를
먼저 재현하는 focused regression을 추가하고, 라우터는 공통·운영 게이트의 누락을
막는다. 테스트 개수나 mock E2E 성공만으로 PostgreSQL, tenant, worker, 운영 UI
증거를 대체하지 않는다.

`docs/`, `AGENTS.md`, `README*.md`, `CONVENTIONS.md` 같은 관례적 문서와 명시적
테스트 경로는 runtime/build 판정보다 먼저 제외한다. worker 위험은 `ai`, `queue`,
`worker` 등의 정확한 경로 segment로만 판정한다. frontend의 `tsconfig*.json`과
`eslint.config.*`는 runtime/build 설정으로 라우팅한다. 그 밖의 변경은 알려진
제품/runtime/build/governance 범주에 반드시 속해야 하며, 새 비문서 경로가 어느
범주에도 속하지 않으면 diff-check만으로 통과시키지 않고 계획 생성을 거부한다.

## 3. 작업 범위 계약

제품 mutation 전 다음을 작업 설명 또는 PR 본문에 고정한다.

- 하나의 사용자 여정과 관찰된 실패
- 수정 범위와 명시적 비범위
- tenant, 권한, 데이터 보존, 중복 실행 같은 불변조건
- 실패를 재현하는 focused regression
- 필요한 PostgreSQL, worker, desktop/390px, 운영 readback

`모든권한`이나 배포 권한은 승인 범위이며 제품 변경 범위를 넓히지 않는다. 인접
문제를 발견해도 직접 원인이 아니면 증거만 남기고 별도 소유권을 배정한다.

### 실패 은폐와 구조 재정비 감사

안정화의 기준은 사용자가 기대한 정상 이용의 복구다. 테스트가 통과하거나 오류가
보이지 않는다는 사실만으로 완료하지 않는다. 먼저 역할·진입 CTA·정상 입력·예상
저장 상태와 후속 화면을 정하고, 실제 호출 경로에서 아래 경계를 추적한다.

| 감사 대상 | 확인할 실패 | 수정·완료 증거 |
|-----------|-------------|----------------|
| disabled·guard·권한/상태 분기 | 정당한 사용도 차단되는지, 해제 조건을 충족할 수 있는지 | 허용 대상의 성공과 금지 대상의 차단을 함께 검증 |
| catch·fallback·빈 목록 | 실패가 빈 데이터·0·완료 응답으로 바뀌는지 | 오류/빈 상태 구별, 초안 보존, 복구 후 실제 저장·reload |
| retry·timeout·비동기 작업 | 최초 실패 소실, 중복 요청/발송, queue 접수를 완료로 오인 | 최초 오류와 최종 상태 구별, 정확한 payload·중복 방지·worker 결과 |
| 테스트·가드 | skip/early return/mock이 성공 여정을 생략하는지 | 정상 CTA→실제 API/저장→소비 역할 반영; mock의 보장 범위 명시 |
| 우회·중복·낭비 구현 | 복제된 정책, 불필요 조회/대기, 서로 다른 상태 정본 | 호출자·호환성·데이터 경계 확인 후 owning 구현으로 통합 |

정당한 tenant/auth/수동 데이터/메시징 보호나 유효한 테스트를 약화해 통과시키지
않는다. 잘못되거나 중복된 가드·기대값은 소유 계약과 정상·금지 경계 검증에 따라
수정하거나 제거하여 정상 경로를 막는 원인을 고친다.
재시도 가능한 일시 오류는 한정된 복구를 허용하되 실패를 성공으로 바꾸지 않는다.
이미 공개된 기능의 차단 안내는 수정 완료가 아니다. 기존 Beta 제한은 진입 전에
명시된 범위에서만 인정하고, 회귀를 정당화하려고 Beta로 바꾸지 않는다.

각 발견에는 exact 기준 SHA와 path/line, 재현 조건, 사용자 영향, 누락된 검증,
현재 상태(repo-confirmed/runtime-unverified 등), 담당 경계와 다음 판정을 기록한다.
정적 코드 감사는 운영 재현이나 전수 점검 완료로 표현하지 않는다. 긴급 수정과
겹친 제품 파일은 release owner가 현재 diff를 공유한 뒤 리뷰한다.

검증은 관찰 가능한 실패·성공 경계에 맞춘 focused 회귀와 기존 필수 게이트를
사용한다. 동일 후보의 통과 증거는 재사용하고 새 변경·실패·미해결 위험이 없으면
반복하지 않는다. 중복 테스트 제거는 각 테스트가 보장한 사용자 결과와 독립
경계를 대조한 후 수행한다. 규칙 문구를 검색하는 검사로 제품 성공을 대체하지 않는다.

## 4. production release bundle

backend와 frontend를 함께 바꾼 사용자 여정은 각 저장소의 공식 릴리스가 끝난 뒤
다음 명령으로 최종 증거를 검증한다.

```powershell
pwsh scripts/codex/assert-production-release-bundle.ps1 `
  -BackendSha <40-char-backend-sha> `
  -BackendRunId <backend-production-run-id> `
  -FrontendSha <40-char-frontend-sha> `
  -FrontendRunId <frontend-production-run-id> `
  -AwsProfile <approved-operator>
```

여러 운영 tenant/domain에 함께 배포되는 변경은 영향을 받는 모든
`version.json` URL을 `-FrontendVersionUrls`로 넘긴다.

명령은 다음을 모두 만족해야만 통과한다.

1. 두 SHA가 각 현재 `origin/main`의 ancestor다.
2. backend run은 `V1 Build and Push latest (OIDC)`의 `main` production run이고
   `Verify deployment`, `Release shared production mutation lock`이 성공했다.
3. backend run의 `pending_deployments`가 0이며, `origin/main`의
   `release-manifest.latest.json`이 `complete: true` 실제 Boolean과
   `status: successful`을 갖고 exact backend SHA를 포함하는 현재
   `origin/main` descendant를 가리킨다.
4. DynamoDB `__deployment_control_v2__` lock readback은 Item이 없거나,
   nonblank `owner.S`와 정수 `ttl.N`을 정확히 갖는다. 각 AttributeValue의 property
   set도 owner는 `{S}`, ttl은 `{N}`만 허용하며 다른 type이나 추가 field가 있으면
   malformed Item으로 거부한다. 정상 Item은 만료된 경우에만 통과한다.
5. frontend run은 `Frontend Quality Gate`의 `main` push run이고
   `Deploy to Cloudflare Pages`, `E2E 왕복 테스트 + tenant availability`가
   성공했다.
6. frontend run의 `pending_deployments`가 0이며 지정 운영 `version.json`이
   exact frontend SHA 또는 이를 포함하는 현재 `origin/main` descendant를
   반환한다.

공식 run의 `headSha`는 항상 입력한 exact SHA와 같아야 한다. descendant 허용은
그 뒤의 정상 직렬 배포가 현재 runtime을 전진시킨 경우에만 적용하며, unrelated
branch나 `origin/main`에 없는 revision은 거부한다.

run 성공만 확인하거나, GitHub 승인 요청이 남아 있거나, manifest가 이전 SHA를
가리키거나, lock owner가 살아 있거나, 운영 version이 전파되지 않은 경우에는
fail closed한다. 결과는 readback 출력일 뿐 새 SSOT로 커밋하지 않는다.

## 5. 소유권과 인계

개별 작업은 기존 [동시 Codex 세션 계약](concurrent-codex-sessions.md)의 branch와
worktree를 소유권 기록으로 사용한다. release owner가 아닌 작업은 clean commit,
PR, exact SHA, CI evidence까지만 인계한다. release owner만 공식 run과 production
bundle readback을 수행한다.

이 계약 변경 검증:

```powershell
pwsh scripts/codex/test-change-risk-contract.ps1
pwsh scripts/codex/test-production-release-bundle-contract.ps1
pwsh scripts/v1/test-workflow-governance-contract.ps1
pwsh scripts/v1/test-verification-contract.ps1
```
