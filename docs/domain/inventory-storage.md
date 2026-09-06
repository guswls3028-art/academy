# 인벤토리 파일 저장 계약

교직원 저장소와 학생 인벤토리는 `POST /api/v1/storage/inventory/upload/`로 파일을
받아 storage R2에 원본을 저장하고 `InventoryFile`에 tenant·scope·폴더·원본명·크기·
MIME·R2 key를 기록한다. 교직원은 admin/student scope를 사용할 수 있고 학생은 자신의
student scope만 사용할 수 있다. 폴더와 파일 조회·이동·삭제도 같은 tenant와 scope를
벗어나면 실패 폐쇄한다.

일반 업로드는 PDF·Office·텍스트·ZIP과 `image/*`, `video/*`를 허용하고 파일당 2GB,
tenant당 200GB 한도를 적용한다. 성적표 제출과 매치업 승격은 별도 제한으로
PDF/PNG/JPEG만 허용한다. 브라우저가 보내는 MIME은 서버가 다시 검사하며, 허용되지
않는 형식·용량·폴더·권한 오류는 R2 쓰기 전에 거부한다.

정상 순서는 R2 원본 업로드 뒤 `InventoryFile` 생성이다. 원본 업로드 뒤 DB 메타데이터
생성이 실패하면 방금 생성한 exact R2 key를 즉시 삭제하고
`500 inventory_metadata_save_failed`를 반환한다. 그 삭제까지 실패하면 임의 재시도나
성공 응답 대신 `502 inventory_storage_cleanup_failed`로 운영 확인을 요구한다. 성적표의
후속 점수 행 생성 실패도 같은 원본과 메타데이터를 보상 정리한다. 다른 tenant·기존
파일·사용자 작성 행은 이 보상 범위에 포함하지 않는다.

검증은 `apps/domains/inventory/tests/test_hardening.py`의 권한·폴더 경계, R2 업로드 성공,
메타데이터 실패 exact-key 정리 회귀를 사용한다. 운영 확인은 개인 파일을 다운로드하지
않고 tenant별 행 수, MIME/상태 집계와 R2 HEAD의 존재·크기·content-type 일치만 읽는다.
