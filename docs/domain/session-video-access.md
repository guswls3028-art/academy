# 차시별 영상 권한

일반 강의 영상은 해당 tenant의 활성 학생·ACTIVE 강의 수강등록과 **정확한
SessionEnrollment**가 모두 있어야 시청할 수 있다. N차시 등록은 앞뒤 차시에
권한을 주지 않는다. 출결·진도·영상별 재생 설정은 차시 등록을 대신하지 않는다.

학생 및 학부모의 선택 자녀는 영상 홈에서 등록된 차시만 보고, 영상 수·시간·진도
집계도 그 차시를 기준으로 받는다. 직접 URL을 입력하더라도 차시 목록, 재생 시작,
access_check, 진도·댓글·좋아요 등 공통 context 소비자는 같은 권한을 확인한다.
SessionEnrollment의 tenant, 수강·학생·강의 tenant와 강의 일치가 모두 필요하다.

차시 등록을 해제하면 해당 차시의 새 재생과 기존 토큰 refresh/renew/heartbeat는
403으로 거절된다. 다른 등록 차시는 계속 정상 이용할 수 있다. 강의 수강등록을
INACTIVE로 바꾸면 일반 수강 권한은 전체 회수된다. 출결·성적·진도 이력은 이
권한 검사 변경으로 삭제하거나 다시 작성하지 않는다.

명시적 PUBLIC 영상과 시스템 공개 영상 공간은 기존 공개 계약을 유지한다.
교직원이 별도 승인한 inactive/direct 영상 권한도 기존 정확한 영상 단위 계약을
유지한다([direct-video-access.md](direct-video-access.md)). 일반 ACTIVE 수강의
차시 누락을 그 예외로 자동 보완하지 않는다.

판정 소유자는 `academy/application/use_cases/student_video_access_context.py`,
차시 관계 조회는 `repositories_video.video_session_memberships` 및
`session_enrollment_exists`, 토큰/발급 시 재검사는 `get_effective_access_mode`다.
학생 영상 홈·통계는 동일 관계 조회로 범위를 제한한다.

DB migration이나 API 응답 형식 변경은 없다. 구 frontend도 서버에서 같은 차시
검사를 받으며 페이지 강제 새로고침이나 일괄 세션 초기화를 요구하지 않는다.
이미 발급된 CDN URL 자체는 그 서명 만료까지 유효할 수 있으며, API의 즉시
권한 회수와 CDN 캐시/이미 내려받은 데이터의 삭제를 동일한 보장으로 취급하지 않는다.

검증: `apps/domains/video/tests/test_session_video_entitlement.py`는 8차시 중
3차시만 등록, 홈/통계 범위, 정상 재생 후 차시 해제와 기존 토큰 재검사, 다른
차시 유지, 강의 INACTIVE, 공개 영상, 다른 tenant 관계를 실제 DB/API로 검증한다.
`tests/test_video_access_security.py`, `tests/test_direct_video_entitlement.py`와
inactive entitlement 회귀를 함께 실행한다. 운영 모양의 로그인·브라우저/CDN 검증은
격리 개발 tenant에서 수행하고 tenant/user residue zero를 확인한다.
