# 출결 명단

## 목적과 진입점

교직원은 강의의 차시 상세 `출결` 탭에서 현재 테넌트 학생의 출결 상태를
조회·수정한다. 목록 API는 `GET /api/v1/lectures/attendance/`이며
`apps/domains/attendance/views.py`의 `AttendanceViewSet`이 소유한다. 화면 동작은
프런트엔드 [`docs/ATTENDANCE-ROSTER-SAFETY.md`](https://github.com/guswls3028-art/academy-frontend/blob/main/docs/ATTENDANCE-ROSTER-SAFETY.md)에 둔다.

## 차시 퇴원과 강의 전체 퇴원

출결·성적 화면의 퇴원 선택은 `PATCH /lectures/attendance/{id}/`에
`status: SECESSION`, `confirm_secession: true`, `secession_scope`를 보낸다.
새 화면은 매번 `session`(이 차시만)을 기본 선택하며 `lecture`(강의 전체)를
명시적으로 선택할 수 있다. 구 클라이언트의 범위 생략은 기존 `lecture` 동작을
유지하므로 서버를 먼저 배포하고 새 화면을 배포한다. 알 수 없는 범위는 400으로
거절하며 일부 변경을 남기지 않는다.

- `session`: 이 차시의 SessionEnrollment를 제거하고 출결 행은 SECESSION으로
  보관한다. 현재 차시에만 속한 시험·과제 대상은 해제하되 다른 등록 차시와 공유한
  시험 대상은 유지한다. 강의 Enrollment 상태, 다른 차시 출결·등록·시험·과제와
  자동 수납은 유지한다.
- `lecture`: 기존처럼 강의 Enrollment와 자동 수납을 비활성화하고 모든 출결을
  SECESSION으로 바꾸며 시험·과제 대상을 해제한다. 학생 계정 자체의 전체 퇴원과는
  구분된다. 이미 한 차시만 퇴원한 뒤에도 전체 퇴원을 선택할 수 있다.

두 동작은 기존 성적·시청 진도·출결 메모를 보관하며 알림을 발송하지 않는다.
동일 범위 반복 요청은 결과를 유지한다. 일반 출결 상태 변경으로 SECESSION을
되돌리지는 않는다. 영상 접근·재생 토큰 회수는
[차시 영상 권한](session-video-access.md)의 동일 membership 검사를 따른다.

### 차시 명시 재등록

한 차시만 퇴원한 학생은 강의 수강이 ACTIVE인 동안 `수강생 등록`에서 다시
선택할 수 있다. 교직원이 검토·등록을 확정하면
`POST /lectures/attendance/bulk_create/` 또는
`POST /enrollments/session-enrollments/bulk_create/`가 해당 차시 membership을
복구하고, 보관된 출결 행의 상태만 `SECESSION → UNSET`으로 변경한다.
현장·영상 출결은 재등록 후 교직원이 직접 선택한다. 기존 출결 ID·메모·등원
예정·기록 시각·시청 진도와 다른 차시 기록은 보존한다. 이미 membership만 복구되고
SECESSION이 남은 상태도 같은 명시 재등록으로 회복한다. 반복 등록은 행을
중복 생성하거나 이미 편집한 일반 출결을 UNSET으로 되돌리지 않는다.

두 공개 등록 경로만 내부 `allow_session_reregistration`을 사용한다. 학생과
Enrollment를 순서대로 잠그고 현재 테넌트·강의·학생·ACTIVE 상태를 검증한 뒤
출결 행을 잠가 복구한다. 일반 조회·상태 PATCH/PUT·자동 영상 개방 호출은
차시 재등록을 수행하지 않는다. 자동 roster 호출은 SECESSION이면 수납·차시
등록을 변경하기 전에 거절하며, 교사 업무 도우미 실행에서 앞서 변경한 값도
함께 rollback한다. 일반 상태 PATCH/PUT은 계속 409를 반환한다.

개별 출결 수정·삭제와 전체 현장 출석·되돌리기도 같은 Student → Enrollment →
Attendance 순서로 잠근다. 처음 조회한 현재 테넌트의 대상 ID와 관계만 잠그며,
잠금 뒤 대상 관계와 기존 상태 조건을 다시 확인한다. 단건 대상이 삭제되거나
기존 조회 필터에서 제외되면 404를 유지한다. 남아 있는 대상의 학생·수강 강의·차시
관계가 바뀌거나 일괄 대상 일부가 사라지면 409로 전체 작업을 중단한다. 조회 후 새로 추가된 행을
일괄 변경에 포함하지 않으며, 되돌리기 토큰은 잠금 뒤 확인한 실제 직전 상태를
담는다. 기존 조회·객체 권한 검사와 일괄 작업의 상태별 대상 조건은 유지한다.

전체 강의 퇴원으로 Enrollment가 INACTIVE이거나 PENDING이면 두 차시 등록 API는
400을 반환하고 수납과 기록을 유지한다. 먼저 기존 강의 수강 등록 절차로
명시적으로 재등록해야 한다. 새 요청 필드나 데이터 마이그레이션은 없으며,
자동 수납 배정은 기존 차시 등록 계약을 유지한다. 이 재등록은 계정 안내나
입실 알림을 즉시 발송하지 않는다.

검증은 `test_attendance_destroy_roster_cleanup.py`의 범위·기존 전체 퇴원·수납·
공유 시험 보존과 `tests/test_session_withdrawal_video_access.py`의 8차시 영상·
ONLINE 토큰·진도 보존, 두 공개 재등록 API·반복·일반 출결 저장·조회 무변경으로
수행한다. `teacher_app/tests/test_ops_assistant.py`는 자동 실행의 퇴원 복구 거절과
프로필 변경 rollback을, `tests/test_enrollment_lifecycle_concurrency_pg.py`는
동시 재등록 중복 방지와 등록 대 출결 수정·삭제·전체 현장·되돌리기의 실제
PostgreSQL 잠금 경합을 검증한다. `test_bulk_present_undo.py`는 잠금 뒤 최신 상태,
대상 관계 변경 시 전체 중단, 조회 후 추가 행 제외와 기존 대상 조건을 검증한다.

## 목록 정렬과 페이지네이션

목록은 항상 현재 요청의 테넌트와 삭제되지 않은 학생으로 먼저 범위를 제한한다.
그 범위 전체를 정렬한 뒤 페이지네이션하므로 50명을 넘는 차시에서도 페이지 간
순서가 이어진다. `ordering`이 없거나 허용되지 않은 값이면 이름 가나다순을
적용한다.

허용 값은 `name`, `status`, `parent_phone`, `phone`, `id`와 각각의 내림차순
형태(`-` 접두사)다. 상태는 화면의 운영 순서인 미입력, 현장, 영상, 보강, 지각,
조퇴, 결석, 출튀, 자료, 부재, 퇴원 순으로 정렬한다. 같은 값은 이름과 ID를
보조 키로 사용해 페이지 이동과 재조회에서도 순서가 흔들리지 않게 한다.

정렬은 조회 표현만 바꾸고 출결 행이나 학생 데이터를 수정하지 않는다. 검색과
상태 필터는 동일한 테넌트 범위 안에서 정렬과 함께 적용된다. 수정·삭제 요청의
행 잠금 쿼리에는 목록 정렬을 적용하지 않는다.

## 강의 명단 Excel 다운로드

강의 수강생 화면의 `엑셀 다운로드`는
`POST /api/v1/lectures/attendance/excel/`로 현재 테넌트와 강의를 검증한 뒤,
Tools worker가 요청 시점의 명단·차시·출결을 새 `.xlsx` 스냅샷으로 만든다.
완료 작업의 `result.download_url`은 1시간 동안만 유효하다. 따라서 다운로드를
다시 누르면 과거 완료 작업이나 만료 URL을 재사용하지 않고 새 작업과 새 URL을
발급해야 한다.

R2 서명 URL은 `Content-Disposition: attachment`와 RFC 5987 UTF-8 파일명,
XLSX MIME(`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`)을
응답 오버라이드로 포함한다. 교차 출처 링크의 `download` 속성을 무시하는 iOS
Safari에서도 XML 응답 화면으로 이동하지 않고 파일 다운로드로 처리하기 위한
서버 계약이다. 작업 실패, 만료 링크, 또는 결과 URL 누락은 성공 파일로 취급하지
않으며 클라이언트는 실패로 표시한다.

## 권한과 실패 경계

- 인증된 현재 테넌트 교직원만 목록을 조회할 수 있다.
- 다른 테넌트의 출결은 정렬·검색 결과와 전체 개수에 포함하지 않는다.
- 허용되지 않은 정렬 문자열은 ORM 필드로 전달하지 않고 기본 이름순으로
  복구한다.
- 빈 목록은 정상적인 페이지 응답으로 반환하며 기존 출결 데이터는 보존한다.

## 검증

```powershell
python -m pytest apps/domains/attendance/tests/test_attendance_list_ordering.py
python -m pytest apps/domains/attendance/tests/test_attendance_excel_export.py
python -m ruff check apps/domains/attendance/views.py `
  apps/domains/attendance/tests/test_attendance_list_ordering.py `
  apps/domains/attendance/tests/test_attendance_excel_export.py `
  apps/infrastructure/storage/r2.py `
  academy/application/use_cases/ai/pipelines/excel_export_handler.py
```

핵심 회귀는 테넌트 격리, 페이지네이션 이전 전체 이름순, 상태 운영 순서,
오름·내림차순, 잘못된 정렬값의 안전한 기본값 복구와 반복 다운로드의 새 작업,
실제 XLSX 파싱, iOS 안전 다운로드 헤더다.
