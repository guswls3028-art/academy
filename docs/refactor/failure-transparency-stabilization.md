# 실패 은폐·정상 이용 복구 점검

**상태:** 1차 정적 리뷰, repo-confirmed / runtime-unverified. 제품 수정·운영 재현 미실시.
**기준:** 2026-09-12, backend `ea0b3ae7866773d0f0a9d8b44056bb3cd68202ab`,
frontend `a13ad36ed7d4a8e976c3d0d2874da73e825d21a0`.

이 문서는 긴급 픽스 이후 지휘통제 release owner가 안정화 작업 범위를 배정할
후보다. 전체 기능을 점검했거나 아래 문제가 운영에서 재현됐다는 기록이 아니다.
성공 기준은 [변경 위험·실사용 감사 계약](../operations/change-risk-and-release-bundle.md),
배포 시점은 [배포 시점과 연속성](../operations/deployment-modes.md)을 따른다.
현재 수정 중인 클리닉·성적·OMR·급여 파일은 해당 소유자가 공유한 exact diff를
기준으로 별도 검토하며 이 점검에서 변경하지 않았다.

## 확인한 후보

### P1: 첫 공개영상 준비 요청이 읽기 전용 HTTP 가드와 충돌

- `apps/domains/video/views/video_views.py:878-895`의 GET `public-session`이
  `apps/support/video/view_dependencies.py:14-22`의 시스템 강의/차시 생성을 호출한다.
  시스템 컨테이너가 없거나 레거시 강의를 갱신해야 하는 tenant에서는 DB 쓰기가 필요하다.
- `apps/api/config/settings/base.py:200`의 safe-method middleware는 그 쓰기를
  차단한다. 교사 frontend `src/app_teacher/domains/videos/api.ts:80-86`은 요청
  실패를 `null`로 바꿔 첫 공개영상 업로드/YouTube 등록 준비의 실제 오류를 감춘다.
- 기존 `apps/domains/video/tests/test_upload_init_folder.py:48`은 시스템 컨테이너를
  사전 생성한다. 해당 통과는 빈 tenant의 최초 준비 경로를 보장하지 않는다.
- **수정 경계:** 읽기 가드를 유지하고 provisioning을 권한/tenant가 검증된 명시적
  쓰기 요청으로 옮긴다. 기존 GET 소비자와 이전 frontend의 호환 전략이 필요하다.
- **완료 조건:** 빈 tenant에서 실제 middleware를 거친 준비→업로드/YouTube 등록→
  최종 처리→목록/reload/허용 학생 노출. 반복 준비의 중복 생성과 교차 tenant 접근도 검증한다.

### P1: 가입 자동승인 설정의 저장 실패가 성공 응답으로 변환

- `apps/domains/students/views/registration_views.py:494-504`는 `tenant.save()`의
  예외를 `pass`한 뒤 메모리 객체의 요청값을 200으로 반환한다.
- frontend `src/app_admin/domains/students/pages/StudentsRequestsPage.tsx:389`는
  응답을 성공값으로 캐시한다. 사용자는 자동승인을 껐다고 보지만 실제 신규 가입은
  이전 DB 설정을 따를 수 있다.
- `apps/core/tests/test_boolean_parsing_and_settings_views.py:33`의 저장 대역은
  `lambda: None`이므로 저장 실패·DB 재조회 경계를 증명하지 않는다.
- **수정 경계:** 실패를 명시적 오류로 전파하고 프론트 입력/캐시의 복구를 확인한다.
  boolean 파싱·tenant 권한과 정상 저장 계약은 유지한다.
- **완료 조건:** PostgreSQL 저장 실패 주입→이전 설정 보존/오류 표시, 정상 재시도→
  GET/reload 일치→합성 신규 가입의 승인 동작 일치. QA 외부 메시지 발송은 0이어야 한다.

### P2: 교사 영상 조회 실패가 실제 빈 목록으로 표시

- frontend `src/app_teacher/domains/videos/pages/VideoListPage.tsx:80`은 query의
  오류 상태를 소비하지 않는다. `:183-186`은 미정의 데이터를 0건으로 계산하고
  `:281-286`은 첫 영상을 추가하라는 빈 상태를 표시한다.
- 첫 요청이 500/503/네트워크 오류로 끝나면 기존 영상이 사라진 것처럼 보인다.
  확인한 교사 영상 mock에서는 목록 장애→재시도 성공을 증명하는 사례를 찾지 못했다.
- **수정 경계:** 최초 조회 실패, 기존 데이터 갱신 실패, 정상 0건을 구분하고 재시도와
  마지막 정상 데이터의 상태를 표시한다. 불필요한 재업로드를 유도하지 않는다.
- **완료 조건:** 503→복구, 정상 0건, 기존 목록 이후 갱신 실패를 각각 검증한다.
  desktop/390px에서 오류·재시도·정상 목록이 구별되어야 한다.

### P2: 자동발송 지연 저장 중 화면 이동 시 실패와 입력 소실

- frontend `src/app_admin/domains/messages/pages/MessageAutoSendPage.tsx:439`와
  `src/app_admin/domains/messages/components/AutoSendSettingsPanel.tsx:483`에
  동일한 unmount 저장 코드가 있고 실패를 `.catch(() => undefined)`로 버린다.
- 600ms 지연 저장 전에 이동하면 대기 항목을 지운 뒤 별도 API 요청을 보낸다.
  실패 시 사용자는 새 발송 시간이 저장되지 않았다는 사실과 복구할 입력을 잃는다.
- **수정 경계:** 두 화면의 저장 생명주기를 한 소유 경로로 통합하고 이동 뒤에도
  실패/재시도할 입력을 보존한다. 발송 정책·승인 템플릿·outbox 실행은 바꾸지 않는다.
- **완료 조건:** 입력 직후 이동, 저장 실패, 응답 역전, 재시도, reload 실제 값의 일치를
  검증한다. 설정 저장 검증이 실제 알림 발송을 일으키지 않아야 한다.

### P1 검증 후보: 오류 복구의 전체 새로고침과 활성 사용자 연속성

- frontend `src/main.tsx:44-50,61-75`의 chunk/preload 오류와
  `src/shared/ui/ErrorBoundary.tsx:89-97`의 오류 복구가
  `src/shared/utils/hardReload.ts:45-51`의 `window.location.replace`로 이어진다.
  확인한 경로에는 활성 재생·업로드·미저장 입력 확인이 없다.
- 버전 감지 알림의 자동 새로고침 금지와는 다른 경로다. 코드상 reload 가능성은
  확인했지만 실제 재생 중단/초안 유실은 아직 재현하지 않았다.
- 기존 정적 후보가 모든 배포의 HOLD 사유는 아니다. 후보의 이전 hash 자산 보존과
  변경 범위를 확인하고, 독립적인 backend 조회 패치와 frontend 노출 조건을 구분한다.
- 해당 frontend workflow `quality-gate.yml:137-147,614-623`은 현재 `dist/`를
  올리고 `functions/[[path]].ts:706-722`는 현재 `ASSETS`를 조회한다. 확인한
  경로에는 이전 hash 병합/이전 deployment fallback이 없다. immutable/SW cache는
  이미 받은 자산을 도울 뿐 미수신 old hash의 보존 증거가 아니다. Cloudflare 실제
  보존 상태는 아직 확인하지 않았다. 새 bundle만 읽는 asset probe도 이를 보장하지 않는다.
- `public/sw.js:14-21`은 HTML/manifest 일부만 precache하며 JS/CSS는 요청 시
  캐시한다(`:63-75`). `public/teacher-sw.js:23-24`도 전체 asset precache가 없다.
  양 SW의 `skipWaiting`/`clients.claim` 자체는 document reload가 아니다. 미방문
  lazy가 캐시에 없을 수 있다는 조건만 확인했으며 old URL의 실제 200/404, 사용자별
  cache/SW 상태, 활성 화면에서 무관한 import 발생 여부는 unknown이다.
- **다음 판정:** 격리 개발에서 영상 재생/편집 중 관련 없는 lazy chunk 실패를
  주입하고 전체 reload, 재생 지속, 입력 보존, 복구 성공을 측정한다. 필요한 오류
  복구 자체를 제거하거나 테스트에서 오류를 무시하지 않는다.
- **재현 설계:** 동일 origin의 A bundle에서 재생/미저장 입력 유지→B로 교체→
  아직 받지 않은 A의 실제 무관한 lazy chunk 한 건을 404로 처리한다. document
  navigation, video DOM 유지·재생시간 전진, 입력과 저장 요청을 관측하고, old chunk를
  계속 제공하는 비교군을 둔다. 실제 비파괴 background import 경로가 없으면 이벤트
  주입은 handler 단위 위험 증거로만 분류한다. 이 설계는 실행하지 않았다.
- **완료 조건:** 해당 실패에서도 사용자가 정상 복구할 수 있고, 배포 후보의
  무중단 판단에 필요한 실제 활동 연속성 증거가 남아야 한다.

## 실행과 종료

지휘통제가 긴급 픽스와의 겹침을 확인하고 각 후보의 구현 담당을 배정한다. 담당은
최신 기준에서 실패를 재현하고 최소 수정·성공/실패 검증·owning 도메인 문서·정확한
PR/CI를 함께 인계한다. merge/배포는 단일 release owner가 기존 게이트로 수행한다.
고정 04시 대기는 없으나 technical HOLD는 실제 조건 충족 전까지 유지한다.

코드 변경 이후에는 후보의 관찰 결과와 정본 문서 링크를 갱신하고, 실제 배포와
관련 사용자 흐름·합성 QA 잔재 0까지 검증된 항목만 완료로 닫는다. 테스트 개수나
가드 통과만으로 이 목록을 완료 처리하지 않는다.
