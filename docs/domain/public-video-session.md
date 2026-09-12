# 공개 영상 공간 준비

전체공개영상은 tenant의 시스템 강의와 order=1 차시를 컨테이너로 사용한다.
인증된 owner/admin/teacher/staff의 영상 추가를 지원하며, 해당 tenant 학생에게
공개된다. 다른 tenant와 학생/학부모는 관리용 준비·조회 API를 사용할 수 없다.
프론트 상호작용은 [공개 영상 흐름](../../../frontend/docs/PUBLIC-VIDEO-WORKFLOW.md)이 소유한다.

## API와 데이터

- `GET /api/v1/media/videos/public-session/`: 기존 `{lecture_id, session_id}`를
  반환한다. 아직 강의나 차시가 없으면 HTTP 200 JSON `null`이다. 시스템 강의가
  없으면 기존 `title=전체공개영상` 강의를 조회하며 기존 ID를 보존한다. 조회는
  생성·정규화하지 않는다. DB 오류는 503 `public_video_session_failed`이며 `null`이 아니다.
- 같은 URL의 `POST`: 사용자 추가 동작에서 공개 영상 공간을 준비하고 HTTP 200으로
  ID를 반환한다. tenant row lock과 기존 시스템 강의 생성 소유자, 기존 DB 유일성
  제약을 재사용한다. 최초 생성·구형 강의의 `is_system` 정규화·차시 생성은 하나의
  transaction이며 실패하면 rollback한다. DB 오류는 안전한 503 detail/code로
  전달하고 진단은 tenant ID와 함께 서버에 기록한다. 재시도는 동일 ID를 반환한다.
- POST body로 다른 tenant/lecture/session을 지정하는 계약은 없다. request tenant와
  활성 직원 membership만으로 대상을 결정한다. provider/storage/worker 호출은 없다.
- 구버전 화면의 기존 `upload/init` 및 `youtube` POST도 선택한 order=1 구형 공개
  컨테이너가 조회 소유자와 일치할 때만 같은 생성 소유자로 정규화한다. 이로써
  구형 GET ID로 추가한 새 영상도 `visibility=PUBLIC`이다. 기존 영상의 visibility,
  폴더, 학생, 수강, 파일은 일괄 변경하지 않는다. 일반 차시 업로드 정책은 그대로다.
- 학생 공개 영상 조회는 계속 쓰기 없이 기존 컨테이너 ID와 tenant 범위를 사용한다.

## 호환 배포와 검증

Backend POST 지원이 모든 rolling instance에 반영된 뒤 frontend를 승격한다.
새 frontend를 old backend에 먼저 배포하면 POST 405가 발생한다. 기존 준비 완료
tenant의 old frontend GET payload와 구형 컨테이너 ID는 유지한다. 미준비 tenant의
열린 old frontend 탭은 POST 준비를 호출할 수 없다. 원래 GET 500이던 이 경계가
새 JS 없이 모두 복구되었다고 주장하지 않으며, 현재 배포 소유자가 구버전 탭의
수렴 범위를 확인한다. GET guard 예외나 강제 reload로 우회하지 않는다.

회귀는 `apps/domains/video/tests/test_public_session_safe_method.py`가 실제 HTTP
middleware를 통해 최초 POST→영상 생성→재조회, 구형 GET 무쓰기/POST 정규화,
실패 rollback→재시도, tenant/role 거부, 학생 ID 조회를 확인한다. 동시 준비 검증은
row lock을 지원하는 DB에서만 실행하므로 SQLite skip을 PostgreSQL 성공으로 세지 않는다.
기존 `test_upload_init_folder.py`, `test_youtube_video_source.py`를 함께 사용한다.
브라우저 route mock과 로컬 smoke는 실제 개발환경의 업로드·처리·학생 재생 성공을
대체하지 않는다. release 전 후속 실사용 검증과 공개 영상 접근 readback은 배포 소유자 책임이다.
