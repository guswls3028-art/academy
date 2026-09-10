# 사용자 여정 결과 게이트

**상태:** 현재 실행 계약
**실행 정본:** `scripts/codex/user-journey-registry.json`,
`scripts/codex/check_user_journey_outcomes.py`, 각 저장소 quality gate

## 목적

정적 guard와 거부 테스트만 통과한 변경을 제품 완료로 부르지 않는다. 등록된
일반 제공(GA) 여정의 변경은 유효한 시작 상태에서 실제 역할의 행동, 저장된 제품
결과, reload 후 동일 상태, 하위 화면 projection, 알림 의도, synthetic 데이터
정리까지 한 묶음으로 증명한다. 잘못된 입력이 거부된다는 증거는 이 긍정 결과에
추가되는 경계이지 대체물이 아니다.

첫 registry는 `score-result-messaging`, `omr-manual-descriptive`,
`clinic-state`, `messaging-template-recipient-log`를 소유한다. 제품 규칙 자체는
각각 `docs/domain/exam-grading.md`, `docs/domain/omr.md`,
`docs/domain/clinic-booking.md`, `docs/domain/messaging-alimtalk.md`,
`docs/ssot/messaging-policy.md`가 계속 소유한다. 이 문서는 그 규칙을 복제하지 않고
검증 결과의 구조만 소유한다.

## 실행 계약

Registry 한 항목은 canonical 역할별 actor/projection 결과, GA/Beta 표시, 변경 경로, 시작 상태, 유효 행동, 거부
사례, 저장 결과, reload, projection, 알림 의도, viewport, cleanup, 실행 증거와
소유 문서를 선언한다. GA 항목은 desktop 1366px와 mobile 390px 모두를 요구한다.
Production-shaped synthetic QA에서는 실제 공급자 발송을 비활성화하고 tenant,
사용자, DB row, queue item, object 잔재가 0임을 별도 readback한다.

`seal_status=gap`은 현재 증거의 한계를 숨기지 않는 차단 상태다. 영향받은 제품
경로를 바꾸는 PR은 prose block만으로 통과하지 않는다. `gap -> sealed` 승격과
GA 제품 경로 변경은 모두 그 PR head에서 발급한 유효한 machine receipt가
필요하다. 아직 네 초기 journey는 기존 감사 결과 모두 `gap`이며, score
recipient의 current main은 parent-only라는 차이도 gap에 명시한다.

Registry의 `executable_evidence`는 deterministic regression의 추적된 source이며,
`receipt_policy`는 고정 valid/invalid case와 audience별 projection을 정의한다. 그
목록만으로 persistent-development 검증이 완료됐다고 추론하지 않는다. 변경 PR은
아래 evidence block을 제공하고, CI는 같은 실행에서 발급된 receipt도 별도로
검사한다.

```markdown
<!-- academy-user-journey-evidence-v1 -->
### Journey: score-result-messaging
- Positive action: <실제 역할 행동과 성공 CTA>
- Persisted outcome: <저장된 제품 결과>
- Reload persistence: <reload 후 동일 상태>
- Downstream projection: <소비 화면 간 일치>
- Actors: owner, admin, teacher
- Projection audiences: owner, admin, teacher, staff, student, parent
- Role outcomes: owner=<결과>; admin=<결과>; teacher=<결과>; staff=<결과>; student=<결과>; parent=<결과>
- Viewports: desktop-1366, mobile-390
- Executable evidence: <registry에 등록된 경로>
- Invalid case: <인접한 거부 사례와 무변경 증거>
- Notification intent: <요청 대상과 product mutation 분리>
- Provider sends: disabled_in_synthetic_qa
- Cleanup: <tenant/user/row/queue/object zero readback>
```

`guard only`, `denied only`, `blocked only`, `N/A`, TODO 같은 placeholder는 어떤
필수 결과도 충족하지 않는다. Roles는 실행 role ID인
`owner/admin/teacher/staff/student/parent`만 쓴다. CI가 경로 매핑으로 선택한 모든
journey block, canonical role 결과, 등록된 실행 증거 경로 전체가 없으면 PR은
실패한다. 수동 workflow의 `--registry-only`는 registry 정합성만 검사한다.

Validator는 삭제와 type change를 포함해 base와 현재 registry의 경로 매핑을 합쳐
변경 영향을 계산한다. 기존 journey 삭제, GA를 Beta로 재표시, sealed를 gap으로
되돌리기, actor/projection/evidence/`must_match`/`must_not_match` 축소를 거부한다.
Pattern 교체는 보존되는 positive/negative fixture가 계속 통과할 때만 가능하다.

## Machine receipt

공식 GitHub Actions의 persistent-development runner는 먼저
`test-results/user-journey-machine-results/<journey-id>.json`을 만든다. 결과는
repository/head SHA, backend API와 frontend bundle, messaging/AI/Tools/Video worker
identity, release/deployment/instance, PII-free actor fingerprint, registry case별
action/outcome, persisted/reload hash, audience별 projection, 1366px/390px screenshot,
등록 test runner report, timestamp/expiry를 포함한다. Provider readback은 Alimtalk,
SMS, LMS, message ID, network dispatch가 모두 정수 0이어야 하며 cleanup은 tenant,
user, row, queue, object, preview token, provider request가 모두 정수 0이어야 한다.
알림 의도는 score/messaging의 `explicit_recipients`, OMR의 `no_notification`, clinic의
`separate_optional`을 구분해 notification이 없는 정상 mutation도 거짓 실패시키지 않는다.

Runner report와 viewport artifact가 생성된 뒤 같은 CI run에서 발급한다.

```powershell
python scripts/codex/user_journey_receipts.py issue `
  --machine-result test-results/user-journey-machine-results/score-result-messaging.json `
  --output test-results/user-journey-receipts/score-result-messaging.json
```

Quality gate는 `test-results/user-journey-receipts/*.json`을 재검증해 PR event의 exact
head, current registry contract hash, current run ID/attempt/workflow, report와 screenshot
byte hash를 대조한다. 다른 SHA/run, 만료, 누락/skip/flaky/failure, 서술만 있는 결과,
provider count 또는 cleanup residue가 0이 아닌 결과는 실패 폐쇄한다. 통과한 PII-free
receipt JSON은 exact head가 포함된 CI artifact로 14일 보존한다. Receipt와 등록
test/report runner를 마련하는 선행 PR은 docs/workflow/tests만 바꾸므로 제품
경로 차단 대상이 아니다. 이후 제품 PR은 그 이미 병합된 runner로 receipt를 만들기
때문에 gap bootstrap이 제품 코드를 영구적으로 막지 않는다. Receipt 발급은 provider
실발송을 허용하지 않으며 production release나 배포 승인을 대신하지 않는다.

## 검증

```powershell
python -m unittest scripts.codex.test_user_journey_outcomes -v
python -m unittest scripts.codex.test_user_journey_receipts -v
python scripts/codex/check_user_journey_outcomes.py
```

프런트 저장소의 `scripts/user-journey-registry.json`은 같은 journey ID의 화면,
viewport, Playwright 증거를 소유한다. 두 저장소를 함께 바꾼 경우 양쪽 PR evidence와
`change-risk-and-release-bundle.md`의 exact SHA release bundle을 모두 충족해야 한다.
