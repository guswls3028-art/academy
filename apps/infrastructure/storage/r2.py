# ==============================================================================
# PATH: apps/infrastructure/storage/r2.py
#
# PURPOSE:
# - API 서버 전용 R2(S3) 접근 레이어
# - AI/Video: R2_AI_BUCKET, R2_VIDEO_BUCKET
# - Storage(인벤토리): R2_STORAGE_BUCKET — upload / copy / delete / presign
# ==============================================================================

from __future__ import annotations

from typing import Callable
from urllib.parse import quote

import boto3
from django.conf import settings


def _get_s3_client(*, timeout_seconds: int | None = None):
    kwargs = {}
    if timeout_seconds is not None:
        from botocore.config import Config

        kwargs["config"] = Config(
            connect_timeout=min(5, timeout_seconds),
            read_timeout=timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT or None,
        aws_access_key_id=settings.R2_ACCESS_KEY or None,
        aws_secret_access_key=settings.R2_SECRET_KEY or None,
        region_name=getattr(settings, "R2_REGION", "auto"),
        **kwargs,
    )


# ---------------------------------------------------------------------
# Upload (AI 버킷 — 기존)
# ---------------------------------------------------------------------

def upload_fileobj_to_r2(
    *,
    fileobj,
    key: str,
    content_type: str | None = None,
) -> None:
    """
    Django UploadedFile -> R2 업로드 (AI 버킷)
    """
    s3 = _get_s3_client()
    s3.upload_fileobj(
        Fileobj=fileobj,
        Bucket=settings.R2_AI_BUCKET,
        Key=key,
        ExtraArgs={
            "ContentType": content_type or "application/octet-stream"
        },
    )


def delete_object_r2_ai(*, key: str, timeout_seconds: int | None = None) -> None:
    """Delete one object from the AI bucket."""
    s3 = _get_s3_client(timeout_seconds=timeout_seconds)
    s3.delete_object(Bucket=settings.R2_AI_BUCKET, Key=key)


def _excel_bucket():
    return getattr(settings, "R2_EXCEL_BUCKET", "academy-excel")


def upload_fileobj_to_r2_excel(
    *,
    fileobj,
    key: str,
    content_type: str | None = None,
) -> None:
    """
    Django UploadedFile -> R2 엑셀 버킷 업로드.
    워커 EXCEL_PARSING job이 동일 버킷에서 다운로드하므로 R2_EXCEL_BUCKET 일치 필요.
    """
    s3 = _get_s3_client()
    s3.upload_fileobj(
        Fileobj=fileobj,
        Bucket=_excel_bucket(),
        Key=key,
        ExtraArgs={
            "ContentType": content_type or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        },
    )


def delete_object_r2_excel(*, key: str) -> None:
    """Delete one rejected or errored Excel dispatch upload."""
    s3 = _get_s3_client()
    s3.delete_object(Bucket=_excel_bucket(), Key=key)


def generate_presigned_get_url_excel(
    *,
    key: str,
    expires_in: int = 3600,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """R2 엑셀 버킷 presigned GET URL (다운로드용)."""
    s3 = _get_s3_client()
    params = {
        "Bucket": _excel_bucket(),
        "Key": key,
    }
    if filename:
        safe_name = filename.replace("\r", "").replace("\n", "").replace('"', "")
        ascii_name = safe_name if safe_name.isascii() else "download.xlsx"
        params["ResponseContentDisposition"] = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(safe_name, safe='')}"
        )
    if content_type:
        params["ResponseContentType"] = content_type
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def generate_presigned_get_url(
    *,
    key: str,
    expires_in: int = 3600,
) -> str:
    """
    R2 object presigned GET URL (AI 버킷)
    """
    s3 = _get_s3_client()
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": settings.R2_AI_BUCKET,
            "Key": key,
        },
        ExpiresIn=expires_in,
    )


# ---------------------------------------------------------------------
# Storage 버킷 (인벤토리) — R2_STORAGE_BUCKET
# ---------------------------------------------------------------------

def _storage_bucket():
    return getattr(settings, "R2_STORAGE_BUCKET", "academy-storage")


def _guess_content_type(key: str, fallback: str | None = None) -> str:
    """파일 확장자로 MIME 타입을 추정. application/octet-stream 대신 정확한 타입 반환."""
    import mimetypes
    guessed, _ = mimetypes.guess_type(key)
    return guessed or fallback or "application/octet-stream"


def upload_fileobj_to_r2_storage(
    *,
    fileobj,
    key: str,
    content_type: str | None = None,
    timeout_seconds: int | None = None,
) -> None:
    """Django UploadedFile -> R2 Storage 버킷 업로드."""
    s3 = _get_s3_client(timeout_seconds=timeout_seconds)
    resolved_type = content_type or _guess_content_type(key)
    s3.upload_fileobj(
        Fileobj=fileobj,
        Bucket=_storage_bucket(),
        Key=key,
        ExtraArgs={
            "ContentType": resolved_type
        },
    )


def generate_presigned_get_url_storage(
    *,
    key: str,
    expires_in: int = 3600,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """R2 Storage 버킷 presigned GET URL.

    filename/content_type를 지정하면 ResponseContentDisposition,
    ResponseContentType이 presigned URL에 포함되어 브라우저가 올바른
    MIME 타입과 파일명으로 다운로드합니다.
    보안 제품이 application/octet-stream을 malware로 분류하는 것을 방지합니다.
    """
    s3 = _get_s3_client()
    params: dict = {
        "Bucket": _storage_bucket(),
        "Key": key,
    }
    if filename:
        safe_name = filename.replace('"', "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_name}"'
    if content_type:
        params["ResponseContentType"] = content_type
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def copy_object_r2_storage(*, source_key: str, dest_key: str) -> None:
    """
    R2 내부 복사 (Copy & Delete 이동 시 사용, 대역폭 비용 없음).
    같은 버킷 내 copy_object.
    """
    s3 = _get_s3_client()
    bucket = _storage_bucket()
    s3.copy_object(
        CopySource={"Bucket": bucket, "Key": source_key},
        Bucket=bucket,
        Key=dest_key,
    )


def delete_object_r2_storage(
    *,
    key: str,
    timeout_seconds: int | None = None,
) -> None:
    """R2 Storage 버킷에서 객체 삭제. 이동 성공 확인 후에만 호출."""
    s3 = _get_s3_client(timeout_seconds=timeout_seconds)
    s3.delete_object(Bucket=_storage_bucket(), Key=key)


def get_object_bytes_r2_storage(
    *,
    key: str,
    max_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> bytes | None:
    """R2 Storage 버킷에서 객체 바이트 직접 가져오기. 없으면 None.

    OCR 영구 캐시 등 짧은 JSON 결과 fetch 용도. 큰 파일은 presign + HTTP 사용.
    """
    from botocore.exceptions import ClientError
    s3 = _get_s3_client(timeout_seconds=timeout_seconds)
    try:
        resp = s3.get_object(Bucket=_storage_bucket(), Key=key)
        body = resp["Body"]
        try:
            if max_bytes is not None and int(resp.get("ContentLength") or 0) > max_bytes:
                return None
            data = body.read(max_bytes + 1 if max_bytes is not None else None)
            if max_bytes is not None and len(data) > max_bytes:
                return None
            return data
        finally:
            body.close()
    except ClientError as err:
        code = (err.response.get("Error") or {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def head_object_r2_storage(*, key: str) -> tuple[bool, int]:
    """객체 존재 여부 및 크기(bytes)."""
    from botocore.exceptions import ClientError
    s3 = _get_s3_client()
    try:
        resp = s3.head_object(Bucket=_storage_bucket(), Key=key)
        return True, int(resp.get("ContentLength") or 0)
    except ClientError as err:
        code = (err.response.get("Error") or {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False, 0
        raise


# ---------------------------------------------------------------------
# Video 버킷 (R2_VIDEO_BUCKET) — raw/HLS 삭제
# ---------------------------------------------------------------------

def _video_bucket():
    return getattr(settings, "R2_VIDEO_BUCKET", "academy-video")


def sum_size_under_prefix_r2_video(*, prefix: str) -> tuple[int, int]:
    """
    Video 버킷에서 prefix 아래 모든 객체의 (총 바이트, 객체 수) 반환.
    페이지네이션 자동 처리. 대용량 prefix는 시간이 오래 걸릴 수 있음 — 호출측에서 캐시 권장.
    """
    s3 = _get_s3_client()
    bucket = _video_bucket()
    total_bytes = 0
    total_objects = 0
    continuation_token = None
    while True:
        list_kw = {"Bucket": bucket, "MaxKeys": 1000, "Prefix": prefix}
        if continuation_token:
            list_kw["ContinuationToken"] = continuation_token
        resp = s3.list_objects_v2(**list_kw)
        for obj in resp.get("Contents") or []:
            total_bytes += int(obj.get("Size") or 0)
            total_objects += 1
        if not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")
    return total_bytes, total_objects


def delete_object_r2_video(*, key: str) -> None:
    """R2 Video 버킷에서 객체 1건 삭제 (raw 등)."""
    s3 = _get_s3_client()
    s3.delete_object(Bucket=_video_bucket(), Key=key)


def delete_prefix_r2_video(
    *,
    prefix: str,
    on_batch_deleted: Callable[[int], None] | None = None,
) -> int:
    """
    R2 Video 버킷에서 prefix 아래 모든 객체 일괄 삭제 (HLS 등).
    delete_objects 최대 1000건씩 배치 처리.
    on_batch_deleted(total_deleted): 배치 삭제 직후 호출 (visibility 연장 등, 장시간 삭제 대비).
    Returns:
        삭제한 객체 수.
    """
    s3 = _get_s3_client()
    bucket = _video_bucket()
    total_deleted = 0
    continuation_token = None
    list_page = 1000
    delete_batch = 1000

    while True:
        list_kw = {"Bucket": bucket, "MaxKeys": list_page, "Prefix": prefix}
        if continuation_token:
            list_kw["ContinuationToken"] = continuation_token
        resp = s3.list_objects_v2(**list_kw)
        contents = resp.get("Contents") or []
        if not contents:
            if not resp.get("IsTruncated"):
                break
            continuation_token = resp.get("NextContinuationToken")
            continue
        keys = [{"Key": obj["Key"]} for obj in contents]
        for i in range(0, len(keys), delete_batch):
            batch = keys[i : i + delete_batch]
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            total_deleted += len(batch)
            if callable(on_batch_deleted):
                on_batch_deleted(total_deleted)
        if not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")

    return total_deleted


# ---------------------------------------------------------------------
# Admin 버킷 (R2_ADMIN_BUCKET) — 테넌트 로고 등
# ---------------------------------------------------------------------

def _admin_bucket():
    return getattr(settings, "R2_ADMIN_BUCKET", "academy-admin")


def upload_fileobj_to_r2_admin(
    *,
    fileobj,
    key: str,
    content_type: str | None = None,
) -> None:
    """Django UploadedFile -> R2 Admin 버킷 업로드 (테넌트 로고 등)."""
    s3 = _get_s3_client()
    s3.upload_fileobj(
        Fileobj=fileobj,
        Bucket=_admin_bucket(),
        Key=key,
        ExtraArgs={
            "ContentType": content_type or "application/octet-stream"
        },
    )


def get_admin_object_bytes(
    *,
    key: str,
    max_bytes: int | None = None,
    timeout_seconds: int | None = None,
) -> tuple[bytes, str] | None:
    """R2 Admin 버킷 객체를 직접 읽고 ``(bytes, content_type)``을 반환한다.

    테넌트 로고처럼 작고 이미 인증된 내부 객체를 presigned HTTP로 다시
    가져오지 않아도 되는 서버 렌더링 경로에서 사용한다. 객체가 없거나
    ``max_bytes``를 넘으면 ``None``을 반환한다.
    """
    from botocore.exceptions import ClientError

    s3 = _get_s3_client(timeout_seconds=timeout_seconds)
    try:
        resp = s3.get_object(Bucket=_admin_bucket(), Key=key)
        body = resp["Body"]
        try:
            if max_bytes is not None and int(resp.get("ContentLength") or 0) > max_bytes:
                return None
            data = body.read(max_bytes + 1 if max_bytes is not None else None)
            if max_bytes is not None and len(data) > max_bytes:
                return None
            content_type = str(resp.get("ContentType") or "application/octet-stream")
            return data, content_type
        finally:
            body.close()
    except ClientError as err:
        code = (err.response.get("Error") or {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def get_admin_object_public_url(*, key: str) -> str | None:
    """
    Admin 버킷 객체의 공개 URL.
    R2_ADMIN_PUBLIC_BASE_URL이 설정된 경우에만 반환 (없으면 None → presigned 사용).
    """
    base = getattr(settings, "R2_ADMIN_PUBLIC_BASE_URL", "") or ""
    base = (base or "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/{key}" if key else base


def resolve_admin_logo_url(*, logo_key: str | None = None, logo_url: str | None = None) -> str | None:
    """
    Admin 버킷 로고용 조회 URL. 공개 도메인이 비디오 버킷과 겹쳐 404 나는 경우를 피하기 위해
    항상 presigned URL을 반환. logo_key 우선, 없으면 logo_url에서 tenant-logos/ 경로 추출 후 presign.
    """
    if logo_key:
        return generate_presigned_get_url_admin(key=logo_key, expires_in=86400 * 7)
    if not logo_url:
        return None
    base = (getattr(settings, "R2_ADMIN_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not base or not logo_url.startswith(base + "/"):
        return logo_url
    key = logo_url[len(base) + 1 :].split("?")[0]
    if not key.startswith("tenant-logos/"):
        return logo_url
    return generate_presigned_get_url_admin(key=key, expires_in=86400 * 7)


def generate_presigned_get_url_admin(
    *,
    key: str,
    expires_in: int = 3600,
) -> str:
    """R2 Admin 버킷 presigned GET URL (공개 URL 미설정 시 로고 등 조회용)."""
    s3 = _get_s3_client()
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": _admin_bucket(),
            "Key": key,
        },
        ExpiresIn=expires_in,
    )
