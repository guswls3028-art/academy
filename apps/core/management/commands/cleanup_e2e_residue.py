# PATH: apps/core/management/commands/cleanup_e2e_residue.py
"""
E2E 테스트 잔재 데이터 정리 커맨드.

배경:
    운영 테넌트(Tenant 1 / hakwonplus)에 E2E 자동화 스펙이 생성한 학생·게시글·
    메시지 템플릿·매치업 파일·수납 비목이 누적되어 학원 운영 화면의 품질을 저하시킴.

매칭 패턴 (자동화 스펙이 찍은 명백한 지문):
    - "[E2E-\\d+" / "[AUDIT-\\d+" / "[CHAOS-\\d+"
    - "[E2E-<label>-<10자리 이상 실행시각>]"
    - 과거 자동화가 고정값으로 만든 세 개의 커뮤니티 게시글 제목
    - "E2E-\\d{6,}" / "AUDIT-CRUD-\\d+"
    - "EDITED-\\d{6}" (내부 테스트 흔적)
    일반적인 "테스트학생" 같은 자연어는 의도적으로 배제 — 운영에서 이름이
    우연히 겹칠 수 있으므로 strict 패턴만 사용한다.

안전장치:
    - 기본 동작은 --dry-run (삭제하지 않음)
    - --tenant-id 미지정 시 거부 (전 테넌트 일괄 정리 금지)
    - 삭제 직전 요약을 표준출력으로 노출

사용:
    python manage.py cleanup_e2e_residue --tenant-id 1 --dry-run
    python manage.py cleanup_e2e_residue --tenant-id 1 --execute --confirm-token <dry-run token>
"""
import hashlib
import re
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

# 명백한 E2E 지문 — 자연어와 겹치지 않는 식별자 패턴만 허용
RESIDUE_PATTERNS = [
    re.compile(r"\[E2E-\d{6,}"),
    # 2026-05 OMR 실측 자동화: [E2E-OMR[-PURPOSE]-YYYYMMDDhhmmss]
    # 14자리 실행 시각과 대문자 automation label을 모두 요구해 자연어와
    # 겹치지 않도록 한다.
    re.compile(r"^\[E2E-[A-Z][A-Z0-9-]*-\d{14}\]"),
    re.compile(r"\[E2E\] "),                 # 2026-05-02: 운영 잔재 [E2E] 공지... 형식
    re.compile(r"\[AUDIT-\w*-?\d{6,}"),
    re.compile(r"\[CHAOS-\d{3,}"),
    re.compile(r"^E2E-\d{6,}"),
    re.compile(r"AUDIT-CRUD-\d{6,}"),
    re.compile(r"^EDITED-\d{5,}$"),
    # 괄호 없는 타임스탬프 접두 패턴 — 자연어에 나타날 수 없음
    re.compile(r"^E2E학생\d{6,}"),
    re.compile(r"^E2E\d{6,}"),
    re.compile(r"^E2E메시지\d+"),             # 2026-05-02: 송장에 노출된 E2E 발신 학생명
    re.compile(r"^E2E ?Test ?Exam \d{6,}"),  # 2026-05-02: 운영 시험 도배 패턴
    re.compile(r"^E2E시험"),
    re.compile(r"^두번째 시험임$"),           # 2026-05-02: 명시적 테스트 명칭
    re.compile(r"^CHAOS-\d{6,}"),
    # 2026-05 Matchup 실측 자동화: [E2E-cut-fix-1778777196]
    # label과 10자리 이상의 실행 시각을 모두 요구한다. 날짜만 있는 사용자 이름은
    # 자동 정리 대상으로 넓히지 않는다.
    re.compile(r"^\[E2E-[A-Za-z][A-Za-z0-9-]*-\d{10,}\]"),
    # 2026-05 직원 성적 감사 자동화가 남긴 날짜+회차 지문.
    re.compile(r"^\[E2E-SCORE-AUDIT-\d{8}-\d{2,}\]"),
]

LEGACY_EXACT_RESIDUE_VALUES = frozenset({
    "[E2E-selflike-test]",
    "[E2E-test] board",
    "[E2E-persist-test]",
})

COPY_PREFIX_RE = re.compile(r"^(?:(?:복사)\s*-\s*)+")
COPY_CHAIN_MIN_ROWS = 10
COPY_CHAIN_MIN_DISTINCT_DEPTHS = 5

def matches_residue(text: str) -> bool:
    if not text:
        return False
    return text in LEGACY_EXACT_RESIDUE_VALUES or any(
        pattern.search(text) for pattern in RESIDUE_PATTERNS
    )


def matches_template_residue(text: str) -> bool:
    return matches_residue(text)


def template_copy_depth_and_base(name: str) -> tuple[int, str]:
    match = COPY_PREFIX_RE.match(name or "")
    if not match:
        return 0, (name or "").strip()
    depth = len(re.findall(r"복사\s*-\s*", match.group(0)))
    return depth, (name[match.end():] or "").strip()


def find_template_copy_chain_residue(templates) -> list:
    """Return only redundant copies from a high-confidence recursive copy chain."""
    groups: dict[tuple, list[tuple[object, int]]] = {}
    for template in templates:
        depth, base_name = template_copy_depth_and_base(template.name or "")
        key = (
            template.tenant_id,
            template.category,
            base_name,
            template.subject,
            template.body,
            template.solapi_template_id,
            template.solapi_status,
        )
        groups.setdefault(key, []).append((template, depth))

    residue = []
    for group in groups.values():
        copy_rows = [(template, depth) for template, depth in group if depth > 0]
        distinct_depths = {depth for _template, depth in copy_rows}
        if (
            len(copy_rows) < COPY_CHAIN_MIN_ROWS
            or len(distinct_depths) < COPY_CHAIN_MIN_DISTINCT_DEPTHS
        ):
            continue
        residue.extend(template for template, _depth in copy_rows)
    return sorted(residue, key=lambda template: template.id)


def build_confirmation_token(*, tenant_id: int, target_groups: dict[str, list]) -> str:
    """Bind an execute attempt to the exact dry-run target set."""
    target_parts = [f"tenant:{tenant_id}"]
    for label in sorted(target_groups):
        ids = [item.id for item in target_groups[label]]
        ids.sort(
            key=lambda item_id: (
                (0, int(item_id))
                if str(item_id).isdigit()
                else (1, str(item_id))
            )
        )
        target_parts.append(f"{label}:{','.join(str(item_id) for item_id in ids)}")
    return hashlib.sha256("|".join(target_parts).encode("utf-8")).hexdigest()


class Command(BaseCommand):
    help = "Tenant별 E2E 자동화 잔재 데이터(학생·직원·강의·차시·시험·게시글·메시지 템플릿·매치업 문서·수납 비목)를 식별/삭제한다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=int,
            required=True,
            help="대상 테넌트 ID (필수). 실수 방지용 — 전 테넌트 일괄 삭제 금지.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=True,
            help="대상만 출력하고 삭제하지 않음 (기본값).",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="실제 삭제 실행. 직전 dry-run의 --confirm-token이 함께 필요.",
        )
        parser.add_argument(
            "--confirm-token",
            default="",
            help="직전 dry-run이 출력한 exact-target 확인 토큰.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="각 카테고리별 출력 샘플 상한 (기본 50).",
        )

    def handle(self, *args, **options):
        tenant_id: int = options["tenant_id"]
        execute: bool = options["execute"]
        limit: int = options["limit"]
        confirm_token: str = (options["confirm_token"] or "").strip()

        # 모델 지연 import (apps 레지스트리 완료 후)
        from apps.domains.students.models import Student
        from apps.domains.community.models.post import PostEntity
        from apps.domains.matchup.models import MatchupDocument
        from apps.domains.messaging.models import MessageTemplate
        from apps.domains.staffs.models import Staff
        from apps.domains.exams.models.exam import Exam
        from apps.domains.homework_results.models.homework import Homework
        from apps.domains.lectures.models import Lecture, Session
        from apps.domains.fees.models import FeeTemplate, InvoiceItem
        from apps.domains.progress.models import ClinicLink
        from apps.domains.results.models import Result
        from apps.domains.submissions.models import (
            OmrUploadBatch,
            OmrUploadBatchItem,
            Submission,
        )
        from apps.core.models import Tenant

        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise CommandError(f"tenant_id={tenant_id} not found")

        self.stdout.write(f"대상 테넌트: {tenant.name} (id={tenant.id})")

        # 1. 학생 — name 또는 ps_number
        students = [
            s for s in Student.objects.filter(tenant_id=tenant_id)
            if matches_residue(s.name or "") or matches_residue(s.ps_number or "")
        ]

        # 2. 커뮤니티 게시글 — title
        posts = [
            p for p in PostEntity.objects.filter(tenant_id=tenant_id)
            if matches_residue(p.title or "")
        ]

        # 3. 매치업 문서 — title
        matchups = [
            m for m in MatchupDocument.objects.filter(tenant_id=tenant_id)
            if matches_residue(m.title or "")
        ]

        # 4. 메시지 템플릿 — name (시스템 템플릿 제외)
        all_user_templates = list(
            MessageTemplate.objects.filter(tenant_id=tenant_id, is_system=False)
        )
        templates = [
            t for t in all_user_templates
            if matches_template_residue(t.name or "")
        ]
        marked_template_ids = {template.id for template in templates}
        recursive_templates = [
            template
            for template in find_template_copy_chain_residue(all_user_templates)
            if template.id not in marked_template_ids
        ]

        # 4-a. 직원 — 날짜+회차가 포함된 strict automation marker만.
        staffs = [
            staff
            for staff in Staff.objects.filter(tenant_id=tenant_id)
            if matches_residue(staff.name or "")
        ]

        # 5. 시험 — title (template/regular 모두). 2026-05-02 운영 [E2E Test Exam ...] 도배 sweep.
        exams = [
            e for e in Exam.objects.filter(tenant_id=tenant_id)
            if matches_residue(e.title or "")
        ]
        exam_ids = [exam.id for exam in exams]
        exam_submissions = list(
            Submission.objects.filter(
                tenant_id=tenant_id,
                target_type=Submission.TargetType.EXAM,
                target_id__in=exam_ids,
            )
        )
        omr_batches = list(
            OmrUploadBatch.objects.filter(
                tenant_id=tenant_id,
                exam_id__in=exam_ids,
            )
        )
        omr_batch_items = list(
            OmrUploadBatchItem.objects.filter(batch__in=omr_batches)
        )

        # 6. 과제 — title. 동일 패턴.
        homeworks = [
            h for h in Homework.objects.filter(tenant_id=tenant_id)
            if matches_residue(h.title or "")
        ]

        # 7. 강의/차시 — title. 강의와 그 하위 차시가 모두 strict marker일 때만
        #    강의 단위로 정리하고, 정상 강의 아래의 marker 차시는 별도 대상으로 둔다.
        lectures = [
            lecture
            for lecture in Lecture.objects.filter(tenant_id=tenant_id, is_system=False)
            if matches_residue(lecture.title or "")
        ]
        lecture_ids = {lecture.id for lecture in lectures}
        sessions = [
            session
            for session in Session.objects.filter(lecture__tenant_id=tenant_id)
            if session.lecture_id not in lecture_ids
            and matches_residue(session.title or "")
        ]

        # 8. 수납 비목 — name. 연결된 학생비용/청구항목이 있으면 삭제 대신 비활성화한다.
        fee_templates = [
            f for f in FeeTemplate.objects.filter(tenant_id=tenant_id)
            if matches_residue(f.name or "")
        ]

        def fee_template_ref_count(fee_template) -> int:
            return (
                fee_template.student_fees.count()
                + InvoiceItem.objects.filter(fee_template=fee_template).count()
            )

        def fee_template_action(fee_template) -> str:
            return "deactivate" if fee_template_ref_count(fee_template) else "delete"

        total = (
            len(students) + len(posts) + len(matchups) + len(templates)
            + len(recursive_templates) + len(staffs)
            + len(exams) + len(homeworks) + len(lectures) + len(sessions)
            + len(fee_templates)
        )

        self._print_group(
            "학생 (Student)",
            students,
            limit,
            lambda s: (
                f"id={s.id} ps={s.ps_number} name={s.name!r} "
                f"deleted={s.deleted_at is not None} user_id={s.user_id}"
            ),
        )
        self._print_group("게시글 (PostEntity)", posts, limit, lambda p: f"id={p.id} title={p.title!r}")
        self._print_group(
            "매치업 문서 (MatchupDocument)",
            matchups,
            limit,
            lambda m: (
                f"id={m.id} inventory_id={m.inventory_file_id} title={m.title!r} "
                f"problems={m.problems.count()} reports={m.hit_reports.count()}"
            ),
        )
        self._print_group(
            "메시지 템플릿 (MessageTemplate)",
            templates,
            limit,
            lambda t: (
                f"id={t.id} name={t.name!r} default={t.is_user_default} "
                f"autosend_refs={t.auto_send_configs.count()}"
            ),
        )
        self._print_group(
            "반복 복제 템플릿 (MessageTemplate copy chain)",
            recursive_templates,
            limit,
            lambda t: (
                f"id={t.id} name={t.name!r} default={t.is_user_default} "
                f"autosend_refs={t.auto_send_configs.count()}"
            ),
        )
        self._print_group(
            "직원 (Staff)",
            staffs,
            limit,
            lambda staff: (
                f"id={staff.id} name={staff.name!r} user_id={staff.user_id} "
                f"history={self._staff_history_count(staff)}"
            ),
        )
        self._print_group("시험 (Exam)", exams, limit, lambda e: f"id={e.id} type={e.exam_type} title={e.title!r}")
        self._print_group(
            "시험 제출 (Submission)",
            exam_submissions,
            limit,
            lambda submission: (
                f"id={submission.id} exam_id={submission.target_id} "
                f"source={submission.source} status={submission.status}"
            ),
        )
        self._print_group(
            "OMR 업로드 배치 (OmrUploadBatch)",
            omr_batches,
            limit,
            lambda batch: (
                f"id={batch.id} exam_id={batch.exam_id} "
                f"session_id={batch.session_id} lecture_id={batch.lecture_id}"
            ),
        )
        self._print_group(
            "OMR 업로드 항목 (OmrUploadBatchItem)",
            omr_batch_items,
            limit,
            lambda item: (
                f"id={item.id} batch_id={item.batch_id} exam_id={item.exam_id} "
                f"submission_id={item.submission_id} "
                f"duplicate_of_submission_id={item.duplicate_of_submission_id}"
            ),
        )
        self._print_group("과제 (Homework)", homeworks, limit, lambda h: f"id={h.id} title={h.title!r}")
        self._print_group(
            "강의 (Lecture)",
            lectures,
            limit,
            lambda lecture: (
                f"id={lecture.id} title={lecture.title!r} "
                f"sessions={lecture.sessions.count()}"
            ),
        )
        self._print_group(
            "고립 차시 (Session)",
            sessions,
            limit,
            lambda session: (
                f"id={session.id} lecture_id={session.lecture_id} "
                f"title={session.title!r}"
            ),
        )
        self._print_group(
            "수납 비목 (FeeTemplate)",
            fee_templates,
            limit,
            lambda f: (
                f"id={f.id} name={f.name!r} active={f.is_active} "
                f"refs={fee_template_ref_count(f)} action={fee_template_action(f)}"
            ),
        )

        target_groups = {
            "exams": exams,
            "fee_templates": fee_templates,
            "homeworks": homeworks,
            "lectures": lectures,
            "matchups": matchups,
            "omr_batch_items": omr_batch_items,
            "omr_batches": omr_batches,
            "posts": posts,
            "recursive_templates": recursive_templates,
            "sessions": sessions,
            "staffs": staffs,
            "students": students,
            "submissions": exam_submissions,
            "templates": templates,
        }
        exact_token = build_confirmation_token(
            tenant_id=tenant_id,
            target_groups=target_groups,
        )

        self.stdout.write(self.style.HTTP_INFO(f"\n=== 합계: {total}건 ==="))
        self.stdout.write(f"확인 토큰 (exact targets): {exact_token}")

        if not execute:
            self.stdout.write(self.style.WARNING(
                "--dry-run 모드 (기본값). 실제 삭제하지 않음.\n"
                "삭제하려면 동일 대상에 대해 --execute --confirm-token <위 토큰>을 사용."
            ))
            return

        if total == 0:
            self.stdout.write("삭제할 잔재 없음 — 종료.")
            return

        if confirm_token != exact_token:
            raise CommandError(
                "확인 토큰이 없거나 현재 exact target set과 일치하지 않습니다. "
                "dry-run을 다시 실행해 대상 ID를 검토하세요."
            )

        self._validate_execute_targets(
            tenant_id=tenant_id,
            exams=exams,
            exam_submissions=exam_submissions,
            students=students,
            lectures=lectures,
            matchups=matchups,
            omr_batches=omr_batches,
            omr_batch_items=omr_batch_items,
            sessions=sessions,
            staffs=staffs,
            templates=[*templates, *recursive_templates],
        )
        storage_deleted = self._delete_external_storage(
            tenant_id=tenant_id,
            posts=posts,
            matchups=matchups,
            submissions=exam_submissions,
        )

        # 외부 저장소 삭제가 모두 검증된 뒤 DB를 한 트랜잭션으로 정리한다.
        with transaction.atomic():
            from apps.domains.students.services.lifecycle import permanently_delete_students

            omr_batch_del = sum(batch.delete()[0] for batch in omr_batches)
            student_result = permanently_delete_students(
                tenant=tenant,
                student_ids=[student.id for student in students],
            )
            s_del = student_result.deleted_count
            p_del = sum(p.delete()[0] for p in posts)
            m_del = sum(m.inventory_file.delete()[0] for m in matchups)
            t_del = sum(t.delete()[0] for t in [*templates, *recursive_templates])
            staff_del = sum(staff.delete()[0] for staff in staffs)
            result_del = (
                Result.objects.filter(target_type="exam", target_id__in=exam_ids).delete()[0]
                if exam_ids else 0
            )
            submission_del = (
                Submission.objects.filter(
                    tenant_id=tenant_id,
                    target_type=Submission.TargetType.EXAM,
                    target_id__in=exam_ids,
                ).delete()[0]
                if exam_ids else 0
            )
            clinic_link_del = (
                ClinicLink.objects.filter(
                    tenant_id=tenant_id,
                    source_type="exam",
                    source_id__in=exam_ids,
                ).delete()[0]
                if exam_ids else 0
            )
            e_del = sum(e.delete()[0] for e in exams)
            h_del = sum(h.delete()[0] for h in homeworks)
            lecture_del, session_del, score_draft_del = self._delete_lecture_residue(
                lectures=lectures,
                sessions=sessions,
            )
            f_del = 0
            f_deactivated = 0
            for fee_template in fee_templates:
                if fee_template_ref_count(fee_template):
                    if fee_template.is_active or fee_template.auto_assign:
                        fee_template.is_active = False
                        fee_template.auto_assign = False
                        fee_template.save(update_fields=["is_active", "auto_assign", "updated_at"])
                        f_deactivated += 1
                else:
                    f_del += fee_template.delete()[0]

        self.stdout.write(self.style.SUCCESS(
            f"\n삭제 완료:\n"
            f"  - 학생 cascade rows: {s_del}\n"
            f"  - 게시글 cascade rows: {p_del}\n"
            f"  - 매치업/인벤토리 cascade rows: {m_del}\n"
            f"  - R2 objects verified deleted: {storage_deleted}\n"
            f"  - 템플릿 cascade rows: {t_del}\n"
            f"  - 직원 cascade rows: {staff_del}\n"
            f"  - 시험 결과 cascade rows: {result_del}\n"
            f"  - OMR 업로드 배치 cascade rows: {omr_batch_del}\n"
            f"  - 시험 제출 cascade rows: {submission_del}\n"
            f"  - 시험 클리닉 링크 cascade rows: {clinic_link_del}\n"
            f"  - 시험 cascade rows: {e_del}\n"
            f"  - 과제 cascade rows: {h_del}\n"
            f"  - 강의 cascade rows: {lecture_del}\n"
            f"  - 고립 차시 cascade rows: {session_del}\n"
            f"  - 점수 편집 draft cascade rows: {score_draft_del}\n"
            f"  - 수납 비목 cascade rows: {f_del}\n"
            f"  - 수납 비목 비활성화: {f_deactivated}"
        ))

    @staticmethod
    def _validate_execute_targets(
        *,
        tenant_id,
        exams,
        exam_submissions,
        students,
        lectures,
        matchups,
        omr_batches,
        omr_batch_items,
        sessions,
        staffs,
        templates,
    ) -> None:
        from apps.domains.submissions.models import OmrUploadBatchItem

        active_student_ids = [student.id for student in students if student.deleted_at is None]
        if active_student_ids:
            raise CommandError(
                "활성 학생은 E2E 표식만으로 영구 삭제하지 않습니다. "
                f"먼저 공식 soft-delete 수명주기를 완료하세요: ids={active_student_ids}"
            )

        referenced_template_ids = [
            template.id
            for template in templates
            if template.is_user_default or template.auto_send_configs.exists()
        ]
        if referenced_template_ids:
            raise CommandError(
                "기본/자동발송 참조 템플릿은 자동 정리하지 않습니다: "
                f"ids={referenced_template_ids}"
            )

        unsafe_staff_ids = [
            staff.id
            for staff in staffs
            if (
                staff.user_id is not None
                or bool(staff.profile_photo)
                or Command._staff_history_count(staff) > 0
            )
        ]
        if unsafe_staff_ids:
            raise CommandError(
                "계정/프로필/급여 이력이 있는 직원은 자동 정리하지 않습니다: "
                f"ids={unsafe_staff_ids}"
            )

        from academy.adapters.db.django import repositories_teachers as teacher_repo

        linked_teacher_staff_ids = [
            staff.id
            for staff in staffs
            if teacher_repo.teacher_count_tenant_name_phone(
                staff.tenant,
                staff.name,
                staff.phone or "",
            )
        ]
        if linked_teacher_staff_ids:
            raise CommandError(
                "강사 레코드가 연결된 직원은 자동 정리하지 않습니다: "
                f"ids={linked_teacher_staff_ids}"
            )

        unsafe_lecture_ids = [
            lecture.id
            for lecture in lectures
            if lecture.is_system
            or any(
                not matches_residue(title or "")
                for title in lecture.sessions.values_list("title", flat=True)
            )
        ]
        if unsafe_lecture_ids:
            raise CommandError(
                "시스템 강의 또는 strict marker가 없는 하위 차시를 포함한 강의는 "
                f"자동 정리하지 않습니다: ids={unsafe_lecture_ids}"
            )

        target_exam_ids = {exam.id for exam in exams}
        target_submission_ids = {submission.id for submission in exam_submissions}
        target_batch_ids = {batch.id for batch in omr_batches}
        target_batch_item_ids = {item.id for item in omr_batch_items}
        invalid_batch_ids = (
            [
                batch.id
                for batch in omr_batches
                if batch.tenant_id != tenant_id
                or batch.exam_id not in target_exam_ids
            ]
            if exams
            else []
        )
        invalid_batch_item_ids = [
            item.id
            for item in omr_batch_items
            if item.batch_id not in target_batch_ids
            or item.tenant_id != item.batch.tenant_id
            or item.exam_id != item.batch.exam_id
            or (
                item.submission_id is not None
                and item.submission_id not in target_submission_ids
            )
            or (
                item.duplicate_of_submission_id is not None
                and item.duplicate_of_submission_id not in target_submission_ids
            )
        ]
        external_batch_item_ids = list(
            OmrUploadBatchItem.objects.filter(
                Q(submission_id__in=target_submission_ids)
                | Q(duplicate_of_submission_id__in=target_submission_ids)
            )
            .exclude(id__in=target_batch_item_ids)
            .values_list("id", flat=True)
        )
        if invalid_batch_ids or invalid_batch_item_ids or external_batch_item_ids:
            raise CommandError(
                "E2E 시험의 OMR 배치 그래프가 exact target 밖을 참조해 정리를 "
                "거부합니다: "
                f"batch_ids={invalid_batch_ids} "
                f"item_ids={invalid_batch_item_ids} "
                f"external_item_ids={external_batch_item_ids}"
            )
        non_target_exam_ids = sorted({
            exam_id
            for lecture in lectures
            for exam_id in lecture.sessions.values_list("exams__id", flat=True)
            if exam_id is not None and exam_id not in target_exam_ids
        })
        if non_target_exam_ids:
            raise CommandError(
                "E2E 강의에 strict marker 대상이 아닌 시험이 연결되어 정리를 "
                f"거부합니다: exam_ids={non_target_exam_ids}"
            )

        if any(session.lecture_id in {lecture.id for lecture in lectures} for session in sessions):
            raise CommandError("강의 대상과 고립 차시 대상이 중복되었습니다.")

        from apps.domains.matchup.services import document_has_protected_matchup_problems

        protected_document_ids = []
        for document in matchups:
            has_authored_report = document.hit_reports.exclude(
                status="draft",
                submitted_at__isnull=True,
                summary="",
                entries__isnull=True,
            ).exists()
            if (
                document_has_protected_matchup_problems(document)
                or has_authored_report
            ):
                protected_document_ids.append(document.id)
        if protected_document_ids:
            raise CommandError(
                "수동 컷/핀 또는 작성된 보고서가 있는 매치업 문서는 자동 정리하지 않습니다: "
                f"ids={protected_document_ids}"
            )

    @staticmethod
    def _staff_history_count(staff) -> int:
        return (
            staff.work_records.count()
            + staff.expense_records.count()
            + staff.work_month_locks.count()
            + staff.payroll_snapshots.count()
        )

    @staticmethod
    def _delete_lecture_residue(*, lectures, sessions) -> tuple[int, int, int]:
        from apps.domains.results.models import ScoreEditDraft
        from apps.support.lectures.view_dependencies import (
            first_lecture_delete_blocker,
            first_session_delete_blocker,
        )

        lecture_deleted = 0
        session_deleted = 0
        score_draft_deleted = 0

        for lecture in lectures:
            blocker = first_lecture_delete_blocker(lecture)
            if blocker == "sessions with score edit drafts":
                deleted, _ = ScoreEditDraft.objects.filter(
                    session__lecture=lecture,
                ).delete()
                score_draft_deleted += deleted
                blocker = first_lecture_delete_blocker(lecture)
            if blocker:
                raise CommandError(
                    f"E2E 강의 id={lecture.id}에 보호 대상이 남아 정리를 거부합니다: {blocker}"
                )
            deleted, _ = lecture.delete()
            lecture_deleted += deleted

        for session in sessions:
            blocker = first_session_delete_blocker(
                session.__class__.objects.filter(id=session.id)
            )
            if blocker == "score edit drafts":
                deleted, _ = ScoreEditDraft.objects.filter(session=session).delete()
                score_draft_deleted += deleted
                blocker = first_session_delete_blocker(
                    session.__class__.objects.filter(id=session.id)
                )
            if blocker:
                raise CommandError(
                    f"E2E 차시 id={session.id}에 보호 대상이 남아 정리를 거부합니다: {blocker}"
                )
            deleted, _ = session.delete()
            session_deleted += deleted

        return lecture_deleted, session_deleted, score_draft_deleted

    @staticmethod
    def _delete_external_storage(*, tenant_id: int, posts, matchups, submissions) -> int:
        from apps.domains.community.models import PostAttachment
        from apps.domains.matchup.models import ProblemSegmentationProposal
        from apps.domains.submissions.models import Submission
        from apps.infrastructure.storage.r2 import (
            delete_object_r2_storage,
            head_object_r2_storage,
        )
        from apps.support.omr.scan_images import select_omr_scan_image

        def omr_scan_keys(submission) -> list[str]:
            selection = select_omr_scan_image(
                submission_meta=submission.meta,
                original_file_key=submission.file_key,
                tenant_id=None,
            )
            ai_result = (
                submission.meta.get("ai_result")
                if isinstance(submission.meta, dict)
                else None
            )
            result = (
                ai_result.get("result")
                if isinstance(ai_result, dict)
                else None
            )
            result = result if isinstance(result, dict) else {}
            return list(dict.fromkeys(filter(None, [
                selection["original_scan_image_key"],
                selection["scan_image_key"],
                str(result.get("aligned_image_key") or "").strip(),
                str(result.get("aligned_scan_image_key") or "").strip(),
            ])))

        keys: list[str] = list(
            PostAttachment.objects.filter(post__in=posts)
            .exclude(r2_key="")
            .values_list("r2_key", flat=True)
        )
        for document in matchups:
            if document.r2_key:
                keys.append(document.r2_key)
            if document.inventory_file.r2_key:
                keys.append(document.inventory_file.r2_key)
            for image_key, meta in document.problems.values_list("image_key", "meta"):
                if image_key:
                    keys.append(image_key)
                cleanup = (meta or {}).get("public_cleanup")
                public_key = cleanup.get("public_image_key") if isinstance(cleanup, dict) else ""
                if public_key:
                    keys.append(public_key)
            keys.extend(
                key
                for key in ((document.meta or {}).get("page_image_keys") or [])
                if isinstance(key, str) and key
            )
            keys.extend(
                ProblemSegmentationProposal.objects.filter(document=document)
                .exclude(image_key="")
                .values_list("image_key", flat=True)
            )

        submission_keys: dict[int, list[str]] = {}
        for submission in submissions:
            if submission.source != Submission.Source.OMR_SCAN:
                continue
            owned_keys = omr_scan_keys(submission)
            expected_prefix = f"tenants/{tenant_id}/ai/submissions/{submission.id}/"
            if any(not key.startswith(expected_prefix) for key in owned_keys):
                raise CommandError(
                    "OMR 제출의 소유 prefix 밖 파일이 포함되어 저장소 정리를 "
                    f"중단합니다: submission_id={submission.id}"
                )
            submission_keys[submission.id] = owned_keys
            keys.extend(owned_keys)

        unique_keys = list(dict.fromkeys(keys))
        wrong_tenant_keys = [
            key for key in unique_keys
            if not key.startswith(f"tenants/{tenant_id}/")
        ]
        if wrong_tenant_keys:
            raise CommandError(
                "테넌트 prefix 밖의 R2 key가 포함되어 저장소 정리를 중단합니다. "
                f"count={len(wrong_tenant_keys)}"
            )

        target_submission_ids = set(submission_keys)
        shared_submission_ids = []
        target_key_set = {
            key for owned_keys in submission_keys.values() for key in owned_keys
        }
        for submission in (
            Submission.objects.filter(
                tenant_id=tenant_id,
                source=Submission.Source.OMR_SCAN,
            )
            .exclude(id__in=target_submission_ids)
            .only("id", "tenant_id", "file_key", "meta")
        ):
            referenced_keys = set(omr_scan_keys(submission))
            if referenced_keys & target_key_set:
                shared_submission_ids.append(submission.id)
        if shared_submission_ids:
            raise CommandError(
                "다른 OMR 제출이 같은 파일을 참조해 저장소 정리를 중단합니다: "
                f"submission_ids={shared_submission_ids}"
            )

        for key in unique_keys:
            delete_object_r2_storage(key=key)
            exists, _size = head_object_r2_storage(key=key)
            if exists:
                raise CommandError("R2 삭제 readback 실패. DB 정리를 중단합니다.")
        return len(unique_keys)

    def _print_group(self, label: str, items, limit: int, fmt):
        self.stdout.write(f"\n--- {label}: {len(items)}건 ---")
        for it in items[:limit]:
            self.stdout.write(f"  {fmt(it)}")
        if len(items) > limit:
            self.stdout.write(f"  ... 외 {len(items) - limit}건")
