# Production-shaped wrong-note canary for the isolated persistent development runtime.
# It creates two disposable qa-* tenants, uses the real HTTP API, tools SQS worker,
# and development R2 bucket, then proves exact message deletion and zero residue.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha-[0-9a-f]{40}-run-[0-9]+-[0-9]+$')]
    [string]$ExpectedReleaseId,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ExpectedApiDigest,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ExpectedToolsDigest,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+$')]
    [string]$RunId,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+$')]
    [string]$RunAttempt,
    [ValidatePattern('^$|^i-[0-9a-f]+$')]
    [string]$InstanceId = "",
    [ValidateRange(180, 900)]
    [int]$TimeoutSec = 600,
    [switch]$Ci = $false,
    [string]$AwsProfile = "default"
)

$ErrorActionPreference = "Stop"
$ScriptRoot = $PSScriptRoot
if ($Ci) {
    Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue
} elseif ($AwsProfile -and $AwsProfile.Trim()) {
    $env:AWS_PROFILE = $AwsProfile.Trim()
}
if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = "ap-northeast-2" }

$script:PlanMode = $false
. (Join-Path $ScriptRoot "core\env.ps1")
. (Join-Path $ScriptRoot "core\ssot.ps1")
. (Join-Path $ScriptRoot "core\aws.ps1")
Assert-AwsMutationIdentity | Out-Null
Load-SSOT -Env prod | Out-Null

if (-not $script:ApiDevelopmentEnabled) {
    throw "Persistent API development environment is disabled in params.yaml."
}
if ($script:ApiDevelopmentAccessMode -ne "ssm-only") {
    throw "API development access must remain ssm-only."
}

$describeArgs = @("ec2", "describe-instances")
if ($InstanceId) {
    $describeArgs += @("--instance-ids", $InstanceId)
} else {
    $describeArgs += @(
        "--filters",
        "Name=tag:Name,Values=$($script:ApiDevelopmentInstanceName)",
        "Name=tag:ManagedBy,Values=$($script:ApiDevelopmentManagedByTag)",
        "Name=tag:Lifecycle,Values=active",
        "Name=instance-state-name,Values=running"
    )
}
$describeArgs += @("--region", $script:Region, "--output", "json")
$instances = @((Invoke-AwsJson $describeArgs).Reservations.Instances | Where-Object { $_.InstanceId })
if ($instances.Count -ne 1 -or [string]$instances[0].State.Name -ne "running") {
    throw "Expected exactly one running API development instance; actual=$($instances.Count)."
}
$instance = $instances[0]
$instanceId = [string]$instance.InstanceId
$tags = @{}
foreach ($tag in @($instance.Tags)) {
    $tags[[string]$tag.Key] = [string]$tag.Value
}
if (
    $tags["Name"] -ne $script:ApiDevelopmentInstanceName -or
    $tags["ManagedBy"] -ne $script:ApiDevelopmentManagedByTag -or
    $tags["Lifecycle"] -ne "active"
) {
    throw "Target instance is outside the active managed API development boundary."
}

$capability = [Convert]::ToHexString(
    [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLowerInvariant()
$suffix = $capability.Substring(0, 12)
$tenantCode = "qa-ymath-realuse-wn-$RunId-$RunAttempt-$suffix"
$boundaryTenantCode = "$tenantCode-boundary"

$python = @'
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

import boto3
import django

django.setup()

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from apps.core.management.commands.setup_ymath_realuse_scenario import (
    Command as ScenarioCommand,
    assert_isolated_runtime,
)
from apps.core.models import OpsAuditLog, Tenant
from apps.domains.ai.callbacks import dispatch_ai_result_to_domain
from apps.domains.ai.models import AIJobModel, AIResultModel
from apps.domains.exams.models import AnswerKey, Exam, ExamQuestion, Sheet
from apps.domains.results.models import Result, ResultItem, WrongNotePDF
from apps.domains.results.services.wrong_note_pdf_service import (
    delete_wrong_note_pdf_object,
)
from apps.domains.results.services.wrong_note_service import (
    WrongNoteQuery,
    build_wrong_note_source_fingerprint,
    list_wrong_notes_for_enrollment,
)
from apps.infrastructure.storage.r2 import _get_s3_client
from apps.support.results.wrong_note_pdf_dependencies import (
    create_wrong_note_pdf_ai_job,
    publish_wrong_note_pdf_ai_job,
)

ACTION = os.environ["QA_ACTION"]
CAPABILITY = os.environ["QA_CAPABILITY"]
TENANT_CODE = os.environ["QA_TENANT"]
BOUNDARY_CODE = os.environ["QA_BOUNDARY_TENANT"]
RELEASE_ID = os.environ["QA_RELEASE"]
API_DIGEST = os.environ["QA_API_DIGEST"]
API_IMAGE = os.environ["QA_API_IMAGE"]
OWNER_ACTION = "development.qa.wrong_note_canary.owner"
EVIDENCE_ACTION = "development.qa.wrong_note_canary.evidence"
PASSWORD_PARAMETER = "/academy/api/development/ymath-realuse-password"


def owner_payload(code: str, tenant_id: int) -> dict:
    owner_sha = hashlib.sha256(
        f"wrong-note-canary:{code}:{tenant_id}:{CAPABILITY}".encode()
    ).hexdigest()
    return {
        "schema": "wrong-note-development-canary/v1",
        "tenant_code": code,
        "tenant_id": tenant_id,
        "owner_sha256": owner_sha,
    }


def assert_runtime() -> None:
    assert ACTION in {"Run", "Inspect", "Cleanup"}
    assert re.fullmatch(r"[a-f0-9]{64}", CAPABILITY)
    assert re.fullmatch(r"qa-ymath-realuse-wn-[0-9]+-[0-9]+-[a-f0-9]{12}", TENANT_CODE)
    assert BOUNDARY_CODE == f"{TENANT_CODE}-boundary"
    assert re.fullmatch(r"sha-[a-f0-9]{40}-run-[0-9]+-[0-9]+", RELEASE_ID)
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", API_DIGEST)
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "apps.api.config.settings.development"
    assert os.environ["ACADEMY_RUNTIME_ENV"] == "development"
    assert os.environ["ACADEMY_DEVELOPMENT_RELEASE_ID"] == RELEASE_ID
    assert API_IMAGE == f"809466760795.dkr.ecr.ap-northeast-2.amazonaws.com/academy-api@{API_DIGEST}"
    assert settings.DATABASES["default"]["NAME"] == "academy_api_development"
    assert settings.DATABASES["default"]["USER"] == "academy_api_development_app"
    assert settings.TOOLS_SQS_QUEUE_NAME == "academy-v1-development-tools-queue"
    assert all(
        getattr(settings, key) == "academy-development-artifacts"
        for key in (
            "R2_AI_BUCKET",
            "R2_STORAGE_BUCKET",
            "R2_ADMIN_BUCKET",
            "R2_VIDEO_BUCKET",
            "R2_EXCEL_BUCKET",
        )
    )
    assert_isolated_runtime()


def exact_tenant(code: str):
    matches = list(Tenant.objects.filter(code__iexact=code).order_by("id")[:2])
    if len(matches) > 1 or (matches and matches[0].code != code):
        raise RuntimeError("Ambiguous disposable tenant identity")
    return matches[0] if matches else None


def assert_owner(tenant) -> None:
    records = list(
        OpsAuditLog.objects.filter(
            action=OWNER_ACTION,
            target_tenant=tenant,
            result="success",
        ).values_list("payload", flat=True)[:2]
    )
    if records != [owner_payload(tenant.code, tenant.id)]:
        raise PermissionError("Disposable tenant ownership capability mismatch")


def create_scenario(code: str, password: str) -> object:
    if exact_tenant(code) is not None:
        raise RuntimeError("Disposable tenant already exists")
    os.environ["YMATH_REALUSE_SCENARIO_PASSWORD"] = password
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            call_command(
                "setup_ymath_realuse_scenario",
                tenant_code=code,
                teacher_username="wrong-note-qa-teacher",
                student_count=1,
                session_count=1,
            )
    finally:
        os.environ.pop("YMATH_REALUSE_SCENARIO_PASSWORD", None)
    tenant = exact_tenant(code)
    if tenant is None:
        raise RuntimeError("Disposable tenant setup did not persist")
    OpsAuditLog.objects.create(
        action=OWNER_ACTION,
        actor_username="wrong-note-development-canary",
        target_tenant=tenant,
        payload=owner_payload(code, tenant.id),
        result="success",
    )
    return tenant


def http_json(method: str, path: str, *, tenant_code: str, token: str = "", body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"X-Tenant-Code": tenant_code, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"detail": "non-json error"}
        return error.code, payload


def login(code: str, password: str) -> str:
    status, payload = http_json(
        "POST",
        "/api/v1/token/",
        tenant_code=code,
        body={
            "username": "wrong-note-qa-teacher",
            "password": password,
            "tenant_code": code,
        },
    )
    if status != 200 or not payload.get("access"):
        raise RuntimeError(f"Disposable teacher login failed: status={status}")
    return str(payload["access"])


def stale_state(job: WrongNotePDF) -> list[str]:
    return [
        str(job.status),
        str(job.file_path or ""),
        str(job.error_message or ""),
        job.updated_at.isoformat(),
    ]


def wait_for_status(job_id: int, token: str, timeout_seconds: int = 240) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, payload = http_json(
            "GET",
            f"/api/v1/results/wrong-notes/pdf/{job_id}/",
            tenant_code=TENANT_CODE,
            token=token,
        )
        if status != 200:
            raise RuntimeError(f"Wrong-note status failed: status={status}")
        if payload.get("status") == WrongNotePDF.Status.DONE:
            return payload
        if payload.get("status") == WrongNotePDF.Status.FAILED:
            raise RuntimeError("Wrong-note generation entered FAILED")
        time.sleep(2)
    raise TimeoutError("Wrong-note generation did not complete before deadline")


def download_pdf(url: str) -> tuple[int, str]:
    if not url.startswith("https://"):
        raise RuntimeError("Wrong-note status did not return an HTTPS download URL")
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = response.read()
    if not payload.startswith(b"%PDF") or len(payload) < 1_000:
        raise RuntimeError("Wrong-note download is not a non-empty PDF")
    return len(payload), hashlib.sha256(payload).hexdigest()


def run_canary() -> dict:
    if exact_tenant(TENANT_CODE) or exact_tenant(BOUNDARY_CODE):
        raise RuntimeError("Disposable canary tenant residue exists before setup")
    password = boto3.client("ssm", region_name="ap-northeast-2").get_parameter(
        Name=PASSWORD_PARAMETER,
        WithDecryption=True,
    )["Parameter"]["Value"]
    tenant = create_scenario(TENANT_CODE, password)
    boundary_tenant = create_scenario(BOUNDARY_CODE, password)

    Enrollment = apps.get_model("enrollment", "Enrollment")
    Session = apps.get_model("lectures", "Session")
    enrollment = Enrollment.objects.select_related("student", "lecture").get(
        tenant=tenant
    )
    session = Session.objects.get(lecture=enrollment.lecture, order=1)
    exam = Exam.objects.create(
        tenant=tenant,
        title="Wrong-note development canary",
        exam_type=Exam.ExamType.REGULAR,
        segmentation_status=Exam.SegmentationStatus.READY,
    )
    exam.sessions.add(session)
    sheet = Sheet.objects.create(exam=exam, total_questions=1, choice_count=1)
    question = ExamQuestion.objects.create(
        sheet=sheet,
        number=1,
        score=1,
        question_kind=ExamQuestion.QuestionKind.CHOICE,
    )
    AnswerKey.objects.create(exam=exam, answers={str(question.id): "2"})
    result = Result.objects.create(
        enrollment=enrollment,
        target_type="exam",
        target_id=exam.id,
        total_score=0,
        max_score=1,
        submitted_at=timezone.now(),
    )
    ResultItem.objects.create(
        result=result,
        question=question,
        answer="1",
        is_correct=False,
        include_in_wrong_note=True,
        score=0,
        max_score=1,
        source="manual",
    )
    total, items = list_wrong_notes_for_enrollment(
        enrollment_id=enrollment.id,
        q=WrongNoteQuery(
            lecture_id=enrollment.lecture_id,
            from_session_order=1,
            to_session_order=1,
            offset=0,
            limit=200,
        ),
    )
    if total != 1:
        raise RuntimeError(f"Expected one wrong-note source; actual={total}")
    source_fingerprint = build_wrong_note_source_fingerprint(total=total, items=items)

    old_job = WrongNotePDF.objects.create(
        enrollment=enrollment,
        lecture=enrollment.lecture,
        from_session_order=1,
        to_session_order=1,
        status=WrongNotePDF.Status.PENDING,
        source_fingerprint=source_fingerprint,
    )
    old_ai_job = create_wrong_note_pdf_ai_job(
        pdf_job_id=old_job.id,
        tenant_id=tenant.id,
        source_fingerprint=source_fingerprint,
    )
    AIJobModel.objects.filter(id=old_ai_job.id).update(
        status="DONE",
        completed_at=timezone.now(),
    )

    token = login(TENANT_CODE, password)
    boundary_token = login(BOUNDARY_CODE, password)
    create_body = {
        "enrollment_id": enrollment.id,
        "from_session_order": 1,
        "to_session_order": 1,
        "output_format": "pdf",
        "source_fingerprint": source_fingerprint,
    }
    duplicate_status, _ = http_json(
        "POST",
        "/api/v1/results/wrong-notes/pdf/",
        tenant_code=TENANT_CODE,
        token=token,
        body=create_body,
    )
    if duplicate_status != 409:
        raise RuntimeError(f"Active duplicate request was not rejected: status={duplicate_status}")

    WrongNotePDF.objects.filter(id=old_job.id).update(
        updated_at=timezone.now() - timedelta(minutes=6)
    )
    retry_status, retry_payload = http_json(
        "POST",
        "/api/v1/results/wrong-notes/pdf/",
        tenant_code=TENANT_CODE,
        token=token,
        body=create_body,
    )
    if retry_status != 202:
        raise RuntimeError(f"Stale retry was not accepted: status={retry_status}")
    new_job_id = int(retry_payload["job_id"])
    old_job.refresh_from_db()
    if old_job.status != WrongNotePDF.Status.FAILED:
        raise RuntimeError("Stale predecessor did not become FAILED")
    old_snapshot = stale_state(old_job)
    old_key = f"tenants/{tenant.id}/results/wrong-notes/{old_job.id}.pdf"
    callbacks = (
        (
            "DONE",
            {
                "outcome": WrongNotePDF.Status.DONE,
                "wrong_note_pdf_job_id": old_job.id,
                "file_path": old_key,
            },
            None,
        ),
        (
            "DONE",
            {
                "outcome": WrongNotePDF.Status.FAILED,
                "wrong_note_pdf_job_id": old_job.id,
                "error_message": "late worker failure",
                "file_path": "",
            },
            None,
        ),
        ("FAILED", {}, "late transport failure"),
    )
    for callback_status, payload, callback_error in callbacks:
        handled = dispatch_ai_result_to_domain(
            job_id=old_ai_job.job_id,
            status=callback_status,
            result_payload=payload,
            error=callback_error,
            source_domain=old_ai_job.source_domain,
            source_id=old_ai_job.source_id,
        )
        old_job.refresh_from_db()
        if not handled or stale_state(old_job) != old_snapshot:
            raise RuntimeError("Late predecessor callback changed terminal state")

    completed = wait_for_status(new_job_id, token)
    file_bytes, file_sha256 = download_pdf(str(completed.get("file_url") or ""))
    reload_status, reloaded = http_json(
        "GET",
        f"/api/v1/results/wrong-notes/pdf/{new_job_id}/",
        tenant_code=TENANT_CODE,
        token=token,
    )
    if (
        reload_status != 200
        or reloaded.get("status") != WrongNotePDF.Status.DONE
        or reloaded.get("file_path") != completed.get("file_path")
        or not reloaded.get("file_url")
    ):
        raise RuntimeError("Wrong-note DONE state did not survive reload")

    boundary_status, _ = http_json(
        "GET",
        f"/api/v1/results/wrong-notes/pdf/{new_job_id}/",
        tenant_code=BOUNDARY_CODE,
        token=boundary_token,
    )
    if boundary_status != 403:
        raise RuntimeError(f"Cross-tenant status lookup was not denied: status={boundary_status}")

    new_job = WrongNotePDF.objects.get(id=new_job_id)
    new_ai_job = AIJobModel.objects.get(
        source_domain="results_wrong_note_pdf",
        source_id=str(new_job.id),
    )
    if new_ai_job.status != "DONE" or not AIResultModel.objects.filter(job=new_ai_job).exists():
        raise RuntimeError("Actual tools worker did not persist the terminal AI result")
    new_snapshot = stale_state(new_job)
    new_ai_job.status = "PENDING"
    if not publish_wrong_note_pdf_ai_job(new_ai_job):
        raise RuntimeError("Terminal duplicate delivery could not be published")

    mismatch_job = AIJobModel.objects.create(
        job_id=f"wrong-note-canary-tenant-{CAPABILITY[:20]}",
        job_type="wrong_note_pdf_generation",
        status="PENDING",
        tenant_id=str(tenant.id),
        source_domain="results_wrong_note_pdf",
        source_id=str(old_job.id),
        payload={
            "wrong_note_pdf_job_id": old_job.id,
            "source_fingerprint": source_fingerprint,
        },
        tier="basic",
    )
    mismatch_job.tenant_id = str(boundary_tenant.id)
    if not publish_wrong_note_pdf_ai_job(mismatch_job):
        raise RuntimeError("Tenant-mismatch delivery could not be published")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        mismatch_job.refresh_from_db()
        if mismatch_job.status == "FAILED":
            break
        time.sleep(2)
    if mismatch_job.status != "FAILED" or mismatch_job.last_error != "tenant_mismatch_in_sqs_message":
        raise RuntimeError("Tools worker did not reject the mismatched tenant envelope")

    old_job.refresh_from_db()
    new_job.refresh_from_db()
    if stale_state(old_job) != old_snapshot or stale_state(new_job) != new_snapshot:
        raise RuntimeError("Boundary delivery changed a terminal wrong-note job")

    evidence = {
        "schema": "wrong-note-development-canary/v1",
        "tenant_code": TENANT_CODE,
        "boundary_tenant_code": BOUNDARY_CODE,
        "tenant_id": tenant.id,
        "boundary_tenant_id": boundary_tenant.id,
        "old_job_id": old_job.id,
        "new_job_id": new_job.id,
        "new_ai_job_id": new_ai_job.job_id,
        "mismatch_ai_job_id": mismatch_job.job_id,
        "old_snapshot": old_snapshot,
        "new_snapshot": new_snapshot,
        "file_path": new_job.file_path,
        "file_bytes": file_bytes,
        "file_sha256": file_sha256,
        "cross_tenant_status": boundary_status,
        "owner_sha256": owner_payload(TENANT_CODE, tenant.id)["owner_sha256"],
    }
    OpsAuditLog.objects.create(
        action=EVIDENCE_ACTION,
        actor_username="wrong-note-development-canary",
        target_tenant=tenant,
        payload=evidence,
        result="success",
    )
    return {
        "status": "WRONG_NOTE_DEVELOPMENT_FLOW_READY_FOR_LOG_SEAL",
        "release_id": RELEASE_ID,
        "api_digest": API_DIGEST,
        **evidence,
    }


def evidence_record(tenant) -> dict:
    records = list(
        OpsAuditLog.objects.filter(
            action=EVIDENCE_ACTION,
            target_tenant=tenant,
            result="success",
        ).values_list("payload", flat=True)[:2]
    )
    if len(records) != 1 or records[0].get("owner_sha256") != owner_payload(
        TENANT_CODE, tenant.id
    )["owner_sha256"]:
        raise PermissionError("Wrong-note canary evidence ownership mismatch")
    return records[0]


def inspect_canary() -> dict:
    tenant = exact_tenant(TENANT_CODE)
    boundary_tenant = exact_tenant(BOUNDARY_CODE)
    if tenant is None or boundary_tenant is None:
        raise RuntimeError("Disposable canary tenants are missing before inspection")
    assert_owner(tenant)
    assert_owner(boundary_tenant)
    evidence = evidence_record(tenant)
    old_job = WrongNotePDF.objects.get(id=evidence["old_job_id"])
    new_job = WrongNotePDF.objects.get(id=evidence["new_job_id"])
    mismatch_job = AIJobModel.objects.get(job_id=evidence["mismatch_ai_job_id"])
    new_ai_job = AIJobModel.objects.get(job_id=evidence["new_ai_job_id"])
    if stale_state(old_job) != evidence["old_snapshot"]:
        raise RuntimeError("Late or duplicate callback changed the stale predecessor")
    if stale_state(new_job) != evidence["new_snapshot"]:
        raise RuntimeError("Duplicate callback changed the completed replacement")
    if mismatch_job.status != "FAILED" or mismatch_job.last_error != "tenant_mismatch_in_sqs_message":
        raise RuntimeError("Tenant mismatch job did not stay terminal")
    if new_ai_job.status != "DONE" or not AIResultModel.objects.filter(job=new_ai_job).exists():
        raise RuntimeError("Positive tools job lost its terminal result")
    if WrongNotePDF.objects.filter(
        enrollment__tenant=tenant,
        status__in=(WrongNotePDF.Status.PENDING, WrongNotePDF.Status.RUNNING),
    ).exists():
        raise RuntimeError("Wrong-note canary left an active document job")
    client = _get_s3_client(timeout_seconds=30)
    obj = client.get_object(Bucket=settings.R2_STORAGE_BUCKET, Key=evidence["file_path"])
    body = obj["Body"].read()
    if hashlib.sha256(body).hexdigest() != evidence["file_sha256"]:
        raise RuntimeError("Persisted wrong-note PDF fingerprint changed")
    return {
        "status": "WRONG_NOTE_DEVELOPMENT_FLOW_PASS",
        "release_id": RELEASE_ID,
        "api_digest": API_DIGEST,
        "old_job_status": old_job.status,
        "new_job_status": new_job.status,
        "file_bytes": evidence["file_bytes"],
        "file_sha256": evidence["file_sha256"],
        "cross_tenant_status": evidence["cross_tenant_status"],
        "queue_messages_deleted": 2,
        "active_jobs": 0,
    }


def delete_prefix(tenant_id: int) -> None:
    prefix = f"tenants/{tenant_id}/"
    client = _get_s3_client(timeout_seconds=30)
    continuation = None
    while True:
        request = {"Bucket": settings.R2_STORAGE_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if continuation:
            request["ContinuationToken"] = continuation
        page = client.list_objects_v2(**request)
        objects = [{"Key": item["Key"]} for item in page.get("Contents") or []]
        if objects:
            client.delete_objects(
                Bucket=settings.R2_STORAGE_BUCKET,
                Delete={"Objects": objects, "Quiet": True},
            )
        if not page.get("IsTruncated"):
            return
        continuation = page["NextContinuationToken"]


def cleanup_canary() -> dict:
    tenants = [exact_tenant(TENANT_CODE), exact_tenant(BOUNDARY_CODE)]
    tenants = [tenant for tenant in tenants if tenant is not None]
    if not tenants:
        return {
            "status": "WRONG_NOTE_DEVELOPMENT_CLEANUP_PASS",
            "remaining": {"tenants": 0, "users": 0, "ai_jobs": 0, "r2_objects": 0},
        }
    for tenant in tenants:
        assert_owner(tenant)
    tenant_ids = [tenant.id for tenant in tenants]
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        if not WrongNotePDF.objects.filter(
            enrollment__tenant_id__in=tenant_ids,
            status__in=(WrongNotePDF.Status.PENDING, WrongNotePDF.Status.RUNNING),
        ).exists():
            break
        time.sleep(2)
    if WrongNotePDF.objects.filter(
        enrollment__tenant_id__in=tenant_ids,
        status__in=(WrongNotePDF.Status.PENDING, WrongNotePDF.Status.RUNNING),
    ).exists():
        raise RuntimeError("Refusing cleanup while a wrong-note worker job is active")

    for key in WrongNotePDF.objects.filter(
        enrollment__tenant_id__in=tenant_ids
    ).exclude(file_path="").values_list("file_path", flat=True):
        if not delete_wrong_note_pdf_object(str(key)):
            raise RuntimeError("Tracked wrong-note object cleanup failed")
    for tenant_id in tenant_ids:
        delete_prefix(tenant_id)

    AIJobModel.objects.filter(tenant_id__in=[str(value) for value in tenant_ids]).delete()
    owned_audits = OpsAuditLog.objects.filter(
        target_tenant_id__in=tenant_ids,
        action__in=(OWNER_ACTION, EVIDENCE_ACTION, "development.qa.scenario"),
    )
    audit_ids = list(owned_audits.values_list("id", flat=True))
    for tenant in reversed(tenants):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            call_command("setup_ymath_realuse_scenario", tenant_code=tenant.code, destroy=True)
    OpsAuditLog.objects.filter(id__in=audit_ids).delete()

    scenario = ScenarioCommand()
    remaining = {
        "tenants": sum(scenario._remaining_for_code(code)["tenants"] for code in (TENANT_CODE, BOUNDARY_CODE)),
        "users": sum(scenario._remaining_for_code(code)["users"] for code in (TENANT_CODE, BOUNDARY_CODE)),
        "ai_jobs": AIJobModel.objects.filter(tenant_id__in=[str(value) for value in tenant_ids]).count(),
        "audit_rows": OpsAuditLog.objects.filter(id__in=audit_ids).count(),
        "r2_objects": sum(
            scenario._non_database_residue(tenant_id=value, tenant_code=TENANT_CODE)["r2_objects"]
            for value in tenant_ids
        ),
    }
    if any(remaining.values()):
        raise RuntimeError(f"Wrong-note canary cleanup residue: {remaining}")
    return {"status": "WRONG_NOTE_DEVELOPMENT_CLEANUP_PASS", "remaining": remaining}


assert_runtime()
with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
    if ACTION == "Run":
        result = run_canary()
    elif ACTION == "Inspect":
        result = inspect_canary()
    else:
        result = cleanup_canary()
print(json.dumps(result, sort_keys=True))
'@

$pythonB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($python))
$remote = @"
set -euo pipefail
format=`$(printf '"'"'\173\173.Config.Image\175\175'"'"')
api_image=`$(timeout --kill-after=5s 15s docker inspect -f "`$format" academy-api)
tools_image=`$(timeout --kill-after=5s 15s docker inspect -f "`$format" academy-tools-development)
test "`$api_image" = "809466760795.dkr.ecr.ap-northeast-2.amazonaws.com/academy-api@$ExpectedApiDigest"
test "`$tools_image" = "809466760795.dkr.ecr.ap-northeast-2.amazonaws.com/academy-tools-worker@$ExpectedToolsDigest"
started=`$(date -u +%Y-%m-%dT%H:%M:%SZ)
cleanup_needed=1
run_action() {
  action="`$1"
  printf '%s' '$pythonB64' | base64 -d | docker exec -i \
    -e QA_ACTION="`$action" \
    -e QA_CAPABILITY='$capability' \
    -e QA_TENANT='$tenantCode' \
    -e QA_BOUNDARY_TENANT='$boundaryTenantCode' \
    -e QA_RELEASE='$ExpectedReleaseId' \
    -e QA_API_DIGEST='$ExpectedApiDigest' \
    -e QA_API_IMAGE="`$api_image" \
    academy-api python -
}
cleanup() {
  if [ "`$cleanup_needed" = 1 ]; then
    set +e
    cleanup_output=`$(run_action Cleanup)
    cleanup_status=`$?
    printf '%s\n' "`$cleanup_output"
    if [ "`$cleanup_status" -ne 0 ]; then
      echo WRONG_NOTE_DEVELOPMENT_CLEANUP_FAILED >&2
    fi
  fi
}
trap cleanup EXIT

run_output=`$(run_action Run)
printf '%s\n' "`$run_output"
run_json=`$(printf '%s\n' "`$run_output" | tail -n 1)
printf '%s' "`$run_json" | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "WRONG_NOTE_DEVELOPMENT_FLOW_READY_FOR_LOG_SEAL"'
duplicate_job=`$(printf '%s' "`$run_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["new_ai_job_id"])')
mismatch_job=`$(printf '%s' "`$run_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mismatch_ai_job_id"])')

wait_for_log() {
  marker="`$1"
  deadline=`$((SECONDS + 120))
  while [ "`$SECONDS" -lt "`$deadline" ]; do
    if docker logs --since "`$started" academy-tools-development 2>&1 | grep -F -- "`$marker" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "Missing tools-worker marker: `$marker" >&2
  return 1
}
wait_for_log "AI_JOB_IDEMPOTENT_SKIP | job_id=`$duplicate_job callback_ok=true message_deleted=true"
wait_for_log "AI_JOB_TENANT_MISMATCH_REJECTED | job_id=`$mismatch_job"
wait_for_log "AI_JOB_IDEMPOTENT_SKIP | job_id=`$mismatch_job callback_ok=true message_deleted=true"

inspect_output=`$(run_action Inspect)
printf '%s\n' "`$inspect_output"
printf '%s\n' "`$inspect_output" | tail -n 1 | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "WRONG_NOTE_DEVELOPMENT_FLOW_PASS"'
cleanup_output=`$(run_action Cleanup)
printf '%s\n' "`$cleanup_output"
printf '%s\n' "`$cleanup_output" | tail -n 1 | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "WRONG_NOTE_DEVELOPMENT_CLEANUP_PASS"; assert not any(data["remaining"].values())'
cleanup_needed=0
trap - EXIT
echo WRONG_NOTE_DEVELOPMENT_CANARY_PASS
"@
$remote = $remote.Replace("`r", "")
$paramsRef = Convert-JsonArgToFileRef (
    @{
        commands = @($remote)
        executionTimeout = @([string]$TimeoutSec)
    } | ConvertTo-Json -Compress
)
$paramsFile = $paramsRef -replace '^file://', ''
try {
    $sent = Invoke-AwsJson @(
        "ssm", "send-command",
        "--instance-ids", $instanceId,
        "--document-name", "AWS-RunShellScript",
        "--parameters", $paramsRef,
        "--timeout-seconds", [string]$TimeoutSec,
        "--region", $script:Region,
        "--comment", "Disposable wrong-note API, tools queue, R2, retry, duplicate, and tenant canary",
        "--output", "json"
    )
} finally {
    Remove-TempFiles @($paramsFile)
}
$commandId = [string]$sent.Command.CommandId
if (-not $commandId) {
    throw "Wrong-note development canary returned no SSM command id."
}

$invocation = $null
for ($elapsed = 0; $elapsed -lt $TimeoutSec; $elapsed += 5) {
    Start-Sleep -Seconds 5
    $invocation = Invoke-AwsJson @(
        "ssm", "get-command-invocation",
        "--command-id", $commandId,
        "--instance-id", $instanceId,
        "--region", $script:Region,
        "--output", "json"
    )
    if ($invocation.Status -in @("Success", "Failed", "Cancelled", "TimedOut")) {
        break
    }
}
if (-not $invocation -or [string]$invocation.Status -ne "Success") {
    $status = if ($invocation) { [string]$invocation.Status } else { "TimedOut" }
    $stderr = if ($invocation) { [string]$invocation.StandardErrorContent } else { "" }
    throw "Wrong-note development canary failed: status=$status stderr=$stderr"
}
$output = [string]$invocation.StandardOutputContent
if ($output -notmatch "WRONG_NOTE_DEVELOPMENT_CANARY_PASS") {
    throw "Wrong-note development canary success marker is missing."
}
Write-Host $output.Trim()
