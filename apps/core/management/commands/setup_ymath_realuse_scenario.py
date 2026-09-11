from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from apps.core.models import OpsAuditLog, Program, Tenant, TenantMembership
from apps.core.models.user import user_display_username, user_internal_username
from apps.core.services.password import change_password
from apps.domains.parents.services import ensure_parent_account_for_student
from apps.core.services.student_grade_report_layout import (
    STUDENT_GRADE_REPORT_LAYOUT_KEY,
    ymath_student_grade_report_layout,
)
from apps.core.management.commands.setup_three_tenants import (
    DEFAULT_FEATURE_FLAGS,
    YMATH_FEATURE_FLAGS,
)


SCENARIO_CODE_PREFIX = "qa-ymath-realuse-"
SCENARIO_CODE_RE = re.compile(r"^qa-ymath-realuse-[a-z0-9-]+$")
DEFAULT_SCENARIO_CODE = "qa-ymath-realuse-20260805"
PASSWORD_ENV = "YMATH_REALUSE_SCENARIO_PASSWORD"
LOGIN_UAT_ACCOUNT_COUNT_PER_ROLE = 10
DEVELOPMENT_SETTINGS_MODULE = "apps.api.config.settings.development"
DEVELOPMENT_DATABASE_NAME = "academy_api_development"
DEVELOPMENT_DATABASE_USER = "academy_api_development_app"
DEVELOPMENT_R2_BUCKET = "academy-development-artifacts"
DEVELOPMENT_MOCK_ALIMTALK_PF_ID = "development-mock-pfid"
DEVELOPMENT_MESSAGING_OWNER_CODE = "academy-development-owner"
DEVELOPMENT_MESSAGING_OWNER_NAME = "Academy Development Owner"
DEVELOPMENT_MOCK_ACCOUNT_TEMPLATE_DEFINITIONS = {
    "password_reset_student": {
        "name": "[DEVELOPMENT QA] 학생 비밀번호 재설정",
        "subject": "학생 비밀번호가 변경되었습니다",
        "body": (
            "#{학생이름}학생님, 비밀번호가 변경되었습니다.\n"
            "아이디: #{아이디}\n"
            "임시 비밀번호: #{임시비밀번호}\n"
            "#{비밀번호안내}\n"
            "#{사이트링크}"
        ),
    },
    "password_reset_parent": {
        "name": "[DEVELOPMENT QA] 학부모 비밀번호 재설정",
        "subject": "학부모 비밀번호가 변경되었습니다",
        "body": (
            "#{학생이름}학생 학부모님, 비밀번호가 변경되었습니다.\n"
            "아이디: #{아이디}\n"
            "임시 비밀번호: #{임시비밀번호}\n"
            "#{비밀번호안내}\n"
            "#{사이트링크}"
        ),
    },
}
LOGIN_UAT_RESERVED_USERNAME_PREFIXES = (
    "ymath-qa-student-",
    "ymath-qa-staff-",
    "staff-",
)
SCENARIO_ACTIVITY_AUDIT_ACTIONS = (
    "student_activity.login",
    "student_activity.screen_view",
    "student_activity.target_open",
)
SCENARIO_PROVENANCE_ACTION = "development.qa.scenario"
SYNTHETIC_LONG_VIDEO_DURATION_SECONDS = 900
SYNTHETIC_LONG_VIDEO_HLS_PATH = "qa-fixtures/video-long/master.m3u8"
SYNTHETIC_LONG_VIDEO_TITLE = "장시간 재생 갱신 실사용 검증"
SYNTHETIC_LONG_VIDEO_ACCESS_MODE = "PROCTORED_CLASS"


def assert_isolated_runtime() -> None:
    database = settings.DATABASES.get("default", {})
    database_name = str(database.get("NAME") or "")
    database_user = str(database.get("USER") or "")
    database_engine = str(database.get("ENGINE") or "")
    settings_module = str(os.environ.get("DJANGO_SETTINGS_MODULE") or "")
    buckets = {
        str(getattr(settings, name, "") or "")
        for name in (
            "R2_AI_BUCKET",
            "R2_STORAGE_BUCKET",
            "R2_EXCEL_BUCKET",
            "R2_ADMIN_BUCKET",
            "R2_VIDEO_BUCKET",
        )
    }
    development_runtime = (
        settings_module == DEVELOPMENT_SETTINGS_MODULE
        and database_name == DEVELOPMENT_DATABASE_NAME
        and database_user == DEVELOPMENT_DATABASE_USER
        and buckets == {DEVELOPMENT_R2_BUCKET}
    )
    test_runtime = (
        (database_engine.endswith("sqlite3") or database_name == ":memory:" or "test" in database_name.lower())
        and buckets
        and all(name.startswith("test-") for name in buckets)
    )
    if development_runtime:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            current_database, current_user = cursor.fetchone()
        development_runtime = (
            current_database == DEVELOPMENT_DATABASE_NAME
            and current_user == DEVELOPMENT_DATABASE_USER
        )
    if not (development_runtime or test_runtime):
        raise CommandError(
            "Ymath real-use scenario is allowed only in the isolated development "
            "or test database with matching non-production R2 buckets."
        )


def _ymath_program_contract() -> tuple[dict, dict]:
    source = Program.objects.filter(tenant__code="ymath", is_active=True).order_by("id").first()
    feature_flags = dict(DEFAULT_FEATURE_FLAGS)
    feature_flags.update(YMATH_FEATURE_FLAGS)
    ui_config = {
        "login_title": "Ymath 실사용 검증",
        STUDENT_GRADE_REPORT_LAYOUT_KEY: ymath_student_grade_report_layout(),
    }
    if source:
        if isinstance(source.feature_flags, dict):
            feature_flags.update(source.feature_flags)
        if isinstance(source.ui_config, dict):
            ui_config.update(source.ui_config)
    feature_flags.update(YMATH_FEATURE_FLAGS)
    return feature_flags, ui_config


def _is_persistent_development_runtime() -> bool:
    database = settings.DATABASES.get("default", {})
    return (
        str(os.environ.get("DJANGO_SETTINGS_MODULE") or "") == DEVELOPMENT_SETTINGS_MODULE
        and str(database.get("NAME") or "") == DEVELOPMENT_DATABASE_NAME
        and str(database.get("USER") or "") == DEVELOPMENT_DATABASE_USER
    )


def ensure_development_messaging_baseline() -> None:
    """Converge exact owner templates needed by mock-only account real-use QA."""

    if not _is_persistent_development_runtime():
        raise CommandError("Development messaging fixtures require the persistent development runtime.")
    if str(os.environ.get("SOLAPI_MOCK") or "").strip().lower() not in {"1", "true", "yes"}:
        raise CommandError("Development messaging fixtures require SOLAPI_MOCK=true.")
    if str(getattr(settings, "SOLAPI_KAKAO_PF_ID", "") or "").strip() != DEVELOPMENT_MOCK_ALIMTALK_PF_ID:
        raise CommandError("Development messaging fixtures require the exact mock Alimtalk PFID.")

    from apps.domains.messaging.default_templates import get_default_templates
    from apps.domains.messaging.models import AutoSendConfig, MessageTemplate

    owner_id = int(getattr(settings, "OWNER_TENANT_ID", 1))
    if connection.vendor == "postgresql":
        if not connection.in_atomic_block:
            raise RuntimeError("Development messaging baseline requires transaction.atomic().")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ["development-messaging-owner"],
            )

    owner = Tenant.objects.select_for_update().filter(pk=owner_id).first()
    if owner is None:
        code_owner = (
            Tenant.objects.select_for_update()
            .filter(code=DEVELOPMENT_MESSAGING_OWNER_CODE)
            .first()
        )
        if code_owner is not None:
            raise CommandError("Development messaging owner code is assigned to an unexpected tenant ID.")
        existing_tenant = (
            Tenant.objects.select_for_update()
            .only("id")
            .order_by("id")
            .first()
        )
        if existing_tenant is not None:
            raise CommandError(
                "Development messaging owner is missing from a non-empty development database."
            )
        owner = Tenant(
            pk=owner_id,
            code=DEVELOPMENT_MESSAGING_OWNER_CODE,
            name=DEVELOPMENT_MESSAGING_OWNER_NAME,
            is_active=True,
        )
        owner.full_clean()
        owner.save(force_insert=True)
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                for statement in connection.ops.sequence_reset_sql(no_style(), [Tenant]):
                    cursor.execute(statement)
    elif (
        owner.code != DEVELOPMENT_MESSAGING_OWNER_CODE
        or owner.name != DEVELOPMENT_MESSAGING_OWNER_NAME
        or not owner.is_active
    ):
        raise CommandError("Development messaging owner tenant identity is not exact.")

    defaults = get_default_templates(owner.name)
    definitions = {
        trigger: {
            "name": defaults[trigger]["name"],
            "subject": defaults[trigger]["subject"],
            "body": defaults[trigger]["body"],
        }
        for trigger in ("registration_approved_student", "registration_approved_parent")
    }
    definitions.update(DEVELOPMENT_MOCK_ACCOUNT_TEMPLATE_DEFINITIONS)

    for trigger, definition in definitions.items():
        config = (
            AutoSendConfig.objects.select_for_update()
            .filter(tenant=owner, trigger=trigger)
            .first()
        )
        configured_template = config.template if config else None
        if (
            configured_template is not None
            and configured_template.tenant_id == owner.id
            and str(configured_template.solapi_template_id or "").strip()
            and configured_template.solapi_status == "APPROVED"
        ):
            config.enabled = True
            config.message_mode = "alimtalk"
            config.minutes_before = None
            config.delay_mode = "immediate"
            config.delay_value = None
            config.full_clean()
            config.save()
            continue

        mock_template_id = f"development-mock-{trigger}"
        fixtures = list(
            MessageTemplate.objects.select_for_update()
            .filter(tenant=owner, solapi_template_id=mock_template_id)
            .order_by("id")[:2]
        )
        if len(fixtures) > 1:
            raise CommandError(f"Ambiguous development mock template for {trigger}.")
        template = fixtures[0] if fixtures else MessageTemplate(tenant=owner)
        template.category = MessageTemplate.Category.SIGNUP
        template.name = definition["name"]
        template.subject = definition["subject"]
        template.body = definition["body"]
        template.solapi_template_id = mock_template_id
        template.solapi_status = "APPROVED"
        template.is_system = True
        template.is_user_default = False
        template.full_clean()
        template.save()

        if config is None:
            config = AutoSendConfig(tenant=owner, trigger=trigger)
        config.template = template
        config.enabled = True
        config.message_mode = "alimtalk"
        config.minutes_before = None
        config.delay_mode = "immediate"
        config.delay_value = None
        config.full_clean()
        config.save()


def ensure_development_clinic_cancellation_fixture(tenant: Tenant) -> None:
    """Provision the tenant-owned copy required by the clinic cancel real-use flow."""

    if not _is_persistent_development_runtime():
        raise CommandError("Development messaging fixtures require the persistent development runtime.")
    if str(os.environ.get("SOLAPI_MOCK") or "").strip().lower() not in {"1", "true", "yes"}:
        raise CommandError("Development messaging fixtures require SOLAPI_MOCK=true.")
    if not SCENARIO_CODE_RE.fullmatch(str(tenant.code or "")):
        raise CommandError("Development clinic messaging fixture requires an exact qa-* tenant.")
    if not tenant.is_active or not tenant.messaging_is_active:
        raise CommandError("Development clinic messaging fixture requires active tenant messaging.")

    from apps.domains.messaging.alimtalk_content_builders import (
        get_solapi_template_id,
        get_template_type,
    )
    from apps.domains.messaging.default_templates import get_default_templates
    from apps.domains.messaging.models import AutoSendConfig, MessageTemplate

    trigger = "clinic_cancelled"
    if not get_template_type(trigger) or not get_solapi_template_id(trigger):
        raise CommandError("Development clinic cancellation requires an approved common template mapping.")

    definition = get_default_templates(tenant.name or "학원")[trigger]
    config = (
        AutoSendConfig.objects.select_for_update()
        .filter(tenant=tenant, trigger=trigger)
        .first()
    )
    if config and (config.message_mode or "alimtalk").strip().lower() != "alimtalk":
        raise CommandError("Development clinic cancellation fixture found a non-Alimtalk config.")
    template = config.template if config else None
    if template is not None and template.tenant_id != tenant.id:
        raise CommandError("Development clinic cancellation fixture found a foreign template.")
    if template is None:
        templates = list(
            MessageTemplate.objects.select_for_update()
            .filter(tenant=tenant, name=definition["name"])
            .order_by("id")[:2]
        )
        if len(templates) > 1:
            raise CommandError("Ambiguous development clinic cancellation template.")
        template = templates[0] if templates else MessageTemplate(tenant=tenant)
        template.category = definition["category"]
        template.name = definition["name"]
        template.subject = definition.get("subject", "")
        template.body = definition["body"]
        template.is_system = True
        template.is_user_default = False
        template.full_clean()
        template.save()

    if config is None:
        config = AutoSendConfig(tenant=tenant, trigger=trigger)
    config.template = template
    config.enabled = True
    config.message_mode = "alimtalk"
    config.minutes_before = definition.get("minutes_before")
    config.delay_mode = "immediate"
    config.delay_value = None
    config.full_clean()
    config.save()


class Command(BaseCommand):
    help = (
        "Create an isolated Ymath-shaped teacher, roster, lectures, and sessions "
        "for real source-file product verification."
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-code", default=DEFAULT_SCENARIO_CODE)
        parser.add_argument("--teacher-username", default="ymath-qa-teacher")
        parser.add_argument("--student-count", type=int, default=6)
        parser.add_argument("--session-count", type=int, default=24)
        parser.add_argument(
            "--login-uat",
            action="store_true",
            help="Create a secret-free 10 student + 10 parent + 10 staff login manifest.",
        )
        parser.add_argument(
            "--synthetic-long-video",
            action="store_true",
            help=(
                "Create one metadata-only 15-minute HLS fixture and exactly two "
                "PROCTORED_CLASS student accesses in an isolated development/test runtime."
            ),
        )
        lifecycle = parser.add_mutually_exclusive_group()
        lifecycle.add_argument("--reset", action="store_true")
        lifecycle.add_argument("--destroy", action="store_true")

    def handle(self, *args, **options):
        tenant_code = str(options["tenant_code"] or "").strip().lower()
        if not SCENARIO_CODE_RE.fullmatch(tenant_code):
            raise CommandError("tenant-code must match ^qa-ymath-realuse-[a-z0-9-]+$.")
        teacher_username = str(options["teacher_username"] or "").strip()
        normalized_teacher_username = teacher_username.lower()
        if not teacher_username:
            raise CommandError("teacher-username must not be empty.")
        if normalized_teacher_username.startswith(LOGIN_UAT_RESERVED_USERNAME_PREFIXES):
            raise CommandError("teacher-username conflicts with a reserved login UAT username.")

        assert_isolated_runtime()
        if options["destroy"]:
            deleted = None
            residue = {"activity_audits": 0, "outstanding_tokens": 0}
            with transaction.atomic():
                self._lock_tenant_code(tenant_code)
                existing = self._exact_tenant_or_fail_on_case_variant(tenant_code)
                if existing is not None:
                    evidence = self._cleanup_ephemeral_evidence(existing)
                    deleted = {
                        "tenant_id": existing.id,
                        "counts": self._tenant_counts(existing),
                        "users": existing.users.count(),
                        **evidence["deleted"],
                    }
                    residue = evidence["remaining"]
                    existing.delete()

            remaining = self._remaining_for_code(tenant_code)
            video_residue = self._video_residue_for_code(tenant_code)
            if any(remaining.values()):
                raise CommandError(
                    "Isolated scenario cleanup found a same-code tenant or user residue: "
                    + json.dumps(remaining, ensure_ascii=False, sort_keys=True)
                )
            if any(video_residue.values()):
                raise CommandError(
                    "Isolated scenario cleanup found video residue: "
                    + json.dumps(video_residue, sort_keys=True)
                )

            if deleted is None:
                self.stdout.write(
                    json.dumps(
                        {
                            "status": "YMATH_REALUSE_SCENARIO_ABSENT",
                            "tenant_code": tenant_code,
                            "remaining": {"tenants": 0, "users": 0},
                            "residue": residue,
                            "video_residue": video_residue,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return

            self.stdout.write(
                json.dumps(
                    {
                        "status": "YMATH_REALUSE_SCENARIO_DESTROYED",
                        "tenant_code": tenant_code,
                        "tenant_id": deleted["tenant_id"],
                        "deleted": {
                            **deleted["counts"],
                            "users": deleted["users"],
                            "activity_audits": deleted["activity_audits"],
                            "outstanding_tokens": deleted["outstanding_tokens"],
                        },
                        "remaining": remaining,
                        "residue": residue,
                        "video_residue": video_residue,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return

        login_uat = bool(options["login_uat"])
        student_count = LOGIN_UAT_ACCOUNT_COUNT_PER_ROLE if login_uat else int(options["student_count"])
        session_count = int(options["session_count"])
        synthetic_long_video = bool(options["synthetic_long_video"])
        if not 1 <= student_count <= 30:
            raise CommandError("student-count must be between 1 and 30.")
        if not 1 <= session_count <= 80:
            raise CommandError("session-count must be between 1 and 80.")
        if synthetic_long_video and student_count != 2:
            raise CommandError("--synthetic-long-video requires --student-count=2.")

        password = str(os.environ.get(PASSWORD_ENV) or "")
        if not password:
            raise CommandError(f"{PASSWORD_ENV} must be set.")
        if login_uat:
            parent_login_ids = {
                f"01099{index:06d}"
                for index in range(1, LOGIN_UAT_ACCOUNT_COUNT_PER_ROLE + 1)
            }
            if teacher_username in parent_login_ids:
                raise CommandError("teacher-username conflicts with a generated parent login identifier.")

        reset_counts = None
        synthetic_long_video_payload = None
        with transaction.atomic():
            self._lock_tenant_code(tenant_code)
            if _is_persistent_development_runtime():
                ensure_development_messaging_baseline()
            existing = self._exact_tenant_or_fail_on_case_variant(tenant_code)
            if login_uat and existing is not None and not options["reset"]:
                raise CommandError("--login-uat requires --reset when the tenant already exists.")
            reset_counts = self._tenant_counts(existing) if existing and options["reset"] else None
            if existing and options["reset"]:
                self._cleanup_ephemeral_evidence(existing)
                existing.delete()

            tenant, _ = Tenant.objects.get_or_create(
                code=tenant_code,
                defaults={
                    "name": "Ymath 실사용 복제 검증",
                    "is_active": True,
                },
            )
            tenant.name = "Ymath 실사용 복제 검증"
            tenant.is_active = True
            tenant.save(update_fields=["name", "is_active"])
            if _is_persistent_development_runtime():
                ensure_development_clinic_cancellation_fixture(tenant)
            self._ensure_scenario_provenance(tenant)

            feature_flags, ui_config = _ymath_program_contract()
            program, _ = Program.objects.get_or_create(tenant=tenant)
            program.display_name = "Ymath 실사용 검증"
            program.brand_key = "ymath"
            program.login_variant = Program.LoginVariant.HAKWONPLUS
            program.plan = Program.Plan.ALL
            scenario_started_at = timezone.localdate()
            program.subscription_status = Program.SubscriptionStatus.ACTIVE
            program.subscription_started_at = scenario_started_at
            program.subscription_expires_at = scenario_started_at + timedelta(days=365)
            program.cancel_at_period_end = False
            program.feature_flags = feature_flags
            program.ui_config = ui_config
            program.is_active = True
            program.save()

            teacher = self._ensure_user(
                tenant=tenant,
                login_username=teacher_username,
                password=password,
                name="Ymath QA 선생님",
                is_staff=True,
            )
            TenantMembership.ensure_active(
                tenant=tenant,
                user=teacher,
                role="admin",
            )

            Lecture = apps.get_model("lectures", "Lecture")
            Session = apps.get_model("lectures", "Session")
            Enrollment = apps.get_model("enrollment", "Enrollment")
            SessionEnrollment = apps.get_model("enrollment", "SessionEnrollment")
            Student = apps.get_model("students", "Student")
            Staff = apps.get_model("staffs", "Staff")

            lecture_specs = (
                ("공통수학2 정규반", "공수", "#3158d4"),
                ("대수 심화반", "대수", "#7c3aed"),
            )
            lectures = []
            sessions = []
            start = date(2026, 8, 3)
            for lecture_index, (title, chip, color) in enumerate(lecture_specs):
                lecture, _ = Lecture.objects.update_or_create(
                    tenant=tenant,
                    title=title,
                    defaults={
                        "name": title,
                        "subject": "MATH",
                        "description": "실제 Ymath 원본 전수 검증용 격리 강의",
                        "lecture_time": "월·수 18:00" if lecture_index == 0 else "화·목 20:00",
                        "color": color,
                        "chip_label": chip,
                        "is_active": True,
                    },
                )
                lectures.append(lecture)
                for order in range(1, session_count + 1):
                    session, _ = Session.objects.update_or_create(
                        lecture=lecture,
                        section=None,
                        order=order,
                        defaults={
                            "session_type": Session.SessionType.REGULAR,
                            "regular_order": order,
                            "title": f"{order}차시 실사용 자료",
                            "date": start + timedelta(days=(order - 1) * 3 + lecture_index),
                        },
                    )
                    sessions.append(session)

            students = []
            parents = []
            for index in range(1, student_count + 1):
                student_user = self._ensure_user(
                    tenant=tenant,
                    login_username=f"ymath-qa-student-{index:02d}",
                    password=password,
                    name=f"검증학생 {index:02d}",
                    is_staff=False,
                )
                TenantMembership.ensure_active(
                    tenant=tenant,
                    user=student_user,
                    role="student",
                )
                parent = None
                if login_uat:
                    parent_result = ensure_parent_account_for_student(
                        tenant=tenant,
                        parent_phone=f"01099{index:06d}",
                        student_name=f"검증학생 {index:02d}",
                        initial_password=password,
                    )
                    parent = parent_result.parent
                    change_password(parent.user, password)
                    parents.append(parent)
                student_defaults = {
                    "name": f"검증학생 {index:02d}",
                    "ps_number": f"QA-{index:04d}",
                    "omr_code": f"98{index:06d}",
                    "phone": f"01098{index:06d}",
                    "parent_phone": f"01099{index:06d}",
                    "grade": 1 + ((index - 1) % 3),
                    "school_type": "HIGH",
                    "high_school": "검증고등학교",
                    "memo": "격리 개발환경 실사용 시나리오 학생",
                }
                if parent is not None:
                    student_defaults["parent"] = parent
                student, _ = Student.objects.update_or_create(
                    tenant=tenant,
                    user=student_user,
                    defaults=student_defaults,
                )
                students.append(student)
                for lecture in lectures:
                    enrollment, _ = Enrollment.objects.update_or_create(
                        tenant=tenant,
                        student=student,
                        lecture=lecture,
                        defaults={"status": "ACTIVE"},
                    )
                    lecture_sessions = [item for item in sessions if item.lecture_id == lecture.id]
                    for session in lecture_sessions:
                        SessionEnrollment.objects.get_or_create(
                            tenant=tenant,
                            enrollment=enrollment,
                            session=session,
                        )

            login_staff = []
            if login_uat:
                for index in range(1, LOGIN_UAT_ACCOUNT_COUNT_PER_ROLE + 1):
                    staff_user = self._ensure_user(
                        tenant=tenant,
                        login_username=f"ymath-qa-staff-{index:02d}",
                        password=password,
                        name=f"로그인 검증 직원 {index:02d}",
                        is_staff=True,
                    )
                    TenantMembership.ensure_active(
                        tenant=tenant,
                        user=staff_user,
                        role="staff",
                    )
                    staff, _ = Staff.objects.update_or_create(
                        tenant=tenant,
                        user=staff_user,
                        defaults={
                            "name": f"로그인 검증 직원 {index:02d}",
                            "phone": f"01097{index:06d}",
                            "is_active": True,
                        },
                    )
                    login_staff.append(staff)

            if synthetic_long_video:
                Video = apps.get_model("video", "Video")
                VideoAccess = apps.get_model("video", "VideoAccess")
                video, _ = Video.objects.update_or_create(
                    tenant=tenant,
                    session=sessions[0],
                    folder=None,
                    order=1,
                    defaults={
                        "title": SYNTHETIC_LONG_VIDEO_TITLE,
                        "source_type": Video.SourceType.UPLOADED,
                        "file_key": "",
                        "duration": SYNTHETIC_LONG_VIDEO_DURATION_SECONDS,
                        "status": Video.Status.READY,
                        "hls_path": SYNTHETIC_LONG_VIDEO_HLS_PATH,
                        "allow_skip": False,
                        "max_speed": 1.0,
                        "show_watermark": True,
                    },
                )
                fixture_enrollments = list(
                    Enrollment.objects.filter(
                        tenant=tenant,
                        lecture=lectures[0],
                        student__in=students,
                        status="ACTIVE",
                    ).order_by("student_id")
                )
                if len(fixture_enrollments) != 2:
                    raise CommandError(
                        "Synthetic long-video fixture requires exactly two active enrollments."
                    )
                for enrollment in fixture_enrollments:
                    VideoAccess.objects.update_or_create(
                        video=video,
                        enrollment=enrollment,
                        defaults={
                            "access_mode": SYNTHETIC_LONG_VIDEO_ACCESS_MODE,
                            "rule": "once",
                            "is_override": True,
                        },
                    )
                synthetic_long_video_payload = {
                    "access_mode": SYNTHETIC_LONG_VIDEO_ACCESS_MODE,
                    "duration_seconds": SYNTHETIC_LONG_VIDEO_DURATION_SECONDS,
                    "hls_path": SYNTHETIC_LONG_VIDEO_HLS_PATH,
                    "video_accesses": len(fixture_enrollments),
                    "video_id": video.id,
                }

            if login_uat:
                self._validate_login_uat_contract(
                    tenant=tenant,
                    teacher=teacher,
                    students=students,
                    parents=parents,
                    staffs=login_staff,
                )
                self._validate_active_login_identifiers(
                    tenant=tenant,
                    teacher=teacher,
                    students=students,
                    parents=parents,
                    staffs=login_staff,
                )

        if reset_counts is not None:
            self.stdout.write(
                self.style.WARNING(
                    "Deleted isolated scenario tenant before rebuild: "
                    + json.dumps(reset_counts, ensure_ascii=False, sort_keys=True)
                )
            )

        payload = {
            "status": "YMATH_REALUSE_SCENARIO_READY",
            "tenant_code": tenant.code,
            "tenant_id": tenant.id,
            "teacher_username": teacher_username,
            "teacher_user_id": teacher.id,
            "subscription_expires_at": program.subscription_expires_at.isoformat(),
            "student_ids": [student.id for student in students],
            "lecture_ids": [lecture.id for lecture in lectures],
            "session_ids": [session.id for session in sessions],
            "counts": self._tenant_counts(tenant),
            "video_state": self._video_residue_for_code(tenant.code),
        }
        if synthetic_long_video_payload is not None:
            payload["synthetic_long_video"] = synthetic_long_video_payload
        if login_uat:
            accounts = [
                {
                    "role": "student",
                    "username": user_display_username(student.user),
                    "landing_path": "/student",
                }
                for student in students
            ]
            accounts.extend(
                {
                    "role": "parent",
                    "username": user_display_username(parent.user),
                    "landing_path": "/student",
                }
                for parent in parents
            )
            accounts.extend(
                {
                    "role": "staff",
                    "username": user_display_username(staff.user),
                    "landing_path": "/workspace/mobile",
                }
                for staff in login_staff
            )
            payload["login_manifest"] = {
                "schema_version": 1,
                "tenant_code": tenant.code,
                "account_count": len(accounts),
                "accounts": accounts,
            }
        self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _lock_tenant_code(tenant_code: str) -> None:
        if connection.vendor != "postgresql":
            return
        if not connection.in_atomic_block:
            raise RuntimeError("tenant-code advisory lock requires transaction.atomic().")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"ymath-realuse:{tenant_code}"],
            )

    @staticmethod
    def _exact_tenant_or_fail_on_case_variant(tenant_code: str):
        matches = list(
            Tenant.objects.select_for_update()
            .filter(code__iexact=tenant_code)
            .only("id", "code")
            .order_by("id")
        )
        variants = [tenant.code for tenant in matches if tenant.code != tenant_code]
        if variants:
            raise CommandError(
                "A case-variant tenant code already exists; refusing setup or destroy."
            )
        return next((tenant for tenant in matches if tenant.code == tenant_code), None)

    @staticmethod
    def _ensure_user(*, tenant, login_username, password, name, is_staff):
        User = get_user_model()
        internal_username = user_internal_username(tenant, login_username)
        user, _ = User.objects.get_or_create(
            username=internal_username,
            defaults={
                "tenant": tenant,
                "name": name,
                "is_active": True,
                "is_staff": is_staff,
            },
        )
        user.tenant = tenant
        user.name = name
        user.is_active = True
        user.is_staff = is_staff
        user.save(update_fields=["tenant", "name", "is_active", "is_staff"])
        change_password(user, password)
        return user

    @staticmethod
    def _remaining_for_code(tenant_code: str) -> dict[str, int]:
        return {
            "tenants": Tenant.objects.filter(code__iexact=tenant_code).count(),
            "users": get_user_model().objects.filter(tenant__code__iexact=tenant_code).count(),
        }

    @staticmethod
    def _video_residue_for_code(tenant_code: str) -> dict[str, int]:
        Video = apps.get_model("video", "Video")
        VideoAccess = apps.get_model("video", "VideoAccess")
        VideoProgress = apps.get_model("video", "VideoProgress")
        VideoPlaybackSession = apps.get_model("video", "VideoPlaybackSession")
        VideoPlaybackEvent = apps.get_model("video", "VideoPlaybackEvent")
        video_scope = {"video__tenant__code__iexact": tenant_code}
        events = VideoPlaybackEvent.objects.filter(**video_scope)
        sessions = VideoPlaybackSession.objects.filter(**video_scope)
        return {
            "active_playback_sessions": sessions.filter(status="ACTIVE").count(),
            "playback_events": events.count(),
            "playback_sessions": sessions.count(),
            "player_errors": events.filter(event_type="PLAYER_ERROR").count(),
            "proctored_video_accesses": VideoAccess.objects.filter(
                **video_scope,
                access_mode="PROCTORED_CLASS",
            ).count(),
            "video_accesses": VideoAccess.objects.filter(**video_scope).count(),
            "video_progresses": VideoProgress.objects.filter(**video_scope).count(),
            "videos": Video.all_with_deleted.filter(tenant__code__iexact=tenant_code).count(),
            "violated_events": events.filter(violated=True).count(),
        }

    @staticmethod
    def _cleanup_ephemeral_evidence(tenant) -> dict[str, dict[str, int]]:
        """Delete only transient evidence owned by one exact disposable scenario."""

        user_ids = list(tenant.users.values_list("id", flat=True))
        token_rows = OutstandingToken.objects.filter(user_id__in=user_ids)
        token_ids = list(token_rows.values_list("id", flat=True))
        activity_rows = Command._owned_activity_rows(tenant=tenant, user_ids=user_ids)
        activity_ids = list(activity_rows.values_list("id", flat=True))

        deleted = {
            "activity_audits": len(activity_ids),
            "outstanding_tokens": len(token_ids),
        }
        if activity_ids:
            OpsAuditLog.objects.filter(id__in=activity_ids).delete()
        if token_ids:
            OutstandingToken.objects.filter(id__in=token_ids).delete()
        remaining = {
            "activity_audits": OpsAuditLog.objects.filter(id__in=activity_ids).count(),
            "outstanding_tokens": OutstandingToken.objects.filter(id__in=token_ids).count(),
        }
        if any(remaining.values()):
            raise CommandError(
                "Isolated scenario cleanup left owned token or activity-audit residue: "
                + json.dumps(remaining, sort_keys=True)
            )
        return {"deleted": deleted, "remaining": remaining}

    @staticmethod
    def _owned_database_residue(tenant) -> dict[str, int]:
        user_ids = list(tenant.users.values_list("id", flat=True))
        activity_rows = Command._owned_activity_rows(tenant=tenant, user_ids=user_ids)
        return {
            "activity_audits": activity_rows.count(),
            "outstanding_tokens": OutstandingToken.objects.filter(user_id__in=user_ids).count(),
        }

    @staticmethod
    def _owned_activity_rows(*, tenant, user_ids):
        activity_rows = OpsAuditLog.objects.filter(
            action__in=SCENARIO_ACTIVITY_AUDIT_ACTIONS,
            target_tenant=tenant,
            target_user_id__in=user_ids,
            actor_user_id__in=user_ids,
            created_at__lte=timezone.now(),
        )
        ownership_started_at = (
            OpsAuditLog.objects.filter(
                action="development.qa.setup",
                target_tenant=tenant,
                result="success",
                payload__tenant_code=tenant.code,
                payload__tenant_id=tenant.id,
            )
            .order_by("created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        if ownership_started_at is None:
            ownership_started_at = (
                OpsAuditLog.objects.filter(
                    action=SCENARIO_PROVENANCE_ACTION,
                    target_tenant=tenant,
                    result="success",
                    payload__tenant_code=tenant.code,
                    payload__tenant_id=tenant.id,
                )
                .order_by("created_at")
                .values_list("created_at", flat=True)
                .first()
            )
        if ownership_started_at is None:
            if activity_rows.exists():
                raise CommandError(
                    "Refusing to delete scenario activity without an exact QA ownership provenance seal."
                )
            return activity_rows.none()
        return activity_rows.filter(created_at__gte=ownership_started_at)

    @staticmethod
    def _ensure_scenario_provenance(tenant) -> None:
        exact = {
            "action": SCENARIO_PROVENANCE_ACTION,
            "target_tenant": tenant,
            "result": "success",
            "payload__tenant_code": tenant.code,
            "payload__tenant_id": tenant.id,
        }
        if not OpsAuditLog.objects.filter(**exact).exists():
            OpsAuditLog.objects.create(
                action=SCENARIO_PROVENANCE_ACTION,
                actor_username="setup_ymath_realuse_scenario",
                target_tenant=tenant,
                payload={"tenant_code": tenant.code, "tenant_id": tenant.id},
                result="success",
            )

    @staticmethod
    def _qa_r2_prefixes(tenant_id: int) -> tuple[str, ...]:
        if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id <= 0:
            raise CommandError("Frontend QA R2 cleanup requires an exact positive tenant ID.")
        return (
            f"tenants/{tenant_id}/",
            f"excel/{tenant_id}/",
            f"tenant-logos/{tenant_id}/",
            f"landing-public/reviews/{tenant_id}/",
            f"matchup-showcase-snapshots/tenant_{tenant_id}/",
        )

    @staticmethod
    def _r2_storage_client():
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=settings.R2_ENDPOINT,
            region_name=settings.R2_REGION,
            aws_access_key_id=settings.R2_ACCESS_KEY,
            aws_secret_access_key=settings.R2_SECRET_KEY,
            config=Config(
                connect_timeout=10,
                read_timeout=10,
                retries={"total_max_attempts": 2},
            ),
        )

    @staticmethod
    def _cleanup_qa_r2_objects(*, tenant_id: int) -> dict[str, int]:
        """Delete only exact disposable-tenant prefixes and prove a zero readback."""

        assert_isolated_runtime()
        client = Command._r2_storage_client()
        deleted = 0
        for prefix in Command._qa_r2_prefixes(tenant_id):
            while True:
                page = client.list_objects_v2(
                    Bucket=settings.R2_STORAGE_BUCKET,
                    Prefix=prefix,
                    MaxKeys=1000,
                )
                keys = [item["Key"] for item in page.get("Contents") or []]
                if any(not key.startswith(prefix) for key in keys):
                    raise CommandError("R2 returned an object outside the exact QA prefix.")
                if not keys:
                    break
                response = client.delete_objects(
                    Bucket=settings.R2_STORAGE_BUCKET,
                    Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
                )
                errors = response.get("Errors") or []
                if errors:
                    raise CommandError(
                        f"Frontend QA R2 cleanup failed for {len(errors)} object(s)."
                    )
                deleted += len(keys)
        return {"deleted": deleted, "remaining": 0}

    @staticmethod
    def _non_database_residue(*, tenant_id: int | None, tenant_code: str) -> dict[str, int]:
        client = Command._r2_storage_client()
        r2_objects = 0
        if tenant_id is not None:
            for prefix in Command._qa_r2_prefixes(tenant_id):
                continuation_token = None
                while True:
                    request = {
                        "Bucket": settings.R2_STORAGE_BUCKET,
                        "Prefix": prefix,
                        "MaxKeys": 1000,
                    }
                    if continuation_token:
                        request["ContinuationToken"] = continuation_token
                    page = client.list_objects_v2(**request)
                    r2_objects += len(page.get("Contents") or [])
                    if not page.get("IsTruncated"):
                        break
                    continuation_token = page["NextContinuationToken"]

        marker = f"QA_TENANT={tenant_code}".encode()
        processes = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if marker in environment:
                processes += 1

        expected_port = f"{18000:04X}"
        listeners = 0
        for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
            for line in Path(filename).read_text().splitlines()[1:]:
                columns = line.split()
                if len(columns) >= 4 and columns[1].rsplit(":", 1)[-1] == expected_port and columns[3] == "0A":
                    listeners += 1
        return {"listeners": listeners, "processes": processes, "r2_objects": r2_objects}

    @staticmethod
    def _validate_login_uat_contract(*, tenant, teacher, students, parents, staffs) -> None:
        Student = apps.get_model("students", "Student")
        Parent = apps.get_model("parents", "Parent")
        Staff = apps.get_model("staffs", "Staff")
        memberships = TenantMembership.objects.filter(tenant=tenant, is_active=True)
        role_counts = {role: memberships.filter(role=role).count() for role in ("student", "parent", "staff", "admin")}
        model_counts = {
            "student": Student.objects.filter(tenant=tenant).count(),
            "parent": Parent.objects.filter(tenant=tenant).count(),
            "staff": Staff.objects.filter(tenant=tenant).count(),
        }
        account_user_ids = [student.user_id for student in students]
        account_user_ids.extend(parent.user_id for parent in parents)
        account_user_ids.extend(staff.user_id for staff in staffs)
        expected = LOGIN_UAT_ACCOUNT_COUNT_PER_ROLE
        valid = (
            model_counts == {"student": expected, "parent": expected, "staff": expected}
            and role_counts == {"student": expected, "parent": expected, "staff": expected, "admin": 1}
            and len(account_user_ids) == expected * 3
            and len(set(account_user_ids)) == expected * 3
            and memberships.filter(role="admin", user=teacher).count() == 1
        )
        if not valid:
            raise CommandError(
                "Login UAT database contract mismatch: "
                + json.dumps(
                    {
                        "model_counts": model_counts,
                        "role_counts": role_counts,
                        "distinct_manifest_users": len(set(account_user_ids)),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

    @staticmethod
    def _validate_active_login_identifiers(*, tenant, teacher, students, parents, staffs) -> None:
        identifiers: dict[str, set[int]] = defaultdict(set)
        memberships = (
            TenantMembership.objects
            .filter(tenant=tenant, is_active=True, user__is_active=True)
            .select_related("user")
        )
        for membership in memberships:
            identifier = user_display_username(membership.user).strip()
            if identifier:
                identifiers[identifier].add(membership.user_id)

        Parent = apps.get_model("parents", "Parent")
        for parent in (
            Parent.objects
            .filter(
                tenant=tenant,
                user__is_active=True,
                user__tenant_memberships__tenant=tenant,
                user__tenant_memberships__role="parent",
                user__tenant_memberships__is_active=True,
            )
            .select_related("user")
            .distinct()
        ):
            identifier = str(parent.phone or "").strip()
            if identifier:
                identifiers[identifier].add(parent.user_id)

        expected_identifiers = {user_display_username(teacher): teacher.id}
        expected_identifiers.update(
            {user_display_username(student.user): student.user_id for student in students}
        )
        expected_identifiers.update({str(parent.phone): parent.user_id for parent in parents})
        expected_identifiers.update(
            {user_display_username(staff.user): staff.user_id for staff in staffs}
        )
        ambiguous = {
            identifier: sorted(user_ids)
            for identifier, user_ids in identifiers.items()
            if len(user_ids) != 1
        }
        unresolved = {
            identifier: {
                "expected_user_id": expected_user_id,
                "resolved_user_ids": sorted(identifiers.get(identifier, set())),
            }
            for identifier, expected_user_id in expected_identifiers.items()
            if identifiers.get(identifier) != {expected_user_id}
        }
        if ambiguous or unresolved:
            raise CommandError(
                "Login UAT display identifier contract mismatch: "
                + json.dumps(
                    {"ambiguous": ambiguous, "unresolved": unresolved},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

    @staticmethod
    def _tenant_counts(tenant) -> dict[str, int]:
        model_names = {
            "students": ("students", "Student"),
            "parents": ("parents", "Parent"),
            "staffs": ("staffs", "Staff"),
            "lectures": ("lectures", "Lecture"),
            "sessions": ("lectures", "Session"),
            "enrollments": ("enrollment", "Enrollment"),
            "exams": ("exams", "Exam"),
            "homeworks": ("homework_results", "Homework"),
        }
        counts = {}
        for label, (app_label, model_name) in model_names.items():
            model = apps.get_model(app_label, model_name)
            if label == "sessions":
                counts[label] = model.objects.filter(lecture__tenant=tenant).count()
            else:
                counts[label] = model.objects.filter(tenant=tenant).count()
        return counts
