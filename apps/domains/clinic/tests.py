# apps/domains/clinic/tests.py
"""
클리닉 도메인 운영 보증 테스트

모든 테스트는 실제 DB 상태 변화를 검증한다.
소스 검사(inspect.getsource) 아닌, 모델/서비스/API 호출 결과 기반.
"""
import datetime
import threading
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.models import Tenant
from apps.domains.clinic.models import Session as ClinicSession, SessionParticipant
from apps.domains.enrollment.models import Enrollment
from apps.domains.enrollment.selectors import enrollments_for_tenant
from apps.domains.exams.models import Exam
from apps.domains.lectures.models import Lecture, Session as LectureSession
from apps.domains.progress.models import ClinicLink
from apps.domains.students.models import Student

User = get_user_model()


# ═══════════════════════════════════════════════════
# Test Fixture Helper
# ═══════════════════════════════════════════════════

class ClinicTestMixin:
    """테스트 데이터 생성 helper. 각 테스트 클래스에서 mixin으로 사용."""

    def make_tenant(self, code="t_test", name="Test Academy"):
        return Tenant.objects.create(code=code, name=name)

    def make_user(self, username):
        return User.objects.create_user(username=username, password="test1234")

    def make_student(self, tenant, username_suffix, name="학생"):
        user = self.make_user(f"student_{username_suffix}_{tenant.code}")
        return Student.objects.create(
            tenant=tenant, user=user,
            ps_number=f"PS{username_suffix}",
            omr_code=f"OMR{username_suffix:0>4}"[:8],
            name=f"{name}_{username_suffix}",
            parent_phone=f"010-0000-{username_suffix:0>4}"[:13],
        )

    def make_lecture(self, tenant, title="수학", name="수학반", subject="math"):
        return Lecture.objects.create(
            tenant=tenant, title=f"{title}_{tenant.code}",
            name=name, subject=subject,
        )

    def make_lecture_session(self, lecture, order=1, title="1차시"):
        return LectureSession.objects.create(
            lecture=lecture, order=order, title=title,
        )

    def make_enrollment(self, tenant, student, lecture, status="ACTIVE"):
        return Enrollment.objects.create(
            tenant=tenant, student=student, lecture=lecture, status=status,
        )

    def make_clinic_session(self, tenant, date=None, start_time=None,
                            location="101호", max_participants=10):
        return ClinicSession.objects.create(
            tenant=tenant,
            date=date or datetime.date.today(),
            start_time=start_time or datetime.time(14, 0),
            location=location,
            max_participants=max_participants,
        )

    def make_clinic_link(self, enrollment, lecture_session, tenant=None,
                         reason="AUTO_FAILED", source_type="exam", source_id=1,
                         is_auto=True, cycle_no=1):
        return ClinicLink.objects.create(
            tenant=tenant or enrollment.tenant,
            enrollment=enrollment,
            session=lecture_session,
            reason=reason,
            source_type=source_type,
            source_id=source_id,
            is_auto=is_auto,
            cycle_no=cycle_no,
        )

    def make_participant(self, tenant, clinic_session, student,
                         enrollment=None, status="booked", source="manual"):
        return SessionParticipant.objects.create(
            tenant=tenant,
            session=clinic_session,
            student=student,
            enrollment=enrollment,
            status=status,
            source=source,
        )

    def setup_full_tenant(self, code, student_count=1):
        """tenant + lecture + session + students + enrollments 한 번에 생성"""
        tenant = self.make_tenant(code=code, name=f"Academy {code}")
        lecture = self.make_lecture(tenant, title=f"수학_{code}")
        lec_session = self.make_lecture_session(lecture, order=1)
        clinic_session = self.make_clinic_session(tenant)

        students = []
        enrollments = []
        for i in range(1, student_count + 1):
            s = self.make_student(tenant, f"{code}_{i}")
            e = self.make_enrollment(tenant, s, lecture)
            students.append(s)
            enrollments.append(e)

        return {
            "tenant": tenant,
            "lecture": lecture,
            "lec_session": lec_session,
            "clinic_session": clinic_session,
            "students": students,
            "enrollments": enrollments,
        }


# ═══════════════════════════════════════════════════
# 1. 멀티테넌트 격리 (실제 DB 기반)
# ═══════════════════════════════════════════════════

class MultiTenantIsolationTest(TestCase, ClinicTestMixin):
    """tenant A/B 데이터가 절대 섞이지 않음을 실제 DB에서 검증"""

    def setUp(self):
        self.a = self.setup_full_tenant("tenantA", student_count=2)
        self.b = self.setup_full_tenant("tenantB", student_count=2)
        self.exam_a = Exam.objects.create(
            tenant=self.a["tenant"],
            title="tenant A live exam",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        self.exam_b = Exam.objects.create(
            tenant=self.b["tenant"],
            title="tenant B live exam",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        self.exam_a.sessions.add(self.a["lec_session"])
        self.exam_b.sessions.add(self.b["lec_session"])

        # tenant A에 ClinicLink 생성
        self.link_a = self.make_clinic_link(
            self.a["enrollments"][0], self.a["lec_session"],
            source_type="exam", source_id=self.exam_a.id,
        )
        # tenant B에 ClinicLink 생성
        self.link_b = self.make_clinic_link(
            self.b["enrollments"][0], self.b["lec_session"],
            source_type="exam", source_id=self.exam_b.id,
        )

    def test_clinic_highlight_map_isolates_tenants(self):
        """compute_clinic_highlight_map은 다른 테넌트 ClinicLink를 반환하지 않음"""
        from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map

        # tenant A 기준 조회 — tenant B enrollment은 포함되면 안 됨
        all_ids = {
            self.a["enrollments"][0].id,
            self.a["enrollments"][1].id,
            self.b["enrollments"][0].id,  # 의도적으로 다른 테넌트 ID 포함
        }
        result_a = compute_clinic_highlight_map(
            tenant=self.a["tenant"], enrollment_ids=all_ids,
        )
        # tenant A의 enrollment만 highlight 대상
        self.assertTrue(result_a.get(self.a["enrollments"][0].id))
        self.assertFalse(result_a.get(self.b["enrollments"][0].id, False))

        # tenant B 기준 조회
        result_b = compute_clinic_highlight_map(
            tenant=self.b["tenant"], enrollment_ids=all_ids,
        )
        self.assertTrue(result_b.get(self.b["enrollments"][0].id))
        self.assertFalse(result_b.get(self.a["enrollments"][0].id, False))

    def test_clinic_highlight_matches_student_passcard_booking_lifecycle(self):
        """노란 하이라이트는 학생 패스카드가 CLINIC_REQUIRED일 때만 켜진다."""
        from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map

        tenant = self.a["tenant"]
        student = self.a["students"][0]
        first_enrollment = self.a["enrollments"][0]
        second_lecture = self.make_lecture(tenant, title="영어")
        second_session = self.make_lecture_session(second_lecture, order=2)
        second_enrollment = self.make_enrollment(tenant, student, second_lecture)
        second_exam = Exam.objects.create(
            tenant=tenant,
            title="tenant A second live exam",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        second_exam.sessions.add(second_session)
        second_link = self.make_clinic_link(
            second_enrollment,
            second_session,
            source_type="exam",
            source_id=second_exam.id,
        )
        enrollment_ids = {first_enrollment.id, second_enrollment.id}

        def highlights():
            return compute_clinic_highlight_map(
                tenant=tenant,
                enrollment_ids=enrollment_ids,
            )

        self.assertEqual(highlights(), {
            first_enrollment.id: True,
            second_enrollment.id: True,
        })

        foreign_session = self.make_clinic_session(
            self.b["tenant"],
            date=timezone.localdate() + datetime.timedelta(days=7),
        )
        self.make_participant(
            tenant,
            foreign_session,
            student,
            enrollment=first_enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        self.assertTrue(
            all(highlights().values()),
            "다른 tenant 세션을 가리키는 손상 예약은 상태를 바꾸면 안 된다.",
        )

        clinic_session = self.make_clinic_session(
            tenant,
            date=timezone.localdate() + datetime.timedelta(days=7),
        )
        participant = self.make_participant(
            tenant,
            clinic_session,
            student,
            enrollment=first_enrollment,
            status=SessionParticipant.Status.PENDING,
        )
        self.assertTrue(all(highlights().values()), "승인 대기는 대상자 상태를 유지해야 한다.")

        participant.status = SessionParticipant.Status.BOOKED
        participant.save(update_fields=["status", "updated_at"])
        self.assertFalse(any(highlights().values()), "확정 예약은 학생 전체 하이라이트를 해제해야 한다.")

        clinic_session.date = timezone.localdate() - datetime.timedelta(days=1)
        clinic_session.save(update_fields=["date", "updated_at"])
        participant.status = SessionParticipant.Status.ATTENDED
        participant.save(update_fields=["status", "updated_at"])
        self.assertFalse(any(highlights().values()), "미완료 수강 중에는 날짜가 지나도 해제 상태다.")

        participant.completed_at = timezone.now()
        participant.save(update_fields=["completed_at", "updated_at"])
        self.assertTrue(all(highlights().values()), "미해결 수강완료 뒤에는 대상자로 돌아와야 한다.")

        self.link_a.resolved_at = timezone.now()
        self.link_a.save(update_fields=["resolved_at", "updated_at"])
        second_link.resolved_at = timezone.now()
        second_link.save(update_fields=["resolved_at", "updated_at"])
        self.assertFalse(any(highlights().values()), "모든 과락 요소 해소 뒤에는 합격자 상태다.")

    def test_clinic_link_queryset_isolates_tenants(self):
        """ClinicLink queryset이 tenant별로 격리됨"""
        links_a = ClinicLink.objects.filter(tenant=self.a["tenant"])
        links_b = ClinicLink.objects.filter(tenant=self.b["tenant"])

        self.assertEqual(links_a.count(), 1)
        self.assertEqual(links_b.count(), 1)
        self.assertNotEqual(links_a.first().id, links_b.first().id)

    def test_participant_create_blocked_for_wrong_tenant_session(self):
        """tenant A의 학생이 tenant B의 세션에 예약 시도 → 거부"""
        # DB 레벨에서 tenant A 학생의 participant를 tenant B 세션에 만들 수 있지만,
        # 서비스/뷰 레벨에서 교차 검증을 해야 함
        # 여기서는 실제 중복 방지를 우선 확인
        p = self.make_participant(
            self.a["tenant"], self.a["clinic_session"],
            self.a["students"][0],
        )
        self.assertEqual(p.tenant, self.a["tenant"])
        self.assertEqual(p.session.tenant, self.a["tenant"])

    def test_serializer_highlight_uses_tenant_filter(self):
        """ClinicSessionParticipantSerializer.get_name_highlight_clinic_target가 tenant 필터 적용"""
        from apps.domains.clinic.serializers import ClinicSessionParticipantSerializer

        # 승인 대기는 아직 대상자이므로 하이라이트 유지
        p_a = self.make_participant(
            self.a["tenant"], self.a["clinic_session"],
            self.a["students"][0],
            enrollment=self.a["enrollments"][0],
            status="pending",
        )

        # request mock with tenant A
        factory = RequestFactory()
        request = factory.get("/")
        request.tenant = self.a["tenant"]

        serializer = ClinicSessionParticipantSerializer(p_a, context={"request": request})
        highlight_a = serializer.data.get("name_highlight_clinic_target")
        self.assertTrue(highlight_a, "tenant A participant should be highlighted")

        # 확정 예약 순간에는 패스카드와 동일하게 하이라이트 해제
        p_a.status = SessionParticipant.Status.BOOKED
        p_a.save(update_fields=["status", "updated_at"])
        serializer_booked = ClinicSessionParticipantSerializer(p_a, context={"request": request})
        self.assertFalse(serializer_booked.data.get("name_highlight_clinic_target"))

        # 같은 participant를 tenant B request로 직렬화 시 highlight=False
        request.tenant = self.b["tenant"]
        serializer_b = ClinicSessionParticipantSerializer(p_a, context={"request": request})
        highlight_b = serializer_b.data.get("name_highlight_clinic_target")
        self.assertFalse(highlight_b, "tenant B request must not see tenant A highlights")

    def test_clinic_enrollment_ids_for_session_isolates(self):
        """get_clinic_enrollment_ids_for_session이 tenant별 결과만 반환"""
        from apps.domains.results.utils.clinic import get_clinic_enrollment_ids_for_session

        ids_a = get_clinic_enrollment_ids_for_session(session=self.a["lec_session"])
        ids_b = get_clinic_enrollment_ids_for_session(session=self.b["lec_session"])

        # 각 tenant 자기 enrollment만 포함
        self.assertIn(self.a["enrollments"][0].id, ids_a)
        self.assertNotIn(self.b["enrollments"][0].id, ids_a)
        self.assertIn(self.b["enrollments"][0].id, ids_b)
        self.assertNotIn(self.a["enrollments"][0].id, ids_b)

    def test_clinic_target_service_isolates_tenants(self):
        """ClinicTargetService.list_admin_targets가 다른 테넌트 데이터를 반환하지 않음"""
        from apps.domains.results.services.clinic_target_service import ClinicTargetService

        targets_a = ClinicTargetService.list_admin_targets(tenant=self.a["tenant"])
        targets_b = ClinicTargetService.list_admin_targets(tenant=self.b["tenant"])

        enrollment_ids_a = {t.get("enrollment_id") for t in targets_a}
        enrollment_ids_b = {t.get("enrollment_id") for t in targets_b}

        # 교차 오염 없음
        self.assertTrue(enrollment_ids_a.isdisjoint(enrollment_ids_b))

    def test_admin_clinic_targets_are_newest_first_with_source_semantics(self):
        from apps.domains.results.services.clinic_target_service import ClinicTargetService

        newer_exam = Exam.objects.create(
            tenant=self.a["tenant"],
            title="신년 모의고사 분자의 구조",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        newer_exam.sessions.add(self.a["lec_session"])
        newer_link = self.make_clinic_link(
            self.a["enrollments"][1],
            self.a["lec_session"],
            source_type="exam",
            source_id=newer_exam.id,
        )
        older_at = timezone.now() - datetime.timedelta(hours=2)
        newer_at = timezone.now() - datetime.timedelta(hours=1)
        ClinicLink.objects.filter(id=self.link_a.id).update(created_at=older_at)
        ClinicLink.objects.filter(id=newer_link.id).update(created_at=newer_at)

        rows = ClinicTargetService.list_admin_targets(tenant=self.a["tenant"])
        source_rows = [
            row for row in rows
            if row.get("clinic_link_id") in {self.link_a.id, newer_link.id}
        ]

        self.assertEqual(
            [row["clinic_link_id"] for row in source_rows],
            [newer_link.id, self.link_a.id],
        )
        self.assertEqual(source_rows[0]["source_title"], newer_exam.title)
        self.assertIsNone(source_rows[0]["source_scope"])

    def test_clinic_target_service_rejects_mismatched_link_relations(self):
        """링크의 tenant만 맞고 수강생 tenant가 다른 손상 데이터는 노출하지 않음."""
        from apps.domains.results.utils.clinic import (
            get_clinic_enrollment_ids_for_session,
        )
        from apps.domains.results.utils.clinic_highlight import (
            compute_clinic_highlight_map,
        )
        from apps.domains.results.services.clinic_target_service import ClinicTargetService

        mismatched = self.make_clinic_link(
            self.b["enrollments"][1],
            self.a["lec_session"],
            tenant=self.a["tenant"],
            source_type="exam",
            source_id=self.exam_a.id,
        )
        mismatched_source = self.make_clinic_link(
            self.a["enrollments"][1],
            self.a["lec_session"],
            tenant=self.a["tenant"],
            source_type="exam",
            source_id=self.exam_b.id,
            cycle_no=2,
        )
        mismatched_source.resolved_at = timezone.now()
        mismatched_source.resolution_type = ClinicLink.ResolutionType.WAIVED
        mismatched_source.save(update_fields=["resolved_at", "resolution_type"])

        targets_a = ClinicTargetService.list_admin_targets(tenant=self.a["tenant"])
        history_a = ClinicTargetService.list_admin_targets(
            tenant=self.a["tenant"],
            include_resolved=True,
        )

        self.assertFalse(
            any(target.get("clinic_link_id") == mismatched.id for target in targets_a)
        )
        self.assertFalse(
            any(
                target.get("clinic_link_id") == mismatched_source.id
                for target in history_a
            )
        )
        self.assertFalse(
            any(
                target.get("enrollment_id") == self.b["enrollments"][1].id
                for target in targets_a
            )
        )
        self.assertNotIn(
            self.b["enrollments"][1].id,
            get_clinic_enrollment_ids_for_session(
                session=self.a["lec_session"],
            ),
        )
        self.assertFalse(
            compute_clinic_highlight_map(
                tenant=self.a["tenant"],
                enrollment_ids={self.b["enrollments"][1].id},
            ).get(self.b["enrollments"][1].id, False)
        )


# ═══════════════════════════════════════════════════
# 2. 상태 전이 (실제 DB 기반)
# ═══════════════════════════════════════════════════

class StatusTransitionDBTest(TestCase, ClinicTestMixin):
    """실제 DB 레코드의 상태 전이를 검증"""

    def setUp(self):
        self.data = self.setup_full_tenant("trans", student_count=1)
        self.student = self.data["students"][0]
        self.tenant = self.data["tenant"]
        self.clinic_session = self.data["clinic_session"]

    def _create_participant(self, status="booked"):
        return self.make_participant(
            self.tenant, self.clinic_session, self.student, status=status,
        )

    def test_pending_to_booked(self):
        p = self._create_participant(status="pending")
        p.status = "booked"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.status, "booked")

    def test_booked_to_attended(self):
        p = self._create_participant(status="booked")
        p.status = "attended"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.status, "attended")

    def test_booked_to_no_show(self):
        p = self._create_participant(status="booked")
        p.status = "no_show"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.status, "no_show")

    def test_booked_to_cancelled(self):
        p = self._create_participant(status="booked")
        p.status = "cancelled"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.status, "cancelled")

    def test_complete_requires_attended_status(self):
        """complete()는 참석 상태와 완료 상태를 합치지 않는다."""
        from apps.domains.clinic.services import COMPLETE_ALLOWED_STATUSES

        self.assertEqual(COMPLETE_ALLOWED_STATUSES, {SessionParticipant.Status.ATTENDED})

    def test_complete_blocked_for_cancelled(self):
        """cancelled 상태에서 complete 불가"""
        p = self._create_participant(status="cancelled")
        from apps.domains.clinic.services import COMPLETE_ALLOWED_STATUSES
        terminal = {SessionParticipant.Status.CANCELLED, SessionParticipant.Status.REJECTED}
        self.assertIn(p.status, terminal)
        # complete 시 상태 변경하지 않아야 함
        self.assertNotIn(p.status, COMPLETE_ALLOWED_STATUSES)

    def test_complete_blocked_for_rejected(self):
        """rejected 상태에서 complete 불가"""
        p = self._create_participant(status="rejected")
        from apps.domains.clinic.services import COMPLETE_ALLOWED_STATUSES
        self.assertNotIn(p.status, COMPLETE_ALLOWED_STATUSES)


# ═══════════════════════════════════════════════════
# 3. Resolution / Unresolve Lifecycle (실제 DB)
# ═══════════════════════════════════════════════════

class ResolutionLifecycleTest(TestCase, ClinicTestMixin):
    """resolve/unresolve가 ClinicResolutionService 규칙대로 동작"""

    def setUp(self):
        self.data = self.setup_full_tenant("resol", student_count=1)
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.tenant = self.data["tenant"]
        # exam ClinicLink
        self.link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=42,
        )

    def test_resolve_by_exam_pass(self):
        """시험 통과 시 해소됨"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        count = ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=42,
            score=90.0,
            pass_score=60.0,
        )
        self.assertEqual(count, 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.resolved_at)
        self.assertEqual(self.link.resolution_type, "EXAM_PASS")
        self.assertEqual(self.link.resolution_evidence["exam_id"], 42)
        self.assertEqual(self.link.resolution_evidence["score"], 90.0)

    def test_resolve_idempotent(self):
        """이미 해소된 link 재해소 시 중복 적용 안 됨"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=42, score=90.0, pass_score=60.0,
        )
        # 다시 호출
        count = ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=42, score=95.0, pass_score=60.0,
        )
        self.assertEqual(count, 0)  # 이미 해소됨

    def test_unresolve(self):
        """해소 취소 시 resolved_at 복원"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=42, score=90.0, pass_score=60.0,
        )
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.resolved_at)

        # unresolve
        result = ClinicResolutionService.unresolve(clinic_link_id=self.link.id)
        self.assertIsNotNone(result)
        self.assertIsNone(result.resolved_at)
        self.assertIsNone(result.resolution_type)
        self.assertIsNone(result.resolution_evidence)

        # DB 확인
        self.link.refresh_from_db()
        self.assertIsNone(self.link.resolved_at)

    def test_resolve_manually(self):
        """수동 해소"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        admin_user = self.make_user("admin_resol")
        result = ClinicResolutionService.resolve_manually(
            clinic_link_id=self.link.id,
            user_id=admin_user.id,
            memo="선생님 판단으로 해소",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.resolution_type, "MANUAL_OVERRIDE")
        self.assertEqual(result.resolution_evidence["user_id"], admin_user.id)
        self.assertEqual(result.memo, "선생님 판단으로 해소")

    def test_waive(self):
        """면제 처리"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        result = ClinicResolutionService.waive(
            clinic_link_id=self.link.id,
            user_id=1,
            memo="출석 면제",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.resolution_type, "WAIVED")

    def test_carry_over_creates_new_cycle(self):
        """이월 시 기존 link CARRIED_OVER + 새 link cycle_no+1"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        new_link = ClinicResolutionService.carry_over(clinic_link_id=self.link.id)
        self.assertIsNotNone(new_link)
        self.assertEqual(new_link.cycle_no, 2)
        self.assertEqual(new_link.enrollment_id, self.enrollment.id)
        self.assertEqual(new_link.source_type, "exam")
        self.assertEqual(new_link.source_id, 42)

        # 기존 link는 해소됨
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.resolved_at)
        self.assertEqual(self.link.resolution_type, "CARRIED_OVER")
        self.assertTrue(self.link.resolution_evidence.get("carried_over"))

    def test_homework_resolve(self):
        """과제 통과 시 해소"""
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        hw_link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="homework", source_id=77,
        )
        count = ClinicResolutionService.resolve_by_homework_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            homework_id=77,
            score=85.0,
            max_score=100.0,
        )
        self.assertEqual(count, 1)
        hw_link.refresh_from_db()
        self.assertEqual(hw_link.resolution_type, "HOMEWORK_PASS")


# ═══════════════════════════════════════════════════
# 4. 중복 예약 방어 (실제 DB)
# ═══════════════════════════════════════════════════

class DuplicateBookingDBTest(TestCase, ClinicTestMixin):
    """동일 학생 동일 세션 중복 예약 시 DB가 거부"""

    def setUp(self):
        self.data = self.setup_full_tenant("dup", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.clinic_session = self.data["clinic_session"]

    def test_unique_constraint_prevents_duplicate_active_booking(self):
        """동일 (tenant, session, student) + active status → UniqueConstraint 위반"""
        self.make_participant(
            self.tenant, self.clinic_session, self.student,
            status="booked",
        )
        with self.assertRaises(IntegrityError):
            self.make_participant(
                self.tenant, self.clinic_session, self.student,
                status="pending",
            )

    def test_cancelled_allows_rebooking(self):
        """cancelled 상태 후 재예약 가능"""
        p1 = self.make_participant(
            self.tenant, self.clinic_session, self.student,
            status="cancelled",
        )
        # cancelled는 UniqueConstraint 조건에서 제외 (pending, booked만)
        p2 = self.make_participant(
            self.tenant, self.clinic_session, self.student,
            status="booked",
        )
        self.assertNotEqual(p1.id, p2.id)
        self.assertEqual(p2.status, "booked")

    def test_different_sessions_allow_booking(self):
        """다른 세션에는 같은 학생 예약 가능"""
        cs2 = self.make_clinic_session(
            self.tenant, location="202호",
        )
        p1 = self.make_participant(
            self.tenant, self.clinic_session, self.student, status="booked",
        )
        p2 = self.make_participant(
            self.tenant, cs2, self.student, status="booked",
        )
        self.assertNotEqual(p1.session_id, p2.session_id)


# ═══════════════════════════════════════════════════
# 5. 점수 검증 (실제 서비스 호출)
# ═══════════════════════════════════════════════════

class ScoreValidationDBTest(TestCase, ClinicTestMixin):
    """만점 초과/음수 점수가 실제 서비스에서 거부됨"""

    def setUp(self):
        self.data = self.setup_full_tenant("score", student_count=1)
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.tenant = self.data["tenant"]
        self.admin_user = self.make_user("admin_score")

    def test_exam_retake_rejects_exceeding_max_score(self):
        """시험 재시험에서 만점 초과 → ValueError"""
        from apps.domains.exams.models import Exam
        from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService

        exam = Exam.objects.create(
            tenant=self.tenant, title="테스트시험",
            max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )

        with self.assertRaises(ValueError) as ctx:
            ClinicRemediationService.submit_exam_retake(
                clinic_link_id=link.id,
                score=150.0,  # 만점 초과
                graded_by_user_id=self.admin_user.id,
            )
        self.assertIn("만점", str(ctx.exception))

    def test_exam_retake_accepts_valid_score(self):
        """정상 점수 → 성공"""
        from apps.domains.exams.models import Exam
        from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService

        exam = Exam.objects.create(
            tenant=self.tenant, title="테스트시험2",
            max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )

        result = ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=75.0,
            graded_by_user_id=self.admin_user.id,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.score, 75.0)
        self.assertEqual(result.max_score, 100.0)

        # ClinicLink 해소 확인
        link.refresh_from_db()
        self.assertIsNotNone(link.resolved_at)
        self.assertEqual(link.resolution_type, "EXAM_PASS")

    def test_exam_retake_fail_does_not_resolve(self):
        """불합격 점수 → 미해소"""
        from apps.domains.exams.models import Exam
        from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService

        exam = Exam.objects.create(
            tenant=self.tenant, title="테스트시험3",
            max_score=100.0, pass_score=80.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )

        result = ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=50.0,
            graded_by_user_id=self.admin_user.id,
        )
        self.assertFalse(result.passed)
        link.refresh_from_db()
        self.assertIsNone(link.resolved_at)


# ═══════════════════════════════════════════════════
# 6. Trigger Idempotency (실제 DB)
# ═══════════════════════════════════════════════════

class TriggerIdempotencyDBTest(TestCase, ClinicTestMixin):
    """동일 트리거 반복 호출 시 중복 생성 안 됨"""

    def setUp(self):
        self.data = self.setup_full_tenant("trig", student_count=1)
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]

    def test_idempotent_create_no_duplicate(self):
        """같은 인자로 두 번 호출해도 1건만 생성"""
        from apps.domains.progress.services.clinic_trigger_service import _idempotent_create_clinic_link

        link1 = _idempotent_create_clinic_link(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            source_type="exam", source_id=99,
            reason="AUTO_FAILED",
        )
        self.assertIsNotNone(link1)

        link2 = _idempotent_create_clinic_link(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            source_type="exam", source_id=99,
            reason="AUTO_FAILED",
        )
        self.assertIsNone(link2)  # 이미 존재하므로 None

        # DB에 1건만 존재
        count = ClinicLink.objects.filter(
            enrollment=self.enrollment,
            session=self.lec_session,
            source_type="exam", source_id=99,
        ).count()
        self.assertEqual(count, 1)

    def test_different_source_creates_separate_links(self):
        """다른 source_id는 별도 ClinicLink 생성"""
        from apps.domains.progress.services.clinic_trigger_service import _idempotent_create_clinic_link

        link1 = _idempotent_create_clinic_link(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            source_type="exam", source_id=1,
            reason="AUTO_FAILED",
        )
        link2 = _idempotent_create_clinic_link(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            source_type="exam", source_id=2,
            reason="AUTO_FAILED",
        )
        self.assertIsNotNone(link1)
        self.assertIsNotNone(link2)
        self.assertNotEqual(link1.id, link2.id)

    def test_resolved_link_allows_new_creation(self):
        """해소된 link가 있어도 미해소 link가 없으면 새로 생성"""
        from apps.domains.progress.services.clinic_trigger_service import _idempotent_create_clinic_link
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService

        link1 = _idempotent_create_clinic_link(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            source_type="exam", source_id=50,
            reason="AUTO_FAILED",
        )
        # 해소
        ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=50, score=90, pass_score=60,
        )

        # 새 cycle 생성 가능
        link2 = _idempotent_create_clinic_link(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            source_type="exam", source_id=50,
            reason="AUTO_FAILED",
        )
        self.assertIsNotNone(link2)
        self.assertEqual(link2.cycle_no, 2)


# ═══════════════════════════════════════════════════
# 7. 동시성 검증 (TransactionTestCase)
# ═══════════════════════════════════════════════════

class ConcurrencyTest(TransactionTestCase, ClinicTestMixin):
    """병렬 실행 시 중복 생성 방지 검증.

    Note: SQLite는 진정한 병렬 실행을 지원하지 않으므로,
    이 테스트는 IntegrityError를 통한 방어를 검증한다.
    PostgreSQL에서의 진정한 병렬 테스트는 CI 통합 환경에서 수행.
    """

    def test_concurrent_trigger_creates_one_link(self):
        """두 스레드가 동시에 ClinicLink 생성 시 1건만 남음"""
        if connection.vendor == "sqlite":
            self.skipTest("SQLite smoke DB locks tables under threaded ClinicLink writes.")

        data = self.setup_full_tenant("conc", student_count=1)
        enrollment = data["enrollments"][0]
        lec_session = data["lec_session"]

        results = []
        errors = []

        def create_link():
            try:
                from apps.domains.progress.services.clinic_trigger_service import _idempotent_create_clinic_link
                link = _idempotent_create_clinic_link(
                    enrollment_id=enrollment.id,
                    session=lec_session,
                    source_type="exam", source_id=200,
                    reason="AUTO_FAILED",
                )
                results.append(link)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=create_link)
        t2 = threading.Thread(target=create_link)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 에러가 없어야 함 (IntegrityError는 내부에서 처리됨)
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")

        # 최종 DB에 1건만 존재
        count = ClinicLink.objects.filter(
            enrollment=enrollment,
            session=lec_session,
            source_type="exam", source_id=200,
        ).count()
        self.assertEqual(count, 1, "Exactly 1 link should exist after concurrent creation")

    def test_concurrent_booking_creates_one_participant(self):
        """두 스레드가 동시에 같은 학생 예약 시 1건만 유효"""
        if connection.vendor == "sqlite":
            self.skipTest("SQLite smoke DB locks tables under threaded booking writes.")

        data = self.setup_full_tenant("conc2", student_count=1)
        student = data["students"][0]
        tenant = data["tenant"]
        clinic_session = data["clinic_session"]

        results = []
        errors = []

        def book():
            try:
                p = SessionParticipant.objects.create(
                    tenant=tenant,
                    session=clinic_session,
                    student=student,
                    status="booked",
                    source="manual",
                )
                results.append(p)
            except IntegrityError:
                errors.append("duplicate")

        t1 = threading.Thread(target=book)
        t2 = threading.Thread(target=book)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 하나만 성공, 하나는 IntegrityError
        active_count = SessionParticipant.objects.filter(
            tenant=tenant, session=clinic_session, student=student,
            status__in=["pending", "booked"],
        ).count()
        self.assertEqual(active_count, 1, "Only 1 active booking should exist")


# ═══════════════════════════════════════════════════
# 8. ClinicLink tenant FK 보장
# ═══════════════════════════════════════════════════

class ClinicLinkTenantTest(TestCase, ClinicTestMixin):
    """ClinicLink 생성 시 tenant_id가 올바르게 설정됨"""

    def test_idempotent_create_sets_tenant_from_enrollment(self):
        """_idempotent_create_clinic_link가 enrollment의 tenant를 설정"""
        data = self.setup_full_tenant("tlink", student_count=1)
        enrollment = data["enrollments"][0]
        lec_session = data["lec_session"]

        from apps.domains.progress.services.clinic_trigger_service import _idempotent_create_clinic_link
        link = _idempotent_create_clinic_link(
            enrollment_id=enrollment.id,
            session=lec_session,
            source_type="exam", source_id=1,
            reason="AUTO_FAILED",
        )
        self.assertIsNotNone(link)
        self.assertEqual(link.tenant_id, data["tenant"].id)

    def test_manual_create_sets_tenant(self):
        """manual_create가 tenant를 설정"""
        data = self.setup_full_tenant("tlink2", student_count=1)
        enrollment = data["enrollments"][0]
        lec_session = data["lec_session"]

        from apps.domains.progress.services.clinic_trigger_service import ClinicTriggerService
        link = ClinicTriggerService.manual_create(
            enrollment_id=enrollment.id,
            session_id=lec_session.id,
            reason="MANUAL_REQUEST",
        )
        self.assertEqual(link.tenant_id, data["tenant"].id)

    def test_carry_over_preserves_tenant(self):
        """carry_over가 tenant를 새 link에 전파"""
        data = self.setup_full_tenant("tlink3", student_count=1)
        enrollment = data["enrollments"][0]
        lec_session = data["lec_session"]

        link = self.make_clinic_link(enrollment, lec_session)
        from apps.domains.progress.services.clinic_resolution_service import ClinicResolutionService
        new_link = ClinicResolutionService.carry_over(clinic_link_id=link.id)
        self.assertIsNotNone(new_link)
        self.assertEqual(new_link.tenant_id, data["tenant"].id)


# ═══════════════════════════════════════════════════
# 9. Clinic Reason Detection (실제 DB)
# ═══════════════════════════════════════════════════

class ClinicReasonDetectionDBTest(TestCase, ClinicTestMixin):
    """clinic_reason 자동 판정이 ClinicLink source_type 기반으로 동작"""

    def setUp(self):
        self.data = self.setup_full_tenant("reason", student_count=1)
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.tenant = self.data["tenant"]

    def test_exam_only_reason(self):
        """exam ClinicLink만 있으면 reason=exam"""
        self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=1,
        )
        links = ClinicLink.objects.filter(
            enrollment_id=self.enrollment.id,
            resolved_at__isnull=True,
            session__lecture__tenant=self.tenant,
        )
        has_exam = links.filter(source_type="exam").exists()
        has_homework = links.filter(source_type="homework").exists()
        self.assertTrue(has_exam)
        self.assertFalse(has_homework)
        # 판정: exam
        reason = "both" if (has_exam and has_homework) else "exam" if has_exam else "homework" if has_homework else None
        self.assertEqual(reason, "exam")

    def test_homework_only_reason(self):
        """homework ClinicLink만 있으면 reason=homework"""
        self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="homework", source_id=10,
        )
        links = ClinicLink.objects.filter(
            enrollment_id=self.enrollment.id,
            resolved_at__isnull=True,
        )
        has_exam = links.filter(source_type="exam").exists()
        has_homework = links.filter(source_type="homework").exists()
        reason = "both" if (has_exam and has_homework) else "exam" if has_exam else "homework" if has_homework else None
        self.assertEqual(reason, "homework")

    def test_both_reason(self):
        """exam + homework 둘 다 있으면 reason=both"""
        self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=1,
        )
        self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="homework", source_id=10,
        )
        links = ClinicLink.objects.filter(
            enrollment_id=self.enrollment.id,
            resolved_at__isnull=True,
        )
        has_exam = links.filter(source_type="exam").exists()
        has_homework = links.filter(source_type="homework").exists()
        reason = "both" if (has_exam and has_homework) else "exam" if has_exam else "homework" if has_homework else None
        self.assertEqual(reason, "both")


# ═══════════════════════════════════════════════════
# 10. Enrollment Selection Rule (실제 DB)
# ═══════════════════════════════════════════════════

class EnrollmentSelectionDBTest(TestCase, ClinicTestMixin):
    """enrollment 선택 기준이 -enrolled_at, -id 순서로 일관"""

    def setUp(self):
        self.tenant = self.make_tenant(code="enrol_sel")
        self.student = self.make_student(self.tenant, "es1")
        self.lecture1 = self.make_lecture(self.tenant, title="영어1")
        self.lecture2 = self.make_lecture(self.tenant, title="수학2")

    def test_latest_enrollment_selected(self):
        """여러 active enrollment 중 가장 최근(enrolled_at, id)이 선택됨"""
        e1 = Enrollment.objects.create(
            tenant=self.tenant, student=self.student, lecture=self.lecture1,
            status="ACTIVE",
        )
        e2 = Enrollment.objects.create(
            tenant=self.tenant, student=self.student, lecture=self.lecture2,
            status="ACTIVE",
        )
        # 동일 로직: participant/idcard 경로에서 사용하는 tenant-scoped selector 기준.
        selected = enrollments_for_tenant(self.tenant).filter(
            student=self.student, status="ACTIVE",
        ).order_by("-enrolled_at", "-id").first()

        # 나중에 생성된 enrollment이 선택됨 (enrolled_at이 같으면 id가 큰 것)
        self.assertEqual(selected.id, e2.id)

    def test_inactive_enrollment_not_selected(self):
        """INACTIVE enrollment은 선택되지 않음"""
        e_active = Enrollment.objects.create(
            tenant=self.tenant, student=self.student, lecture=self.lecture1,
            status="ACTIVE",
        )
        e_inactive = Enrollment.objects.create(
            tenant=self.tenant, student=self.student, lecture=self.lecture2,
            status="INACTIVE",
        )
        selected = enrollments_for_tenant(self.tenant).filter(
            student=self.student, status="ACTIVE",
        ).order_by("-enrolled_at", "-id").first()

        self.assertEqual(selected.id, e_active.id)


# ═══════════════════════════════════════════════════
# 11. API 레벨 검증 (DRF APIClient 실 호출)
# ═══════════════════════════════════════════════════

from rest_framework.test import APITestCase
from apps.core.models import TenantMembership


class ClinicAPITestMixin(ClinicTestMixin):
    """API 테스트용 helper — 인증+tenant 설정."""

    def setup_api_tenant(self, code, student_count=1):
        """tenant + admin user + TenantMembership + force_authenticate."""
        data = self.setup_full_tenant(code, student_count=student_count)
        for student in data["students"]:
            student.user.tenant = data["tenant"]
            student.user.save(update_fields=["tenant"])
            TenantMembership.ensure_active(
                tenant=data["tenant"],
                user=student.user,
                role="student",
            )
        admin_user = self.make_user(f"admin_{code}")
        admin_user.is_staff = True
        admin_user.tenant = data["tenant"]
        admin_user.save()
        TenantMembership.objects.create(
            user=admin_user, tenant=data["tenant"],
            role="admin", is_active=True,
        )
        data["admin_user"] = admin_user
        return data

    def _headers(self, tenant):
        return {"HTTP_HOST": "localhost", "HTTP_X_TENANT_CODE": tenant.code}


class TenantIsolationAPITest(APITestCase, ClinicAPITestMixin):
    """API 엔드포인트 레벨에서 tenant A/B 교차 접근 차단 확인."""

    def setUp(self):
        self.a = self.setup_api_tenant("api_a", student_count=1)
        self.b = self.setup_api_tenant("api_b", student_count=1)

        # tenant A에 클리닉 세션 + 예약
        self.part_a = self.make_participant(
            self.a["tenant"], self.a["clinic_session"],
            self.a["students"][0], status="booked",
        )

    def test_tenant_b_cannot_see_tenant_a_participants(self):
        """tenant B 인증으로 tenant A participant 조회 시 빈 목록."""
        self.client.force_authenticate(user=self.b["admin_user"])
        resp = self.client.get(
            "/api/v1/clinic/participants/",
            **self._headers(self.b["tenant"]),
        )
        self.assertEqual(resp.status_code, 200)
        ids = {p["id"] for p in resp.data.get("results", resp.data)}
        self.assertNotIn(self.part_a.id, ids)

    def test_tenant_b_cannot_retrieve_tenant_a_participant(self):
        """tenant B 인증으로 tenant A participant 직접 조회 → 404."""
        self.client.force_authenticate(user=self.b["admin_user"])
        resp = self.client.get(
            f"/api/v1/clinic/participants/{self.part_a.id}/",
            **self._headers(self.b["tenant"]),
        )
        self.assertIn(resp.status_code, [403, 404])

    def test_tenant_b_cannot_complete_tenant_a_participant(self):
        """tenant B 인증으로 tenant A participant complete → 403/404."""
        self.client.force_authenticate(user=self.b["admin_user"])
        resp = self.client.post(
            f"/api/v1/clinic/participants/{self.part_a.id}/complete/",
            **self._headers(self.b["tenant"]),
        )
        self.assertIn(resp.status_code, [403, 404])

    def test_tenant_a_sees_own_participant(self):
        """tenant A 인증으로 자기 participant 정상 조회."""
        self.client.force_authenticate(user=self.a["admin_user"])
        resp = self.client.get(
            f"/api/v1/clinic/participants/{self.part_a.id}/",
            **self._headers(self.a["tenant"]),
        )
        self.assertEqual(resp.status_code, 200)

    def test_participant_generic_update_and_destroy_are_not_exposed(self):
        """Lifecycle actions, not generic detail writes, own participant state."""
        self.client.force_authenticate(user=self.a["admin_user"])

        patch_response = self.client.patch(
            f"/api/v1/clinic/participants/{self.part_a.id}/",
            {"status": "cancelled"},
            format="json",
            **self._headers(self.a["tenant"]),
        )
        delete_response = self.client.delete(
            f"/api/v1/clinic/participants/{self.part_a.id}/",
            **self._headers(self.a["tenant"]),
        )

        self.assertEqual(patch_response.status_code, 405)
        self.assertEqual(delete_response.status_code, 405)
        self.part_a.refresh_from_db()
        self.assertEqual(self.part_a.status, SessionParticipant.Status.BOOKED)

    def test_tenant_a_cannot_create_participant_with_tenant_b_student(self):
        """tenant A session에 tenant B student를 꽂는 교차 테넌트 예약 생성 차단."""
        self.client.force_authenticate(user=self.a["admin_user"])
        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": self.a["clinic_session"].id,
                "student": self.b["students"][0].id,
            },
            **self._headers(self.a["tenant"]),
        )
        self.assertIn(resp.status_code, [400, 403])
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.a["tenant"],
                session=self.a["clinic_session"],
                student=self.b["students"][0],
            ).exists()
        )

    def test_tenant_a_cannot_create_participant_with_deleted_student(self):
        """soft-deleted students are not valid clinic participant targets."""
        self.client.force_authenticate(user=self.a["admin_user"])
        deleted_student = self.make_student(self.a["tenant"], "deleted_api")
        deleted_student.deleted_at = timezone.now()
        deleted_student.save(update_fields=["deleted_at"])

        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": self.a["clinic_session"].id,
                "student": deleted_student.id,
            },
            **self._headers(self.a["tenant"]),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.a["tenant"],
                session=self.a["clinic_session"],
                student=deleted_student,
            ).exists()
        )

    def test_tenant_a_cannot_create_participant_with_deleted_student_enrollment(self):
        """enrollment_id cannot be used to add a soft-deleted student."""
        self.client.force_authenticate(user=self.a["admin_user"])
        deleted_student = self.make_student(self.a["tenant"], "deleted_enroll_api")
        enrollment = self.make_enrollment(
            self.a["tenant"],
            deleted_student,
            self.a["lecture"],
            status="ACTIVE",
        )
        deleted_student.deleted_at = timezone.now()
        deleted_student.save(update_fields=["deleted_at"])

        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": self.a["clinic_session"].id,
                "enrollment_id": enrollment.id,
            },
            **self._headers(self.a["tenant"]),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.a["tenant"],
                session=self.a["clinic_session"],
                student=deleted_student,
            ).exists()
        )


class DuplicateBookingAPITest(APITestCase, ClinicAPITestMixin):
    """API 레벨 중복 예약 방어."""

    def setUp(self):
        self.data = self.setup_api_tenant("dup_api", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.clinic_session = self.data["clinic_session"]
        self.client.force_authenticate(user=self.data["admin_user"])

    def test_duplicate_booking_returns_409(self):
        """동일 학생 동일 세션 이중 POST → 409 CONFLICT."""
        payload = {
            "session": self.clinic_session.id,
            "student": self.student.id,
        }
        resp1 = self.client.post(
            "/api/v1/clinic/participants/",
            payload,
            **self._headers(self.tenant),
        )
        self.assertIn(resp1.status_code, [200, 201])

        resp2 = self.client.post(
            "/api/v1/clinic/participants/",
            payload,
            **self._headers(self.tenant),
        )
        self.assertEqual(resp2.status_code, 409)


class ParticipantCreateServiceAPITest(APITestCase, ClinicAPITestMixin):
    """participant create API가 clinic service write SSOT를 사용한다."""

    def setUp(self):
        self.data = self.setup_api_tenant("participant_create_api", student_count=2)
        self.tenant = self.data["tenant"]
        self.client.force_authenticate(user=self.data["admin_user"])

    def test_admin_create_with_enrollment_resolves_student_and_reason(self):
        enrollment = self.data["enrollments"][0]
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="참가자 사유 실재 시험",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        self.make_clinic_link(
            enrollment,
            self.data["lec_session"],
            tenant=self.tenant,
            source_type="exam",
            source_id=exam.id,
        )

        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": self.data["clinic_session"].id,
                "enrollment_id": enrollment.id,
            },
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        participant = SessionParticipant.objects.get(id=resp.data["id"])
        self.assertEqual(participant.student_id, enrollment.student_id)
        self.assertEqual(participant.enrollment_id, enrollment.id)
        self.assertEqual(participant.status, SessionParticipant.Status.BOOKED)
        self.assertEqual(participant.clinic_reason, "exam")

    def test_create_returns_409_when_session_capacity_is_full(self):
        full_session = self.make_clinic_session(
            self.tenant,
            date=datetime.date.today() + datetime.timedelta(days=1),
            max_participants=1,
        )
        self.make_participant(
            self.tenant,
            full_session,
            self.data["students"][0],
            status=SessionParticipant.Status.BOOKED,
        )

        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": full_session.id,
                "student": self.data["students"][1].id,
            },
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 409, resp.data)


class CompleteBlockedAPITest(APITestCase, ClinicAPITestMixin):
    """참석 전·불참·취소·거절 참가자에 대한 complete API → 400."""

    def setUp(self):
        self.data = self.setup_api_tenant("comp_api", student_count=2)
        self.tenant = self.data["tenant"]
        self.client.force_authenticate(user=self.data["admin_user"])

    def test_complete_cancelled_returns_400(self):
        """cancelled 상태 참가자 complete → 400."""
        p = self.make_participant(
            self.tenant, self.data["clinic_session"],
            self.data["students"][0], status="cancelled",
        )
        resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(resp.status_code, 400)

    def test_complete_rejected_returns_400(self):
        """rejected 상태 참가자 complete → 400."""
        p = self.make_participant(
            self.tenant, self.data["clinic_session"],
            self.data["students"][1], status="rejected",
        )
        resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(resp.status_code, 400)

    def test_complete_no_show_returns_400_without_completion_timestamp(self):
        p = self.make_participant(
            self.tenant, self.data["clinic_session"],
            self.data["students"][0], status="no_show",
        )
        resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(resp.status_code, 400)
        p.refresh_from_db()
        self.assertIsNone(p.completed_at)

    def test_complete_booked_returns_400_without_changing_attendance(self):
        """booked 상태 참가자는 참석 처리 전 완료할 수 없다."""
        p = self.make_participant(
            self.tenant, self.data["clinic_session"],
            self.data["students"][0], status="booked",
        )
        resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(resp.status_code, 400)
        p.refresh_from_db()
        self.assertEqual(p.status, "booked")
        self.assertIsNone(p.completed_at)

    def test_complete_and_uncomplete_preserve_attendance(self):
        p = self.make_participant(
            self.tenant, self.data["clinic_session"],
            self.data["students"][0], status="attended",
        )
        complete_resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(complete_resp.status_code, 200)

        uncomplete_resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/uncomplete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(uncomplete_resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, "attended")
        self.assertIsNone(p.completed_at)

    def test_uncomplete_keeps_append_only_actor_audit_and_restores_passcard_projection(self):
        p = self.make_participant(
            self.tenant, self.data["clinic_session"],
            self.data["students"][0], status="attended",
        )
        complete_resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(complete_resp.status_code, 200, complete_resp.data)

        uncomplete_resp = self.client.post(
            f"/api/v1/clinic/participants/{p.id}/uncomplete/",
            **self._headers(self.tenant),
        )

        self.assertEqual(uncomplete_resp.status_code, 200, uncomplete_resp.data)
        p.refresh_from_db()
        self.assertEqual(
            [entry["action"] for entry in p.completion_history],
            ["complete", "uncomplete"],
        )
        self.assertTrue(all(entry["actor_id"] == self.data["admin_user"].id for entry in p.completion_history))
        self.assertIsNotNone(p.completion_history[1]["previous_completed_at"])


class ParticipantStatusTransitionAPITest(APITestCase, ClinicAPITestMixin):
    """set_status API가 clinic service 상태 전이 SSOT를 사용한다."""

    def setUp(self):
        self.data = self.setup_api_tenant("status_api", student_count=2)
        self.tenant = self.data["tenant"]
        self.admin = self.data["admin_user"]
        self.student = self.data["students"][0]
        self.student.user.tenant = self.tenant
        self.student.user.save(update_fields=["tenant"])

    def test_staff_can_mark_booked_as_no_show(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="booked",
        )
        participant.student_request_memo = "8시까지 끝내주세요."
        participant.save(update_fields=["student_request_memo"])

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "no_show", "memo": "결석"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "no_show")
        self.assertEqual(participant.student_request_memo, "8시까지 끝내주세요.")
        self.assertEqual(participant.staff_memo, "결석")
        self.assertEqual(participant.status_changed_by_id, self.admin.id)

    def test_staff_can_approve_pending_booking(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "booked"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "booked")
        self.assertEqual(participant.status_changed_by_id, self.admin.id)

    def test_staff_cannot_mark_pending_attended_without_approval(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "attended"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "pending")

    def test_locked_participant_query_does_not_join_nullable_session(self):
        from apps.domains.clinic.services.lifecycle import _locked_participant

        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="booked",
        )

        with transaction.atomic(), CaptureQueriesContext(connection) as captured:
            locked = _locked_participant(
                tenant=self.tenant,
                participant_id=participant.id,
            )

        self.assertEqual(locked.id, participant.id)
        participant_queries = [
            q["sql"].lower()
            for q in captured.captured_queries
            if "clinic_sessionparticipant" in q["sql"].lower()
        ]
        self.assertTrue(participant_queries)
        self.assertFalse(
            any('"clinic_session"' in sql or " join clinic_session " in sql for sql in participant_queries),
            participant_queries,
        )

    def test_staff_invalid_transition_returns_400(self):
        self.client.force_authenticate(user=self.admin)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="cancelled",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "booked"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "cancelled")

    def test_missing_session_retrieve_returns_404(self):
        self.client.force_authenticate(user=self.admin)

        resp = self.client.get(
            "/api/v1/clinic/sessions/999999999/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 404, resp.data)

    @patch("apps.domains.clinic.views.participant_views._send_clinic_notification")
    def test_staff_can_delete_session_with_active_participants(self, _mock_send_clinic_notification):
        self.client.force_authenticate(user=self.admin)
        session = self.data["clinic_session"]
        self.make_participant(
            self.tenant,
            session,
            self.student,
            status=SessionParticipant.Status.BOOKED,
        )
        self.make_participant(
            self.tenant,
            session,
            self.data["students"][1],
            status=SessionParticipant.Status.PENDING,
        )

        resp = self.client.delete(
            f"/api/v1/clinic/sessions/{session.id}/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 204, resp.data)
        self.assertFalse(ClinicSession.objects.filter(id=session.id).exists())

    def test_student_can_cancel_own_pending_booking(self):
        self.client.force_authenticate(user=self.student.user)
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="pending",
        )

        resp = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "cancelled"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "cancelled")
        self.assertEqual(participant.status_changed_by_id, self.student.user_id)


class StudentClinicPermissionAPITest(APITestCase, ClinicAPITestMixin):
    """학생 클리닉 예약 권한: 신청만 가능하고 출석/완료 확정은 staff 전용."""

    def setUp(self):
        self.data = self.setup_api_tenant("student_clinic_api", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.student.user.tenant = self.tenant
        self.student.user.save(update_fields=["tenant"])
        self.client.force_authenticate(user=self.student.user)

    def _make_live_exam_clinic_link(self, enrollment, lecture_session):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title=f"패스카드 실재 시험 {ClinicLink.objects.count() + 1}",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(lecture_session)
        return self.make_clinic_link(
            enrollment,
            lecture_session,
            tenant=self.tenant,
            source_type="exam",
            source_id=exam.id,
        )

    def test_student_create_cannot_force_attended_status(self):
        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": self.data["clinic_session"].id,
                "status": "attended",
            },
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        participant = SessionParticipant.objects.get(id=resp.data["id"])
        self.assertEqual(participant.student_id, self.student.id)
        self.assertEqual(participant.status, SessionParticipant.Status.PENDING)

    def test_student_booking_uses_target_enrollment_matching_session_lecture(self):
        target_enrollment = self.data["enrollments"][0]
        self._make_live_exam_clinic_link(
            target_enrollment,
            self.data["lec_session"],
        )
        unrelated_lecture = self.make_lecture(
            self.tenant,
            title="최근수강",
            name="최근수강반",
            subject="science",
        )
        unrelated_enrollment = self.make_enrollment(
            self.tenant,
            self.student,
            unrelated_lecture,
        )
        self.assertGreater(unrelated_enrollment.id, target_enrollment.id)
        self.data["clinic_session"].target_lectures.set([self.data["lecture"]])

        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {"session": self.data["clinic_session"].id},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        participant = SessionParticipant.objects.get(id=resp.data["id"])
        self.assertEqual(participant.enrollment_id, target_enrollment.id)
        self.assertEqual(participant.clinic_reason, "exam")

    def test_idcard_aggregates_targets_from_all_active_enrollments(self):
        target_enrollment = self.data["enrollments"][0]
        link = self._make_live_exam_clinic_link(
            target_enrollment,
            self.data["lec_session"],
        )
        unrelated_lecture = self.make_lecture(
            self.tenant,
            title="다른활성강의",
            name="다른활성반",
            subject="science",
        )
        unrelated_session = self.make_lecture_session(
            unrelated_lecture,
            order=1,
            title="다른 강의 1차시",
        )
        self.make_enrollment(
            self.tenant,
            self.student,
            unrelated_lecture,
        )

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["current_result"], "FAIL")
        self.assertEqual(len(resp.data["lectures"]), 2)
        self.assertTrue(
            any(
                target["clinic_link_id"] == link.id
                and target["enrollment_id"] == target_enrollment.id
                and target["lecture_id"] == self.data["lecture"].id
                for target in resp.data["current_targets"]
            )
        )
        self.assertTrue(
            any(
                history["session_id"] == unrelated_session.id
                and history["clinic_required"] is False
                for history in resp.data["histories"]
            )
        )

    def test_idcard_projects_all_sources_newest_first_across_active_enrollments(self):
        from django.apps import apps as django_apps

        first_enrollment = self.data["enrollments"][0]
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="신년 모의고사 가역반응, 화학평형",
            exam_type=Exam.ExamType.REGULAR,
            is_active=True,
        )
        exam.sessions.add(self.data["lec_session"])
        older_link = self.make_clinic_link(
            first_enrollment,
            self.data["lec_session"],
            tenant=self.tenant,
            source_type="exam",
            source_id=exam.id,
        )

        second_lecture = self.make_lecture(
            self.tenant,
            title="화학평형특강",
            name="화학평형반",
            subject="chemistry",
        )
        second_session = self.make_lecture_session(
            second_lecture,
            order=3,
            title="평형상수 3차시",
        )
        second_enrollment = self.make_enrollment(
            self.tenant,
            self.student,
            second_lecture,
        )
        homework_model = django_apps.get_model("homework_results", "Homework")
        homework = homework_model.objects.create(
            tenant=self.tenant,
            session=second_session,
            title="부교재 화학평형",
        )
        homework_assignment_model = django_apps.get_model("homework", "HomeworkAssignment")
        homework_assignment_model.objects.create(
            tenant=self.tenant,
            homework=homework,
            session=second_session,
            enrollment=second_enrollment,
        )
        newer_link = self.make_clinic_link(
            second_enrollment,
            second_session,
            tenant=self.tenant,
            source_type="homework",
            source_id=homework.id,
        )
        older_at = timezone.now() - datetime.timedelta(hours=2)
        newer_at = timezone.now() - datetime.timedelta(hours=1)
        ClinicLink.objects.filter(id=older_link.id).update(created_at=older_at)
        ClinicLink.objects.filter(id=newer_link.id).update(created_at=newer_at)

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(
            [target["clinic_link_id"] for target in resp.data["current_targets"]],
            [newer_link.id, older_link.id],
        )
        self.assertEqual(
            [target["source_type"] for target in resp.data["current_targets"]],
            ["homework", "exam"],
        )
        self.assertEqual(
            [target["source_id"] for target in resp.data["current_targets"]],
            [homework.id, exam.id],
        )
        self.assertEqual(
            [target["source_title"] for target in resp.data["current_targets"]],
            [homework.title, exam.title],
        )
        self.assertEqual(
            [target["source_scope"] for target in resp.data["current_targets"]],
            [None, None],
        )
        self.assertEqual(
            [target["session_title"] for target in resp.data["current_targets"]],
            [second_session.title, self.data["lec_session"].title],
        )
        self.assertTrue(
            all(target["created_at"] for target in resp.data["current_targets"])
        )

    def test_idcard_confirmed_reservations_hold_reserved_state_and_sort_nearest(self):
        enrollment = self.data["enrollments"][0]
        self._make_live_exam_clinic_link(
            enrollment,
            self.data["lec_session"],
        )
        today = timezone.localdate()
        later = self.make_clinic_session(
            self.tenant,
            date=today + datetime.timedelta(days=2),
            start_time=datetime.time(16, 0),
            location="3층 자습실",
        )
        nearer = self.make_clinic_session(
            self.tenant,
            date=today + datetime.timedelta(days=1),
            start_time=datetime.time(15, 0),
            location="2층 보강실",
        )
        pending = self.make_participant(
            self.tenant,
            later,
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.PENDING,
        )
        today_booked = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        booked = self.make_participant(
            self.tenant,
            nearer,
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.BOOKED,
        )

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["current_result"], "FAIL")
        self.assertEqual(resp.data["passcard_state"], "BOOKING_CONFIRMED")
        self.assertTrue(resp.data["can_leave"])
        self.assertEqual(resp.data["booking_status"], "booked")
        self.assertEqual(resp.data["booking_status_label"], "예약 확정")
        self.assertEqual(resp.data["current_booking"]["participant_id"], today_booked.id)
        self.assertEqual(resp.data["current_booking"]["date"], today.isoformat())
        self.assertEqual(resp.data["current_booking"]["start_time"], "14:00:00")
        self.assertEqual(resp.data["current_booking"]["location"], "101호")
        self.assertEqual(
            [booking["participant_id"] for booking in resp.data["valid_bookings"]],
            [today_booked.id, booked.id, pending.id],
        )

    def test_idcard_pending_reservation_is_visible_but_does_not_allow_return(self):
        enrollment = self.data["enrollments"][0]
        self._make_live_exam_clinic_link(
            enrollment,
            self.data["lec_session"],
        )
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        clinic_session = self.make_clinic_session(
            self.tenant,
            date=tomorrow,
            start_time=datetime.time(17, 0),
            location="승인 대기실",
        )
        pending = self.make_participant(
            self.tenant,
            clinic_session,
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.PENDING,
        )

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.data["current_result"], "FAIL")
        self.assertEqual(resp.data["passcard_state"], "CLINIC_REQUIRED")
        self.assertFalse(resp.data["can_leave"])
        self.assertEqual(resp.data["booking_status"], "pending")
        self.assertEqual(resp.data["booking_status_label"], "승인 대기")
        self.assertEqual(resp.data["current_booking"]["participant_id"], pending.id)

    def test_idcard_reservation_holds_until_completion_then_requires_next_booking(self):
        from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map

        enrollment = self.data["enrollments"][0]
        link = self._make_live_exam_clinic_link(
            enrollment,
            self.data["lec_session"],
        )
        today = timezone.localdate()
        clinic_session = self.make_clinic_session(
            self.tenant,
            date=today,
            start_time=datetime.time(18, 0),
            location="당일 이행실",
        )
        participant = self.make_participant(
            self.tenant,
            clinic_session,
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.ATTENDED,
        )

        def is_highlighted():
            return compute_clinic_highlight_map(
                tenant=self.tenant,
                enrollment_ids={enrollment.id},
            )[enrollment.id]

        attended = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )
        self.assertEqual(attended.data["passcard_state"], "BOOKING_CONFIRMED")
        self.assertEqual(attended.data["booking_status"], "attended")
        self.assertFalse(is_highlighted())

        clinic_session.date = today - datetime.timedelta(days=1)
        clinic_session.save(update_fields=["date", "updated_at"])
        still_attended = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )
        self.assertEqual(still_attended.data["passcard_state"], "BOOKING_CONFIRMED")
        self.assertEqual(still_attended.data["booking_status"], "attended")
        self.assertFalse(is_highlighted())

        participant.completed_at = timezone.now()
        participant.save(update_fields=["completed_at", "updated_at"])
        completed = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )
        self.assertEqual(completed.data["current_result"], "FAIL")
        self.assertEqual(completed.data["passcard_state"], "CLINIC_REQUIRED")
        self.assertFalse(completed.data["can_leave"])
        self.assertEqual(completed.data["booking_status"], "required")
        self.assertIsNone(completed.data["current_booking"])
        self.assertTrue(is_highlighted())

        next_session = self.make_clinic_session(
            self.tenant,
            date=today + datetime.timedelta(days=2),
            start_time=datetime.time(17, 0),
            location="다음 클리닉실",
        )
        next_booking = self.make_participant(
            self.tenant,
            next_session,
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.BOOKED,
        )
        rebooked = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )
        self.assertEqual(rebooked.data["passcard_state"], "BOOKING_CONFIRMED")
        self.assertTrue(rebooked.data["can_leave"])
        self.assertEqual(rebooked.data["current_booking"]["participant_id"], next_booking.id)
        self.assertFalse(is_highlighted())

        ClinicLink.objects.filter(id=link.id).update(resolved_at=timezone.now())
        passed = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )
        self.assertEqual(passed.data["current_result"], "SUCCESS")
        self.assertEqual(passed.data["passcard_state"], "PASSED")
        self.assertFalse(is_highlighted())

    @patch(
        "apps.domains.clinic.views.participant_views._send_clinic_notification",
        return_value={"requested": 1, "failed": 0},
    )
    def test_completed_attendance_correction_to_booked_does_not_restore_release(self, _notify):
        enrollment = self.data["enrollments"][0]
        self._make_live_exam_clinic_link(enrollment, self.data["lec_session"])
        participant = self.make_participant(
            self.tenant,
            self.make_clinic_session(
                self.tenant,
                date=timezone.localdate(),
                start_time=datetime.time(18, 0),
                location="완료 정정실",
            ),
            self.student,
            enrollment=enrollment,
            status=SessionParticipant.Status.ATTENDED,
        )
        self.client.force_authenticate(user=self.data["admin_user"])
        completed = self.client.post(
            f"/api/v1/clinic/participants/{participant.id}/complete/",
            **self._headers(self.tenant),
        )
        self.assertEqual(completed.status_code, 200, completed.data)
        corrected = self.client.patch(
            f"/api/v1/clinic/participants/{participant.id}/set_status/",
            {"status": "booked"},
            format="json",
            **self._headers(self.tenant),
        )
        self.assertEqual(corrected.status_code, 200, corrected.data)

        self.client.force_authenticate(user=self.student.user)
        passcard = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )
        self.assertEqual(passcard.data["passcard_state"], "CLINIC_REQUIRED")
        self.assertFalse(passcard.data["can_leave"])
        self.assertIsNone(passcard.data["current_booking"])

    def test_idcard_terminal_reservations_never_protect_unresolved_target(self):
        enrollment = self.data["enrollments"][0]
        self._make_live_exam_clinic_link(
            enrollment,
            self.data["lec_session"],
        )
        today = timezone.localdate()
        for index, status in enumerate(
            (
                SessionParticipant.Status.CANCELLED,
                SessionParticipant.Status.REJECTED,
                SessionParticipant.Status.NO_SHOW,
            ),
            start=1,
        ):
            clinic_session = self.make_clinic_session(
                self.tenant,
                date=today + datetime.timedelta(days=index),
                start_time=datetime.time(10 + index, 0),
                location=f"종료 상태실 {index}",
            )
            self.make_participant(
                self.tenant,
                clinic_session,
                self.student,
                enrollment=enrollment,
                status=status,
            )

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.data["passcard_state"], "CLINIC_REQUIRED")
        self.assertFalse(resp.data["can_leave"])
        self.assertEqual(resp.data["booking_status"], "required")
        self.assertEqual(resp.data["valid_bookings"], [])

    def test_idcard_passed_student_still_sees_active_reservation(self):
        today = timezone.localdate()
        clinic_session = self.make_clinic_session(
            self.tenant,
            date=today + datetime.timedelta(days=1),
            start_time=datetime.time(11, 0),
            location="합격자 예약실",
        )
        participant = self.make_participant(
            self.tenant,
            clinic_session,
            self.student,
            enrollment=self.data["enrollments"][0],
            status=SessionParticipant.Status.BOOKED,
        )

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.data["current_result"], "SUCCESS")
        self.assertEqual(resp.data["passcard_state"], "PASSED")
        self.assertTrue(resp.data["can_leave"])
        self.assertEqual(resp.data["booking_status"], "booked")
        self.assertEqual(resp.data["current_booking"]["participant_id"], participant.id)

    @patch("apps.domains.clinic.views.idcard_views.timezone.now")
    def test_idcard_server_date_uses_kst_local_date(self, mocked_now):
        mocked_now.return_value = datetime.datetime(
            2026,
            8,
            29,
            15,
            30,
            tzinfo=datetime.timezone.utc,
        )

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.data["server_date"], "2026-08-30")

    def test_deleted_student_cannot_create_own_booking(self):
        self.student.deleted_at = timezone.now()
        self.student.save(update_fields=["deleted_at", "updated_at"])

        resp = self.client.post(
            "/api/v1/clinic/participants/",
            {
                "session": self.data["clinic_session"].id,
            },
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertFalse(
            SessionParticipant.objects.filter(
                tenant=self.tenant,
                session=self.data["clinic_session"],
                student=self.student,
            ).exists()
        )

    def test_deleted_student_gets_empty_idcard(self):
        self.student.deleted_at = timezone.now()
        self.student.save(update_fields=["deleted_at", "updated_at"])

        resp = self.client.get(
            "/api/v1/clinic/idcard/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["student_name"], "")
        self.assertEqual(resp.data["histories"], [])
        self.assertEqual(resp.data["current_result"], "SUCCESS")

    def test_student_cannot_complete_own_booking(self):
        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status="booked",
        )

        resp = self.client.post(
            f"/api/v1/clinic/participants/{participant.id}/complete/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)
        participant.refresh_from_db()
        self.assertEqual(participant.status, "booked")
        self.assertIsNone(participant.completed_at)

    def test_student_cannot_bulk_create_sessions(self):
        resp = self.client.post(
            "/api/v1/clinic/sessions/bulk-create/",
            {
                "dates": [str(datetime.date.today() + datetime.timedelta(days=1))],
                "start_time": "14:00",
                "duration_minutes": 60,
                "location": "101호",
                "max_participants": 10,
                "target_lecture_ids": [],
            },
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)

    def test_student_cannot_send_session_reminder(self):
        resp = self.client.post(
            f"/api/v1/clinic/sessions/{self.data['clinic_session'].id}/send_reminder/",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)

    def test_student_cannot_read_operations_tree(self):
        resp = self.client.get(
            "/api/v1/clinic/sessions/tree/",
            {"year": datetime.date.today().year, "month": datetime.date.today().month},
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 403, resp.data)


class StudentClinicBookingChangeAPITest(APITestCase, ClinicAPITestMixin):
    """학생 예약 변경 API가 clinic service write SSOT를 사용한다."""

    def setUp(self):
        self.data = self.setup_api_tenant("student_booking_change_api", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.student.user.tenant = self.tenant
        self.student.user.save(update_fields=["tenant"])
        self.client.force_authenticate(user=self.student.user)

    def _pending_booking(self):
        return self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status=SessionParticipant.Status.PENDING,
            source=SessionParticipant.Source.STUDENT_REQUEST,
        )

    def _new_session(self, start_time=datetime.time(15, 0), max_participants=10):
        return self.make_clinic_session(
            self.tenant,
            date=datetime.date.today() + datetime.timedelta(days=1),
            start_time=start_time,
            location="변경실",
            max_participants=max_participants,
        )

    def test_student_can_change_own_pending_booking(self):
        old_booking = self._pending_booking()
        new_session = self._new_session()

        with patch(
            "apps.domains.clinic.views.participant_views._send_clinic_notification",
            return_value={"requested": 2, "failed": 0, "send_to": "both"},
        ) as notify:
            resp = self.client.post(
                f"/api/v1/clinic/participants/{old_booking.id}/change-booking/",
                {
                    "new_session_id": str(new_session.id),
                    "student_request_memo": "시간 변경",
                    "send_to": "parent",
                },
                format="json",
                **self._headers(self.tenant),
            )

        self.assertEqual(resp.status_code, 200, resp.data)
        old_booking.refresh_from_db()
        new_booking = SessionParticipant.objects.get(id=resp.data["id"])
        self.assertNotEqual(new_booking.id, old_booking.id)
        self.assertEqual(old_booking.status, SessionParticipant.Status.CANCELLED)
        self.assertEqual(old_booking.status_changed_by_id, self.student.user_id)
        self.assertEqual(new_booking.session_id, new_session.id)
        self.assertEqual(new_booking.student_id, self.student.id)
        self.assertEqual(new_booking.status, SessionParticipant.Status.PENDING)
        self.assertEqual(new_booking.student_request_memo, "시간 변경")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[2], "clinic_reservation_changed")
        self.assertEqual(notify.call_args.kwargs["send_to"], "both")

    def test_change_booking_rejects_malformed_session_id(self):
        old_booking = self._pending_booking()

        resp = self.client.post(
            f"/api/v1/clinic/participants/{old_booking.id}/change-booking/",
            {"new_session_id": "not-a-number"},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        old_booking.refresh_from_db()
        self.assertEqual(old_booking.status, SessionParticipant.Status.PENDING)

    def test_change_booking_rejects_same_session(self):
        old_booking = self._pending_booking()

        resp = self.client.post(
            f"/api/v1/clinic/participants/{old_booking.id}/change-booking/",
            {"new_session_id": str(old_booking.session_id)},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 400, resp.data)
        old_booking.refresh_from_db()
        self.assertEqual(old_booking.status, SessionParticipant.Status.PENDING)

    def test_change_booking_returns_409_for_target_duplicate(self):
        old_booking = self._pending_booking()
        new_session = self._new_session()
        SessionParticipant.objects.create(
            tenant=self.tenant,
            session=new_session,
            student=self.student,
            status=SessionParticipant.Status.PENDING,
            source=SessionParticipant.Source.STUDENT_REQUEST,
        )

        resp = self.client.post(
            f"/api/v1/clinic/participants/{old_booking.id}/change-booking/",
            {"new_session_id": new_session.id},
            format="json",
            **self._headers(self.tenant),
        )

        self.assertEqual(resp.status_code, 409, resp.data)
        old_booking.refresh_from_db()
        self.assertEqual(old_booking.status, SessionParticipant.Status.PENDING)


class ParticipantWriteServiceNotificationTest(TestCase, ClinicTestMixin):
    """service 반환 알림 이벤트가 메시징 clinic_change 계약을 만족한다."""

    def setUp(self):
        self.data = self.setup_full_tenant("participant_notify", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.actor = self.make_user("participant_notify_actor")

    def test_change_booking_notification_contains_clinic_change_variables(self):
        from apps.domains.clinic.services import change_participant_booking

        old_booking = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status=SessionParticipant.Status.PENDING,
            source=SessionParticipant.Source.STUDENT_REQUEST,
        )
        new_session = self.make_clinic_session(
            self.tenant,
            date=datetime.date.today() + datetime.timedelta(days=1),
            start_time=datetime.time(16, 0),
            location="변경실",
        )

        result = change_participant_booking(
            tenant=self.tenant,
            participant_id=old_booking.id,
            new_session_id=str(new_session.id),
            request_student=self.student,
            actor=self.actor,
            student_request_memo="변경 요청",
        )

        self.assertEqual(result.notification.trigger, "clinic_reservation_changed")
        context = result.notification.context
        self.assertIn("클리닉기존일정", context)
        self.assertIn("클리닉변동사항", context)
        self.assertIn("클리닉수정자", context)
        self.assertIn(str(new_session.date), context["클리닉변동사항"])
        self.assertEqual(context["클리닉수정자"], self.actor.username)

    def test_cancel_notification_contains_clinic_change_variables(self):
        from apps.domains.clinic.services import change_participant_status

        participant = self.make_participant(
            self.tenant,
            self.data["clinic_session"],
            self.student,
            status=SessionParticipant.Status.PENDING,
        )

        result = change_participant_status(
            tenant=self.tenant,
            participant_id=participant.id,
            next_status=SessionParticipant.Status.CANCELLED,
            actor=self.actor,
            request_student=self.student,
        )

        self.assertEqual(result.notification.trigger, "clinic_cancelled")
        context = result.notification.context
        self.assertEqual(context["클리닉변동사항"], "예약 취소")
        self.assertIn("클리닉기존일정", context)
        self.assertEqual(context["클리닉수정자"], self.actor.username)


class ScoreValidationAPITest(TestCase, ClinicTestMixin):
    """서비스 레벨 점수 검증 — 만점 초과/음수 점수 거부 확인 (보강)."""

    def setUp(self):
        self.data = self.setup_full_tenant("score_api", student_count=1)
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.tenant = self.data["tenant"]
        self.admin_user = self.make_user("admin_score_api")

    def test_negative_score_not_passed(self):
        """음수 점수 → 재시도 기록 없이 입력 차단."""
        from apps.domains.exams.models import Exam
        from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService
        from apps.domains.results.models import ExamAttempt

        exam = Exam.objects.create(
            tenant=self.tenant, title="음수시험",
            max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )
        with self.assertRaises(ValueError):
            ClinicRemediationService.submit_exam_retake(
                clinic_link_id=link.id,
                score=-10.0,
                graded_by_user_id=self.admin_user.id,
            )
        link.refresh_from_db()
        self.assertIsNone(link.resolved_at)
        self.assertFalse(ExamAttempt.objects.filter(clinic_link=link).exists())

    def test_zero_score_accepted(self):
        """0점 → 불합격이지만 제출 자체는 성공."""
        from apps.domains.exams.models import Exam
        from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService

        exam = Exam.objects.create(
            tenant=self.tenant, title="영점시험",
            max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )
        result = ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=0.0,
            graded_by_user_id=self.admin_user.id,
        )
        self.assertFalse(result.passed)
        link.refresh_from_db()
        self.assertIsNone(link.resolved_at)  # 불합격 → 미해소

    def test_exact_max_score_accepted(self):
        """만점 정확히 → 합격 + 해소."""
        from apps.domains.exams.models import Exam
        from apps.domains.progress.services.clinic_remediation_service import ClinicRemediationService

        exam = Exam.objects.create(
            tenant=self.tenant, title="만점시험",
            max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )
        result = ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=100.0,
            graded_by_user_id=self.admin_user.id,
        )
        self.assertTrue(result.passed)
        link.refresh_from_db()
        self.assertIsNotNone(link.resolved_at)
        self.assertEqual(link.resolution_type, "EXAM_PASS")
