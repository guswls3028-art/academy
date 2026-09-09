# PATH: apps/domains/inventory/r2_path.py
# R2 저장 경로 SSOT
# 학생: tenants/{tenant_id}/students/{student_ps}/inventory/{folder_path}/{file_name}
# 선생님: tenants/{tenant_id}/admin/inventory/{folder_path}/{file_name}

import re
import secrets
from datetime import datetime


def safe_filename(original: str) -> str:
    """충돌 방지: 원본명_YYMMDD_해시.확장자"""
    base, ext = "", ""
    if "." in original:
        idx = original.rfind(".")
        base, ext = original[:idx], original[idx:]
    else:
        base = original
    stamp = datetime.now().strftime("%y%m%d")
    hash_s = secrets.token_hex(16)
    return f"{base}_{stamp}_{hash_s}{ext}"


def folder_path_string(folder_names: list[str]) -> str:
    """폴더 이름 리스트 → R2 경로 세그먼트."""
    safe = [re.sub(r"[^\w\s\-.]", "_", n).strip() or "folder" for n in folder_names]
    return "/".join(safe)


def build_r2_key(
    *,
    tenant_id: int,
    scope: str,
    student_ps: str = "",
    folder_path: str = "",
    file_name: str,
) -> str:
    if scope == "student":
        prefix = f"tenants/{tenant_id}/students/{student_ps}/inventory"
    else:
        prefix = f"tenants/{tenant_id}/admin/inventory"
    if folder_path:
        return f"{prefix}/{folder_path}/{file_name}"
    return f"{prefix}/{file_name}"
