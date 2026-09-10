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

PUT timeout/응답 실패는 provider가 object를 쓰지 않았다고 단정할 수 없다. 이 경로는
canonical owner가 없는 exact 128-bit key만 즉시 삭제하고, 즉시 삭제 성공 여부와 무관하게
durable pending intent를 남겨 늦게 나타난 object도 재확인한다. attach transaction의
예상하지 못한 예외도 같은 owner scan과 exact-key 보상 경로를 사용한다. 이미 다른
canonical owner가 같은 key를 참조하면 보상 삭제하지 않는다.

학생번호는 저장소 ownership identity다. soft delete는 원래 학생번호의
`InventoryFolder`/`InventoryFile.student_ps`를 `_del_{student_id}_{old_ps}` tombstone으로
같은 transaction에서 옮기고 restore는 이를 되돌린다. 신규 생성 또는 기존 학생 rename이
과거에 쓰인 학생번호를 claim할 때 exact tombstoned predecessor가 하나뿐이면 남은 legacy
metadata를 그 tombstone으로 격리한 뒤 claim한다. attribution이 복수이거나 모호하면 해당
claim만 실패 폐쇄한다. 구 런타임이 남긴 active replacement 이전 시각의 metadata가 원래
namespace에 공존하는 경우 학생/학부모 list·download는 `409
student_storage_namespace_conflict`로 차단하되, replacement가 정상 claim 뒤 새로 만든
자기 파일은 허용한다. 2026-09-10 production/development PII-free read-only audit에서는 이
legacy conflict 조합이 0건이어서 데이터 migration은 필요하지 않았다.
active replacement 이전의 legacy metadata가 원래 namespace에 남은 상태에서는 replacement의
soft delete나 일반 학생번호 변경도 그 metadata를 자기 tombstone/새 번호로 옮기지 않고
`student_storage_namespace_conflict`로 전체 identity mutation을 되돌린다.

student scope의 folder create/rename/delete, file rename/delete, upload attach와 file/folder
move 최종 metadata write는 같은 student-PS namespace lock 아래에서 활성 owner와 최신
row scope를 다시 읽는다. admin scope도 folder/file create·upload·rename·delete·move가
같은 tenant admin-inventory mutation lock을 사용한다. file/folder move는 student/admin
scope 모두 동일 scope의 mutation을 직렬화하고 전체 folder parent·file folder/key snapshot 및 현재 target ancestry를 다시
검사해 stale copy commit과 parent cycle을 거부한다. overwrite 직전에는 locked current
file set의 성적표 evidence와 owner-pinned Matchup graph도 다시 잠그고 조회하므로 preflight
후 새 보호 연결이 생기면 이동 전체를 `409`로 되돌린다.
PostgreSQL의 `StudentReportedScore.evidence_file` FK는 즉시 검사되는 참조 계약이다. 따라서
성적표 evidence를 추가하는 transaction은 해당 `InventoryFile`에 즉시 참조 잠금을 잡고,
동시에 시작된 overwrite/delete는 그 transaction의 commit 또는 rollback까지 기다린 뒤
최신 보호 graph를 판정한다. deferred FK에 의존해 미확정 evidence를 건너뛰지 않는다.
모든 이동은 논리 경로가 같아도
128-bit fresh destination key에 복사하므로 기존 canonical destination이나 source key를
pre-commit에 덮어쓰지 않는다. 최종 transaction은 exact object-key attachability를 확인한
뒤 DB ownership만 넘긴다. PUT/Copy가 timeout 등으로 성공 여부가 불명확하면 exact key
cleanup intent는 최소
5분의 provider settle window 동안 `PENDING`을 유지한다. 이 기간의 이른 absent-delete가
terminal `CLEANED`로 오판되는 것을 막고, 이후 재처리가 늦게 나타난 객체까지 삭제한다.
R2 copy/PUT 같은 긴 network 작업은 transaction 밖에서 실행하고,
최종 attach/move가 소유권 변경에 져서 실패하면 새 exact object만 owner scan 후 보상
정리한다. 성공 commit 뒤 old/replaced key 삭제 실패는 이미 성공한 이동을 500으로 바꾸지
않고 durable cleanup intent로 재시도한다. 따라서 soft delete/reuse와 겹친 오래된 삭제
요청이 tombstone owner의 파일을 지울 수 없다.
provider copy 실패는 raw 예외를 노출하지 않고 `502 inventory_storage_copy_failed`, final
ownership/topology 재검증 충돌은 `409 inventory_move_conflict`, 예상하지 못한 DB 실패는
`500 inventory_move_commit_failed`로 응답하며 모두 새 detached key 보상 정리를 먼저
수행한다.

검증은 `apps/domains/inventory/tests/test_hardening.py`,
`apps/domains/inventory/tests/test_student_upload_lifecycle_concurrency_pg.py`와
`tests/test_student_reported_scores.py`의 학생·학부모 권한, sibling/tenant·폴더 경계,
R2 업로드 성공, reload, 128-bit key, 메타데이터 실패 exact-key/durable intent 정리,
PUT/attach와 soft/permanent delete의 양방향 PostgreSQL 경쟁, soft delete와 오래된 file
delete 경쟁, move stale-copy/key/path/topology/ancestry/cleanup 회귀를 사용한다. 운영 확인은 개인 파일을 다운로드하지 않고 tenant별 행 수,
MIME/상태 집계와 R2 HEAD의 존재·크기·content-type 일치만 읽는다.
