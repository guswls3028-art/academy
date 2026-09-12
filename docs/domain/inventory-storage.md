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

## 일반 삭제의 DB·원본 정리 경계

파일 삭제와 폴더 삭제는 삭제 대상 metadata를 잠그고 tenant·scope·학생 범위를 다시
확인한다. 기본 폴더 삭제는 잠긴 트리에서 비어 있음을 확인한 경우만 허용하며,
하위 항목을 지우려면 기존 `recursive=true`가 필요하다. 다른 tenant나 학생의 행이
잘못 연결된 cascade는 `409 inventory_delete_scope_invalid`로 전체 중단한다.
검수 성적표 원본과 매치업의 수동/소유자 고정 문제 보호는 그대로 `409`이며,
파일 DELETE는 pending/verified 원본을 보호하고, void/반려 원본은 검수 상태·사유
감사행을 남기고 파일 연결만 해제한다. UI 다중 선택 삭제도 각 파일 DELETE를 호출하므로
동일한 파일 규칙을 적용한다. 재귀 폴더 cascade·기존 덮어쓰기 등 whole-tree 경로는
상태와 무관하게 모든 성적표 감사 연결을 보호한다. 원본마다 별도 UI 확인을 추가하거나
기존 다중 선택 삭제를 새 가드로 막지 않는다.

정상 삭제는 Inventory metadata/cascade와 `SubmissionStorageCleanupIntent`의 exact
storage key 기록을 **같은 DB 트랜잭션**에서 수행한다. DB 삭제나 intent 저장 또는
바깥 트랜잭션이 롤백되면 원본·감사 연결을 유지하고 공급자 DELETE는 0회다.
정리 대상은 선택한 파일·매치업 문서 원본과 연결된 매치업 문제/분리 제안 이미지,
`public_cleanup.public_image_key`, 문서의 `page_image_keys`뿐이다. 버킷 prefix를
열거하지 않는다. 기존 global key도 정확한 소유 metadata가 있으면 처리하되,
명시적인 `tenants/<다른 tenant>/` key는 거부한다. 새 namespace로 이동하거나
학생 영구삭제·이동·업로드 경로를 함께 변경하지 않는다.

외부 DELETE는 바깥 commit 이후 기존 outbox processor가 수행한다. intent는
tenant·bucket·exact key로 중복을 막고, 기존 scalar/매치업 JSON 참조를 모든 tenant에서
검사한다. 살아 있는 다른 소유자가 있으면 그 객체를 보존한다. commit 뒤 새 소유자가
확인돼도 정리는 보류한다. PostgreSQL에서는 기존 key advisory lock/claim token을
재사용한다. 기존 소유자 검사·claim lease·retry 정책을 유지하며, 아직 협력 key lock을
쓰지 않는 모든 attachment writer의 동시성까지 새로 보장한다는 의미는 아니다.

commit 후 공급자 실패/정리 보류는 DB 삭제를 되돌리지 않는다. 보통의 HTTP 요청은
`502 inventory_storage_cleanup_pending`으로 **목록에서 삭제됨·원본 정리 재시도 대기**를
명시한다. 단건 파일 응답은 `deleted: true`, 폴더는 기존 `deleted` 통계 객체를 유지하고,
둘 다 `storage_cleanup: {pending, failed, cleaned}` 수치를 반환한다. 폴더의 `r2_objects`는
실제 cleaned 수이며 DB 행 삭제 수와 같다고 가정하지 않는다. 모든 원본 정리가 끝난
기존 성공 경로는 파일 `204`/폴더 `200` 그대로다. 더 바깥 atomic 안의 내부 호출에서는
callback 전 응답이 생성되므로 pending은 아직 미실행 상태이며 정리 완료 증거가 아니다.

화면은 오류 응답 뒤에도 정본 목록을 재조회하여 실제 삭제 상태를 반영해야 한다.
실패를 “원본과 목록 모두 그대로”로 설명하거나 같은 DELETE를 자동 재전송하지 않는다.
남은 intent는 기존 `process_submission_storage_cleanup --limit 100` 및 학생 purge의
동일 정리 processor로 재시도한다. 한 번에 최대 100개인 기존 callback 한도를 넘긴
폴더도 남은 intent를 보존한다. cleaned key는 다시 지우지 않으며 failed/deferred만
정리한다. 상세 계약은 [학생 핵심 계약](student-core.md)의 영구삭제 outbox와 공유한다.

일반 업로드는 PDF·Office·텍스트·ZIP과 `image/*`, `video/*`를 허용하고 파일당 2GB,
tenant당 200GB 한도를 적용한다. 성적표 제출과 매치업 승격은 별도 제한으로
PDF/PNG/JPEG만 허용한다. 브라우저가 보내는 MIME은 서버가 다시 검사하며, 허용되지
않는 형식·용량·폴더·권한 오류는 R2 쓰기 전에 거부한다.

정상 순서는 R2 원본 업로드 뒤 `InventoryFile` 생성이다. 원본 업로드 뒤 DB 메타데이터
생성이 실패하면 방금 생성한 exact R2 key를 즉시 삭제하고
`500 inventory_metadata_save_failed`를 반환한다. 그 삭제까지 실패하면 임의 재시도나
성공 응답 대신 `502 inventory_storage_cleanup_failed`로 운영 확인을 요구한다. 성적표의
후속 점수 행 생성 실패도 같은 원본과 메타데이터를 보상 정리한다. 학부모가 제출한
성적표는 선택 자녀를 `student`, 인증된 학부모를 `submitted_by`로 기록하고 학생/학부모/
교직원 조회가 동일한 원본과 점수 행을 반환한다. 다른 tenant·기존
파일·사용자 작성 행은 이 보상 범위에 포함하지 않는다.

검증은 `apps/domains/inventory/tests/test_hardening.py`와
`tests/test_student_reported_scores.py`의 학생·학부모 권한, sibling/tenant·폴더 경계,
R2 업로드 성공, reload, 메타데이터 실패 exact-key 정리 회귀를 사용한다. 운영 확인은 개인 파일을 다운로드하지
않고 tenant별 행 수, MIME/상태 집계와 R2 HEAD의 존재·크기·content-type 일치만 읽는다.
삭제 회귀는 `apps/domains/inventory/tests/test_delete_durability.py`의 실제 outer-commit
응답, DB/intent 롤백, 연결 이미지 exact-key, global/shared/타tenant 보호, 실패 후 재시도와
목록 재조회, 기존 학생 영구삭제 outbox 회귀를 함께 실행한다. 공급자 호출은 mock으로
격리하며 SQLite 통과를 PostgreSQL 잠금 동시성이나 실제 R2/화면 검증으로 간주하지 않는다.
