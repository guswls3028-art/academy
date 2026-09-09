# 인벤토리 파일 저장 계약

교직원 저장소와 학생 인벤토리는 `POST /api/v1/storage/inventory/upload/`로 파일을
받아 storage R2에 원본을 저장하고 `InventoryFile`에 tenant·scope·폴더·원본명·크기·
MIME·R2 key를 기록한다. 교직원은 admin/student scope를 사용할 수 있고 학생은 자신의
student scope만 사용할 수 있다. 학부모도 현재 tenant의 활성 연결 자녀를
`X-Student-Id`로 명시하면 그 자녀의 student scope에서 폴더 생성, 파일 업로드·조회·
수정·이동·삭제와 성적표 제출을 같은 API로 수행할 수 있다. 모든 경로가 헤더의 자녀와
`student_ps`를 함께 검증하며 둘이 다르거나 헤더가 누락·미소유·삭제·다른 tenant이면
R2/DB 쓰기 전에 `403`으로 실패 폐쇄한다. 교직원 조회는 같은 자녀 scope 행을 그대로
보므로 별도 학부모 전용 사본은 만들지 않는다.

빈 폴더 또는 보호되지 않은 파일의 일반 삭제가 성공하면 응답은 본문 없는 `204 No
Content`다. `204`에 JSON `{}`나 다른 본문을 붙이지 않으며, 전송 길이는 없거나 0이어야
한다. 재귀 폴더 삭제의 정리 결과는 기존처럼 JSON 본문이 있는 `200`으로 반환한다.

일반 업로드는 PDF·Office·텍스트·ZIP과 `image/*`, `video/*`를 허용하고 파일당 2GB,
tenant당 200GB 한도를 적용한다. 성적표 제출과 매치업 승격은 별도 제한으로
PDF/PNG/JPEG만 허용한다. 브라우저가 보내는 MIME은 서버가 다시 검사하며, 허용되지
않는 형식·용량·폴더·권한 오류는 R2 쓰기 전에 거부한다.

정상 순서는 R2 원본 업로드 뒤 `InventoryFile` 생성이다. 원본 업로드 뒤 DB 메타데이터
생성이 실패하면 방금 생성한 exact R2 key를 즉시 삭제하고
`500 inventory_metadata_save_failed`를 반환한다. 그 삭제까지 실패하면 임의 재시도나
성공 응답 대신 durable Storage cleanup intent를 남긴 뒤
`502 inventory_storage_cleanup_failed`로 운영 확인을 요구한다. 성적표의
후속 점수 행 생성 실패도 같은 원본과 메타데이터를 보상 정리한다. 학부모가 제출한
성적표는 선택 자녀를 `student`, 인증된 학부모를 `submitted_by`로 기록하고 학생/학부모/
교직원 조회가 동일한 원본과 점수 행을 반환한다. 다른 tenant·기존
파일·사용자 작성 행은 이 보상 범위에 포함하지 않는다.

업로드 key의 랜덤 suffix는 128-bit(`token_hex(16)`)이며 Inventory helper를 공유하는
매치업 경로도 같은 충돌 저항을 사용한다. 최대 2GB PUT 동안 DB transaction을 열어두지
않는다. 서버는 권한·활성 학생·폴더·quota를 먼저 cheap preflight하고 R2 PUT 뒤 짧은
attach transaction을 연다. student scope는
`academy:student-ps-namespace:v1:{tenant_id}:{student_ps}`를 잠근 뒤 Student row lock
없이 활성 owner/선택 자녀를 다시 읽고, 선택 folder row와 quota를 재검증한다. 이어 exact
Storage object-key lock을 잡아 cleanup intent와 모든 canonical owner가 없음을 확인한 뒤
`InventoryFile`과 성적표 점수 행을 한 번에 commit한다. PUT 사이에 학생이 삭제되거나
folder/quota/key ownership이 달라지면 성공으로 추측하지 않고 `409`/`403`으로 attach를
거절한 뒤 방금 PUT한 exact key를 보상 삭제한다. provider 원문 예외는 노출하지 않고
`502 inventory_storage_upload_failed`를 반환한다.

검증은 `apps/domains/inventory/tests/test_hardening.py`,
`apps/domains/inventory/tests/test_student_upload_lifecycle_concurrency_pg.py`와
`tests/test_student_reported_scores.py`의 학생·학부모 권한, sibling/tenant·폴더 경계,
R2 업로드 성공, reload, 128-bit key, 메타데이터 실패 exact-key/durable intent 정리,
PUT/attach와 삭제의 양방향 PostgreSQL 경쟁 회귀를 사용한다. 운영 확인은 개인 파일을 다운로드하지
않고 tenant별 행 수, MIME/상태 집계와 R2 HEAD의 존재·크기·content-type 일치만 읽는다.
