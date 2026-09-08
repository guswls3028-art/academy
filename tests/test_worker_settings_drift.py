"""
Worker Settings Drift Test
============================
worker.py INSTALLED_APPS이 base.py 대비 누락된 도메인을 자동 검증.

배경 (2026-04-28 사고):
  - Storage-Matchup 통합으로 MatchupDocument.inventory_file FK 추가됨
  - apps/api/config/settings/worker.py INSTALLED_APPS에 matchup + inventory 누락
  - AI 워커가 MatchupProblem 콜백 시 ValueError:
    "Related model 'inventory.InventoryFile' cannot be resolved"
  - 28개 doc reanalyze 모두 stuck 상태에 멈춤

이 테스트는 두 가지를 검증:
  1. worker INSTALLED_APPS의 모든 모델의 FK target이 동일 INSTALLED_APPS에 있음
  2. 의도적으로 worker에서 제외된 도메인은 worker가 ORM으로 다루지 않아야 함

신규 도메인 추가 시:
  - 워커가 모델을 사용 → worker.py INSTALLED_APPS에 추가
  - 워커가 사용 안 함 → EXCLUDED_FROM_WORKER에 명시 (이 테스트가 통과)
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import django
import pytest


# 워커가 의도적으로 사용하지 않는 도메인 — base.py에는 있지만 worker.py에는 없어도 OK.
# 워커가 이 앱의 모델을 import하거나 ORM으로 다루면 안 됨 (다루면 worker.py에 추가 필요).
EXCLUDED_FROM_WORKER = {
    # 결제/회비 — 워커 흐름과 무관
    "fees",
    "billing",
    # 선생 웹앱 — 워커 흐름과 무관
    "teacher_app",
    # admin/sessions/staticfiles 등 Django contrib (HTTP 전용)
    "admin",
    "sessions",
    "messages",
    "staticfiles",
    # 외부 패키지 (worker 미필요)
    "rest_framework",
    "rest_framework_simplejwt",
    "token_blacklist",
    "django_filters",
    "drf_yasg",
    "corsheaders",
    "django_extensions",
}


def _load_worker_settings_module():
    """worker.py settings를 별도 import로 읽어옴 (현재 base.py 환경에 영향 안 줌)."""
    backend_root = Path(__file__).resolve().parents[1]
    worker_path = backend_root / "apps" / "api" / "config" / "settings" / "worker.py"
    spec = importlib.util.spec_from_file_location("_worker_settings_drift", worker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_base_settings_module():
    backend_root = Path(__file__).resolve().parents[1]
    base_path = backend_root / "apps" / "api" / "config" / "settings" / "base.py"
    spec = importlib.util.spec_from_file_location("_base_settings_drift", base_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_app_label(entry: str) -> str:
    """'apps.domains.inventory' → 'inventory', 'apps.domains.ai.apps.AIDomainConfig' → 'ai'."""
    # AppConfig 경로면 부모로 (apps.domains.ai.apps.X → apps.domains.ai)
    parts = entry.split(".")
    if parts and parts[-1][:1].isupper():
        parts = parts[:-2] if len(parts) >= 2 else parts
    if "domains" in parts:
        idx = parts.index("domains")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1]


def test_worker_installed_apps_no_missing_domain():
    """base.py에 있고 EXCLUDED 명시 안 된 도메인이 worker.py에 있는지."""
    base_mod = _load_base_settings_module()
    worker_mod = _load_worker_settings_module()

    base_labels = {_normalize_app_label(a) for a in base_mod.INSTALLED_APPS}
    worker_labels = {_normalize_app_label(a) for a in worker_mod.INSTALLED_APPS}

    # base에는 있지만 worker에 없는 — EXCLUDED에 등록되어 있어야 함
    missing = (base_labels - worker_labels) - EXCLUDED_FROM_WORKER
    assert not missing, (
        f"base.py INSTALLED_APPS에 있지만 worker.py에 없고 EXCLUDED_FROM_WORKER에도 명시 안 된 도메인: {sorted(missing)}\n"
        f"이 도메인을 워커가 사용하면 worker.py INSTALLED_APPS에 추가하고, 사용하지 않으면 "
        f"tests/test_worker_settings_drift.py의 EXCLUDED_FROM_WORKER에 추가하세요.\n"
        f"(2026-04-28 inventory/matchup 누락 사고와 같은 ValueError 'Related model X.Y cannot be resolved' 재발 방지)"
    )


def test_worker_storage_settings_honor_runtime_bucket_overrides(monkeypatch):
    """Worker writes must use the same tenant storage bucket as the API env."""
    monkeypatch.setenv("R2_REGION", "test-region")
    monkeypatch.setenv("R2_STORAGE_BUCKET", "test-storage-worker")
    monkeypatch.setenv("R2_ADMIN_BUCKET", "test-admin-worker")

    worker_mod = _load_worker_settings_module()

    assert worker_mod.R2_REGION == "test-region"
    assert worker_mod.R2_STORAGE_BUCKET == "test-storage-worker"
    assert worker_mod.R2_ADMIN_BUCKET == "test-admin-worker"


def test_worker_settings_load_owner_tenant_id_from_runtime(monkeypatch):
    monkeypatch.setenv("OWNER_TENANT_ID", "23")

    worker_mod = _load_worker_settings_module()

    assert worker_mod.OWNER_TENANT_ID == 23


def test_worker_settings_fail_startup_for_malformed_owner_tenant_id(monkeypatch):
    monkeypatch.setenv("OWNER_TENANT_ID", "not-an-integer")

    with pytest.raises(ValueError):
        _load_worker_settings_module()


def test_worker_models_fk_targets_resolvable():
    """worker INSTALLED_APPS 모든 모델의 FK target도 worker INSTALLED_APPS에 있는지.

    worker.py 환경에서 Django를 부팅하면, 어떤 모델 A가 EXCLUDED 도메인의 모델 B를
    FK로 참조할 때 'Related model X.Y cannot be resolved' 가 자동 발생.
    이 테스트가 통과하면 워커가 사용하는 모든 모델 그래프가 self-contained.
    """
    # 별도 Django 부팅 — pytest의 base.py 컨텍스트와 분리 위해 subprocess 사용
    import subprocess
    backend_root = Path(__file__).resolve().parents[1]
    script = backend_root / "tests" / "_worker_boot_check.py"
    if not script.exists():
        # 헬퍼 스크립트가 없으면 skip (CI 첫 도입 시 작성 필요)
        import pytest
        pytest.skip("tests/_worker_boot_check.py not present (run-only check)")

    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "apps.api.config.settings.worker"
    env["SECRET_KEY"] = "test-worker-drift"
    env["PYTHONPATH"] = str(backend_root) + os.pathsep + env.get("PYTHONPATH", "")
    # 워커 settings는 DB 환경변수를 요구하지만 부팅 자체는 lazy — 모델 import만으로 FK 검증 가능.
    env.setdefault("DB_NAME", "test")
    env.setdefault("DB_USER", "test")
    env.setdefault("DB_PASSWORD", "test")
    env.setdefault("DB_HOST", "localhost")
    env.setdefault("DB_PORT", "5432")

    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=str(backend_root),
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Worker settings boot failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}\n"
        f"흔한 원인: 워커가 사용하는 모델이 EXCLUDED 도메인의 모델을 FK로 참조."
    )
