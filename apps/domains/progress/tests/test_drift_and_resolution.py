"""
시험↔클리닉 드리프트 해소 및 resolution lifecycle 회귀 테스트

검증 포인트:
1. 드리프트 해소: 세션에 시험 여러 개 + 일부 불합격 → exam_passed=False
2. ClinicLink meta 누적: auto_create_if_failed → auto_create_if_exam_risk 시
   kinds 배열에 EXAM_FAILED/EXAM_RISK 둘 다 보존
3. carry_over → resolution_type=CARRIED_OVER (WAIVED 아님)
4. resolution_history append-only 이력
5. submit_exam_retake: 이미 해소된 link → ValueError
6. submit_exam_retake/homework_retake: 음수 점수 → ValueError
7. completed_at 불변 (미완료 회귀해도 유지)
8. legacy fallback: meta.exam_id 매칭 실패 시 skip (과매칭 방지)
"""
from __future__ import annotations


from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from apps.domains.clinic.tests import ClinicTestMixin
from apps.core.models import TenantMembership
from apps.domains.exams.models import Exam, ExamEnrollment, ExamLecturePolicy
from apps.domains.progress.models import (
    ClinicLink,
    ProgressPolicy,
)
from apps.domains.progress.services.clinic_remediation_service import (
    ClinicRemediationService,
)
from apps.domains.progress.services.clinic_resolution_service import (
    ClinicResolutionService,
)
from apps.domains.progress.services.clinic_trigger_service import (
    ClinicTriggerService,
)
from apps.domains.progress.views import ClinicLinkViewSet

User = get_user_model()


class SharedExamTargetScopeTest(TestCase, ClinicTestMixin):
    def setUp(self):
        self.data = self.setup_full_tenant("shared-target", student_count=2)
        self.tenant = self.data["tenant"]
        self.target_enrollment, self.other_enrollment = self.data["enrollments"]
        self.lec_session = self.data["lec_session"]
        self.lecture = self.data["lecture"]

    def test_non_target_exam_does_not_fail_progress_or_create_clinic(self):
        from apps.domains.results.models import Result
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        target_exam = Exam.objects.create(
            tenant=self.tenant,
            title="Target exam",
            pass_score=60,
            max_score=100,
        )
        other_exam = Exam.objects.create(
            tenant=self.tenant,
            title="Other student's exam",
            pass_score=60,
            max_score=100,
        )
        target_exam.sessions.add(self.lec_session)
        other_exam.sessions.add(self.lec_session)
        ExamEnrollment.objects.create(
            exam=target_exam,
            enrollment=self.target_enrollment,
        )
        ExamEnrollment.objects.create(
            exam=other_exam,
            enrollment=self.other_enrollment,
        )
        Result.objects.create(
            target_type="exam",
            target_id=target_exam.id,
            enrollment=self.target_enrollment,
            total_score=85,
            max_score=100,
        )
        ProgressPolicy.objects.create(
            lecture=self.lecture,
            exam_pass_source=ProgressPolicy.ExamPassSource.EXAM,
            exam_start_session_order=1,
            homework_start_session_order=2,
        )

        progress = SessionProgressCalculator.calculate(
            enrollment_id=self.target_enrollment.id,
            session=self.lec_session,
            attendance_type="offline",
        )
        ClinicTriggerService.auto_create_per_exam(progress)

        self.assertTrue(progress.exam_passed)
        self.assertEqual(
            [row["exam_id"] for row in progress.exam_meta["exams"]],
            [target_exam.id],
        )
        self.assertFalse(
            ClinicLink.objects.filter(
                enrollment=self.target_enrollment,
                source_type="exam",
                source_id=other_exam.id,
            ).exists()
        )


class DriftResolutionTest(TestCase, ClinicTestMixin):
    """Phase 2: 드리프트 해소 관련 테스트"""

    def setUp(self):
        self.data = self.setup_full_tenant("drift", student_count=1)
        self.tenant = self.data["tenant"]
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.lecture = self.data["lecture"]

    def test_clinic_link_viewset_rejects_mismatched_enrollment_tenant(self):
        other = self.setup_full_tenant("drift-other", student_count=1)
        mismatched = self.make_clinic_link(
            other["enrollments"][0],
            self.lec_session,
            tenant=self.tenant,
        )

        request = RequestFactory().get("/progress/clinic-links/")
        request.tenant = self.tenant
        request.query_params = request.GET
        view = ClinicLinkViewSet()
        view.request = request

        self.assertNotIn(
            mismatched.id,
            set(view.get_queryset().values_list("id", flat=True)),
        )

    def test_session_not_completed_when_any_exam_failed(self):
        """세션에 시험 2개, 1개 통과/1개 불합격 → exam_passed=False"""
        from apps.domains.results.models import Result
        exam_a = Exam.objects.create(tenant=self.tenant, title="A", pass_score=60.0, max_score=100.0)
        exam_b = Exam.objects.create(tenant=self.tenant, title="B", pass_score=60.0, max_score=100.0)
        exam_a.sessions.add(self.lec_session)
        exam_b.sessions.add(self.lec_session)

        Result.objects.create(
            target_type="exam", target_id=exam_a.id,
            enrollment=self.enrollment, total_score=80, max_score=100,
        )
        Result.objects.create(
            target_type="exam", target_id=exam_b.id,
            enrollment=self.enrollment, total_score=40, max_score=100,
        )

        # exam 범위에 regular_order=1 포함하도록 policy 명시 (defaults는 2 이상)
        policy, _ = ProgressPolicy.objects.get_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "homework_start_session_order": 2,
            },
        )
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )
        sp = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        self.assertFalse(sp.exam_passed, "MAX 전략이어도 개별 불합격 있으면 passed=False")
        self.assertFalse(sp.completed, "일부 시험 불합격 → 세션 미완료")
        self.assertIn("all_passed", sp.exam_meta)
        self.assertFalse(sp.exam_meta["all_passed"])

    def test_session_uses_shared_exam_cutoff_for_its_lecture(self):
        """강의별 귀가 기준은 SessionProgress와 ClinicLink 판정에도 적용된다."""
        from apps.domains.results.models import Result
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        exam = Exam.objects.create(
            tenant=self.tenant,
            title="학교 공통 시험",
            pass_score=60.0,
            max_score=100.0,
        )
        exam.sessions.add(self.lec_session)
        ExamLecturePolicy.objects.create(
            exam=exam,
            lecture=self.lecture,
            pass_score=70.0,
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=65,
            max_score=100,
        )
        ProgressPolicy.objects.update_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "exam_end_session_order": 9999,
            },
        )

        progress = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )

        self.assertFalse(progress.exam_passed)
        self.assertEqual(progress.exam_meta["exams"][0]["pass_score"], 70.0)

    def test_missing_result_blocks_session_completion(self):
        """세션에 시험 2개, Result는 1개만 있어도 세션 완료로 판정되면 안 됨.

        시험 A에 응시 + 합격, 시험 B는 Result 없음(미응시/채점 미입력) 상태에서
        이전에는 per_exam_rows가 A 1건만 생성되어 `all(passed)`=True로 세션 완료.
        수정 후: per_exam_rows는 exam_ids 전체(2건) 포함하고 B는 passed=False(no_result)
        → exam_passed=False, 세션 완료 차단.
        """
        from apps.domains.results.models import Result
        exam_a = Exam.objects.create(tenant=self.tenant, title="A", pass_score=60.0, max_score=100.0)
        exam_b = Exam.objects.create(tenant=self.tenant, title="B", pass_score=60.0, max_score=100.0)
        exam_a.sessions.add(self.lec_session)
        exam_b.sessions.add(self.lec_session)

        Result.objects.create(
            target_type="exam", target_id=exam_a.id,
            enrollment=self.enrollment, total_score=80, max_score=100,
        )
        # exam_b에는 Result 없음 (누락 상태)

        ProgressPolicy.objects.get_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "homework_start_session_order": 2,
            },
        )
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )
        sp = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        self.assertFalse(
            sp.exam_passed,
            "exam_b Result 누락 → 전수 검사에서 passed=False여야 함",
        )
        self.assertFalse(sp.completed, "Result 누락 시험이 있으면 세션 완료 불가")
        exams_meta = sp.exam_meta.get("exams", [])
        self.assertEqual(len(exams_meta), 2, "exam_ids 전체가 per_exam_rows에 포함")
        b_row = next((x for x in exams_meta if x["exam_id"] == exam_b.id), None)
        self.assertIsNotNone(b_row)
        self.assertTrue(b_row.get("no_result"))
        self.assertFalse(b_row.get("passed"))
        self.assertTrue(sp.exam_meta.get("missing_results"))

    def test_missing_result_creates_actionable_auto_clinic_link(self):
        """미채점/미응시 시험도 현재 학습 todo이면 검토 가능한 클리닉 대상이다."""
        from apps.domains.results.models import Result
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        exam_a = Exam.objects.create(tenant=self.tenant, title="A", pass_score=60.0, max_score=100.0)
        exam_b = Exam.objects.create(tenant=self.tenant, title="B", pass_score=60.0, max_score=100.0)
        exam_a.sessions.add(self.lec_session)
        exam_b.sessions.add(self.lec_session)

        Result.objects.create(
            target_type="exam", target_id=exam_a.id,
            enrollment=self.enrollment, total_score=80, max_score=100,
        )

        ProgressPolicy.objects.get_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "homework_start_session_order": 2,
            },
        )
        sp = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )

        self.assertFalse(sp.exam_passed)
        self.assertTrue(sp.exam_meta.get("missing_results"))
        ClinicTriggerService.auto_create_if_failed(sp)

        missing_link = ClinicLink.objects.get(
            enrollment=self.enrollment,
            session=self.lec_session,
            source_type="exam",
            source_id=exam_b.id,
        )
        self.assertEqual(missing_link.meta.get("kind"), "EXAM_MISSING")
        self.assertIsNone(missing_link.meta.get("score"))

    def test_scored_failed_exam_still_creates_auto_clinic_link(self):
        """실제 점수가 있는 불합격 시험은 기존처럼 자동 클리닉 대상이 된다."""
        from apps.domains.results.models import Result
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        exam = Exam.objects.create(tenant=self.tenant, title="Failed", pass_score=60.0, max_score=100.0)
        exam.sessions.add(self.lec_session)
        Result.objects.create(
            target_type="exam", target_id=exam.id,
            enrollment=self.enrollment, total_score=40, max_score=100,
        )
        ProgressPolicy.objects.get_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "homework_start_session_order": 2,
            },
        )
        sp = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )

        ClinicTriggerService.auto_create_if_failed(sp)

        self.assertTrue(
            ClinicLink.objects.filter(
                enrollment=self.enrollment,
                session=self.lec_session,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).exists()
        )

    def test_retake_is_not_poisoned_by_old_not_submitted_attempt(self):
        """정상 재응시는 과거 비대표 미응시 이력과 무관하게 현재 점수로 판정한다."""
        from apps.domains.results.models import ExamAttempt, Result
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Absent then passed",
            pass_score=60.0,
            max_score=100.0,
        )
        exam.sessions.add(self.lec_session)
        ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=1,
            is_representative=False,
            status="done",
            meta={"status": "NOT_SUBMITTED"},
        )
        retake = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=2,
            is_retake=True,
            is_representative=True,
            status="done",
            meta={"total_score": 85.0, "max_score": 100.0},
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=retake,
            total_score=85,
            max_score=100,
        )
        ProgressPolicy.objects.update_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "exam_end_session_order": 9999,
            },
        )

        progress = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        ClinicTriggerService.auto_create_per_exam(progress)

        self.assertTrue(progress.exam_passed)
        self.assertIsNone(progress.exam_meta["exams"][0]["meta_status"])
        self.assertFalse(
            ClinicLink.objects.filter(
                enrollment=self.enrollment,
                session=self.lec_session,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).exists()
        )

    def test_exam_pass_resolution_does_not_recreate_failed_clinic_link(self):
        """재시험 합격으로 닫힌 시험은 파이프라인 재실행 후에도 새 미해소 링크가 생기면 안 된다."""
        from django.apps import apps
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        Result = apps.get_model("results", "Result")
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="RetakePass",
            pass_score=60.0,
            max_score=100.0,
        )
        exam.sessions.add(self.lec_session)
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=40,
            max_score=100,
        )
        ProgressPolicy.objects.get_or_create(
            lecture=self.lecture,
            defaults={
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "exam_start_session_order": 1,
                "homework_start_session_order": 2,
            },
        )

        sp = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        ClinicTriggerService.auto_create_if_failed(sp)
        link = ClinicLink.objects.get(
            enrollment=self.enrollment,
            session=self.lec_session,
            source_type="exam",
            source_id=exam.id,
            resolved_at__isnull=True,
        )

        retake = ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=80.0,
            max_score=100.0,
            pass_score=60.0,
            graded_by_user_id=self.enrollment.student.user_id,
        )
        self.assertTrue(retake.passed)
        link.refresh_from_db()
        self.assertEqual(link.resolution_type, ClinicLink.ResolutionType.EXAM_PASS)
        self.assertFalse(
            ClinicLink.objects.filter(
                enrollment=self.enrollment,
                session=self.lec_session,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).exists(),
            "EXAM_PASS 해소 직후 미해소 링크가 남으면 안 됨",
        )

        sp_after = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        ClinicTriggerService.auto_create_if_failed(sp_after)

        self.assertEqual(
            ClinicLink.objects.filter(
                enrollment=self.enrollment,
                session=self.lec_session,
                source_type="exam",
                source_id=exam.id,
            ).count(),
            1,
            "해소된 같은 source에 cycle 2 미해소 링크를 재생성하면 안 됨",
        )
        self.assertFalse(
            ClinicLink.objects.filter(
                enrollment=self.enrollment,
                session=self.lec_session,
                source_type="exam",
                source_id=exam.id,
                resolved_at__isnull=True,
            ).exists(),
        )

    def test_completed_at_preserved_on_regression(self):
        """completed=True → 점수 수정으로 False 회귀해도 completed_at 유지"""
        from apps.domains.results.models import Result
        exam = Exam.objects.create(tenant=self.tenant, title="E", pass_score=60.0, max_score=100.0)
        exam.sessions.add(self.lec_session)
        r = Result.objects.create(
            target_type="exam", target_id=exam.id,
            enrollment=self.enrollment, total_score=80, max_score=100,
        )
        # exam 범위에 regular_order=1 포함
        ProgressPolicy.objects.get_or_create(
            lecture=self.lecture,
            defaults={
                "exam_start_session_order": 1,
                "homework_start_session_order": 2,
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
            },
        )

        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )
        sp = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        self.assertTrue(sp.completed)
        self.assertIsNotNone(sp.completed_at)
        first_completed_at = sp.completed_at

        # 점수 하락 (회귀)
        r.total_score = 40
        r.save(update_fields=["total_score", "updated_at"])

        sp2 = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=self.lec_session,
            attendance_type="online",
            video_progress_rate=100,
        )
        self.assertFalse(sp2.completed)
        self.assertEqual(
            sp2.completed_at, first_completed_at,
            "completed_at은 최초 완료 시점으로 불변",
        )

    def test_progress_policy_range_uses_regular_order_not_display_order(self):
        """보강이 display order에 끼어도 진도 정책 범위는 정규 n차시 번호를 기준으로 한다."""
        from django.apps import apps
        from apps.domains.progress.services.session_calculator import (
            SessionProgressCalculator,
        )

        Session = apps.get_model("lectures", "Session")
        supplement = Session.objects.create(
            lecture=self.lecture,
            order=2,
            session_type=Session.SessionType.SUPPLEMENT,
            title="보강",
        )
        regular_second = Session.objects.create(
            lecture=self.lecture,
            order=3,
            regular_order=2,
            title="2차시",
        )
        ProgressPolicy.objects.update_or_create(
            lecture=self.lecture,
            defaults={
                "exam_start_session_order": 2,
                "exam_end_session_order": 2,
                "homework_start_session_order": 2,
                "homework_end_session_order": 2,
            },
        )

        supplement_progress = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=supplement,
            attendance_type="online",
            video_progress_rate=100,
            homework_submitted=False,
        )
        second_progress = SessionProgressCalculator.calculate(
            enrollment_id=self.enrollment.id,
            session=regular_second,
            attendance_type="online",
            video_progress_rate=100,
            homework_submitted=True,
        )

        self.assertEqual(supplement_progress.exam_meta.get("note"), "out_of_exam_range")
        self.assertEqual(second_progress.exam_meta.get("note"), "no_exams_in_session")


class AutoCreateMetaMergeTest(TestCase, ClinicTestMixin):
    """Phase 1: auto_create meta 덮어쓰기 차단"""

    def setUp(self):
        self.data = self.setup_full_tenant("meta", student_count=1)
        self.tenant = self.data["tenant"]
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]

    def test_exam_risk_preserves_failed_evidence(self):
        """auto_create_if_failed → auto_create_if_exam_risk 호출 시 근거 보존"""
        # 첫 단계: EXAM_FAILED 링크 생성
        link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.lec_session,
            reason="AUTO_FAILED",
            source_type="exam",
            source_id=101,
            meta={
                "kind": "EXAM_FAILED",
                "kinds": ["EXAM_FAILED"],
                "exam_id": 101,
                "score": 42.0,
                "pass_score": 60.0,
            },
        )

        # evaluate mock: LOW_CONFIDENCE_OMR 감지
        from unittest.mock import patch
        fake_reasons = {"LOW_CONFIDENCE_OMR": {"count": 3, "threshold": 2}}
        with patch(
            "apps.domains.progress.services.clinic_trigger_service.ClinicExamRuleService.evaluate",
            return_value=fake_reasons,
        ):
            ClinicTriggerService.auto_create_if_exam_risk(
                enrollment_id=self.enrollment.id,
                session=self.lec_session,
                exam_id=101,
            )

        link.refresh_from_db()
        self.assertIn("EXAM_FAILED", link.meta["kinds"], "기존 EXAM_FAILED kind 보존")
        self.assertIn("EXAM_RISK", link.meta["kinds"])
        self.assertEqual(link.meta.get("score"), 42.0, "기존 score 보존")
        self.assertEqual(link.meta.get("pass_score"), 60.0, "기존 pass_score 보존")
        self.assertEqual(link.meta.get("exam_reasons"), fake_reasons)


class CarriedOverResolutionTest(TestCase, ClinicTestMixin):
    """Phase 3: CARRIED_OVER enum + history"""

    def setUp(self):
        self.data = self.setup_full_tenant("carry", student_count=1)
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]

    def test_carry_over_sets_carried_over_type(self):
        """carry_over → resolution_type=CARRIED_OVER (WAIVED 아님)"""
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=1,
        )
        new_link = ClinicResolutionService.carry_over(clinic_link_id=link.id)
        link.refresh_from_db()
        self.assertEqual(link.resolution_type, ClinicLink.ResolutionType.CARRIED_OVER)
        self.assertNotEqual(link.resolution_type, ClinicLink.ResolutionType.WAIVED)
        self.assertEqual(new_link.cycle_no, 2)
        # history 기록 확인
        self.assertTrue(link.resolution_history)
        self.assertEqual(link.resolution_history[-1]["action"], "carry_over")

    def test_unresolve_preserves_evidence_in_history(self):
        """unresolve 시 이전 evidence가 history에 누적"""
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=1,
        )
        ClinicResolutionService.resolve_manually(
            clinic_link_id=link.id, user_id=1, memo="테스트",
        )
        link.refresh_from_db()
        self.assertIsNotNone(link.resolved_at)

        ClinicResolutionService.unresolve(clinic_link_id=link.id)
        link.refresh_from_db()
        self.assertIsNone(link.resolved_at)
        # history에 이전 MANUAL_OVERRIDE + evidence 보존
        actions = [h["action"] for h in (link.resolution_history or [])]
        self.assertIn("resolve_manual", actions)
        self.assertIn("unresolve", actions)
        prev = next(
            h for h in link.resolution_history
            if h["action"] == "unresolve"
        )
        self.assertEqual(prev["prev_resolution_type"], "MANUAL_OVERRIDE")
        self.assertEqual(prev["prev_evidence"].get("memo"), "테스트")


class RemediationValidationTest(TestCase, ClinicTestMixin):
    """Phase 1: 점수 검증 & 중복 요청 방어"""

    def setUp(self):
        self.data = self.setup_full_tenant("remed", student_count=1)
        self.tenant = self.data["tenant"]
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.admin = User.objects.create_user(
            username="admin_remed", password="x",
        )

    def test_exam_retake_negative_score_rejected(self):
        """음수 점수 → ValueError"""
        exam = Exam.objects.create(
            tenant=self.tenant, title="N", max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )
        with self.assertRaises(ValueError) as ctx:
            ClinicRemediationService.submit_exam_retake(
                clinic_link_id=link.id, score=-5.0,
                graded_by_user_id=self.admin.id,
            )
        self.assertIn("0 이상", str(ctx.exception))

    def test_resolved_link_retake_rejected(self):
        """이미 해소된 link에 retake 요청 → ValueError"""
        exam = Exam.objects.create(
            tenant=self.tenant, title="R", max_score=100.0, pass_score=60.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )
        # 수동 해소
        ClinicResolutionService.resolve_manually(
            clinic_link_id=link.id, user_id=self.admin.id,
        )
        with self.assertRaises(ValueError) as ctx:
            ClinicRemediationService.submit_exam_retake(
                clinic_link_id=link.id, score=80.0,
                graded_by_user_id=self.admin.id,
            )
        self.assertIn("이미 해소", str(ctx.exception))

    def test_exam_retake_uses_custom_cutline_and_keeps_attempt_meta(self):
        """재시험은 원시험 커트라인 대신 시도별 커트라인으로 통과 판정한다."""
        from apps.domains.results.models import ExamAttempt

        exam = Exam.objects.create(
            tenant=self.tenant, title="Cut", max_score=100.0, pass_score=70.0,
        )
        link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=exam.id,
        )

        result = ClinicRemediationService.submit_exam_retake(
            clinic_link_id=link.id,
            score=60.0,
            max_score=100.0,
            pass_score=60.0,
            graded_by_user_id=self.admin.id,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.pass_score, 60.0)
        link.refresh_from_db()
        self.assertEqual(link.resolution_type, ClinicLink.ResolutionType.EXAM_PASS)
        self.assertEqual(link.resolution_evidence["pass_score"], 60.0)
        self.assertEqual(link.resolution_evidence["max_score"], 100.0)

        attempt = ExamAttempt.objects.get(
            exam=exam,
            enrollment=self.enrollment,
            attempt_index=result.attempt_index,
        )
        self.assertFalse(attempt.is_representative)
        self.assertEqual(attempt.meta["total_score"], 60.0)
        self.assertEqual(attempt.meta["pass_score"], 60.0)
        self.assertEqual(attempt.meta["max_score"], 100.0)


class StudentResultRemediatedTest(TestCase, ClinicTestMixin):
    """
    student_result_service.get_my_exam_result_data의 remediated/final_pass 판정이
    EXAM_PASS + MANUAL_OVERRIDE 모두 커버하는지 검증.

    드리프트 재발 방지: admin_student_grades_view / student_app.results.views는
    MANUAL_OVERRIDE를 REMEDIATED로 분류하므로, 시험 상세 뷰도 동일하게 맞춰야 한다.
    """

    def setUp(self):
        from apps.domains.exams.models import Exam, ExamEnrollment
        from apps.domains.results.models import Result

        self.data = self.setup_full_tenant("remed_detail", student_count=1)
        self.tenant = self.data["tenant"]
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]
        self.student_user = self.enrollment.student.user
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.student_user,
            role="student",
        )

        self.exam = Exam.objects.create(
            tenant=self.tenant, title="MidTerm",
            max_score=100.0, pass_score=60.0,
            allow_retake=False, max_attempts=1,
        )
        self.exam.sessions.add(self.lec_session)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.enrollment)

        # 1차 불합격 Result
        Result.objects.create(
            target_type="exam", target_id=self.exam.id,
            enrollment=self.enrollment,
            total_score=40, max_score=100,
        )

        self.link = self.make_clinic_link(
            self.enrollment, self.lec_session,
            source_type="exam", source_id=self.exam.id,
        )
        self.admin = User.objects.create_user(
            username="admin_remed_detail", password="x",
        )

    def _fetch(self):
        from unittest.mock import MagicMock
        from apps.domains.results.services.student_result_service import (
            get_my_exam_result_data,
        )
        request = MagicMock()
        request.user = self.student_user
        request.tenant = self.tenant
        return get_my_exam_result_data(request, self.exam.id, tenant=self.tenant)

    def test_exam_pass_resolution_sets_remediated(self):
        """EXAM_PASS(재시험 통과)로 해소되면 remediated=True, final_pass=True"""
        ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=self.exam.id,
            score=80.0, pass_score=60.0,
        )
        data = self._fetch()
        self.assertFalse(data["is_pass"], "1차는 불합격")
        self.assertTrue(data["remediated"])
        self.assertTrue(data["final_pass"])
        self.assertFalse(data["clinic_required"])
        self.assertIsNotNone(data["clinic_retake"])

    def test_manual_override_resolution_sets_remediated(self):
        """MANUAL_OVERRIDE(관리자 수동 해소)도 remediated=True — 드리프트 재발 방지"""
        ClinicResolutionService.resolve_manually(
            clinic_link_id=self.link.id,
            user_id=self.admin.id,
            memo="수동 해소 회귀 테스트",
        )
        data = self._fetch()
        self.assertFalse(data["is_pass"], "1차는 불합격")
        self.assertTrue(
            data["remediated"],
            "MANUAL_OVERRIDE 해소도 remediated로 인식돼야 함",
        )
        self.assertTrue(
            data["final_pass"],
            "MANUAL_OVERRIDE 해소 → 학생 상세에서도 최종 합격으로 보여야 함",
        )
        self.assertFalse(data["clinic_required"])

    def test_waived_resolution_is_not_remediated(self):
        """WAIVED(면제)는 remediated에 포함되지 않음 (목록 뷰 정책과 일치)"""
        ClinicResolutionService.waive(
            clinic_link_id=self.link.id,
            user_id=self.admin.id,
            memo="면제",
        )
        data = self._fetch()
        self.assertFalse(data["remediated"])
        self.assertFalse(data["final_pass"])
        self.assertFalse(data["clinic_required"])


class RankingFirstAttemptTest(TestCase, ClinicTestMixin):
    """
    석차=1차 정책 유지 검증.

    ExamAttempt(attempt_index=1).meta["initial_snapshot"]["total_score"]가 있으면
    ranking은 이 값을 사용해야 한다. Result.total_score가 재응시로 덮어쓰여져도
    석차 기준은 1차 점수 그대로 유지된다.
    """

    def setUp(self):
        from apps.domains.exams.models import Exam, ExamEnrollment
        from apps.domains.results.models import Result, ExamAttempt

        self.data = self.setup_full_tenant("rank_first", student_count=2)
        self.tenant = self.data["tenant"]
        self.e_alice = self.data["enrollments"][0]
        self.e_bob = self.data["enrollments"][1]
        self.lec_session = self.data["lec_session"]

        self.exam = Exam.objects.create(
            tenant=self.tenant, title="RankTest",
            max_score=100.0, pass_score=60.0,
            allow_retake=True, max_attempts=2,
        )
        self.exam.sessions.add(self.lec_session)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.e_alice)
        ExamEnrollment.objects.create(exam=self.exam, enrollment=self.e_bob)

        # Alice: 1차 90점 → 재응시 50점으로 Result 덮어쓰여진 상황 시뮬레이션
        self.r_alice = Result.objects.create(
            target_type="exam", target_id=self.exam.id,
            enrollment=self.e_alice,
            total_score=50, max_score=100,  # 덮어쓰기 후 값
        )
        self.a_alice_1st = ExamAttempt.objects.create(
            exam_id=self.exam.id, enrollment_id=self.e_alice.id,
            attempt_index=1, is_representative=False,  # 재응시로 대표 넘김
            status="done",
            meta={"initial_snapshot": {"total_score": 90.0, "max_score": 100.0}},
        )
        self.a_alice_2nd = ExamAttempt.objects.create(
            exam_id=self.exam.id, enrollment_id=self.e_alice.id,
            attempt_index=2, is_representative=True,
            status="done",
        )
        self.r_alice.attempt_id = self.a_alice_2nd.id
        self.r_alice.save(update_fields=["attempt_id"])

        # Bob: 1차만 70점, 재응시 없음
        self.r_bob = Result.objects.create(
            target_type="exam", target_id=self.exam.id,
            enrollment=self.e_bob,
            total_score=70, max_score=100,
        )
        self.a_bob_1st = ExamAttempt.objects.create(
            exam_id=self.exam.id, enrollment_id=self.e_bob.id,
            attempt_index=1, is_representative=True,
            status="done",
            meta={"initial_snapshot": {"total_score": 70.0, "max_score": 100.0}},
        )
        self.r_bob.attempt_id = self.a_bob_1st.id
        self.r_bob.save(update_fields=["attempt_id"])

    def test_ranking_uses_first_attempt_snapshot(self):
        """Alice의 Result=50점이어도 석차는 1차 90점 기준이어야 한다."""
        from apps.domains.results.utils.ranking import compute_exam_rankings
        ranks = compute_exam_rankings(exam_id=self.exam.id, tenant=self.tenant)
        self.assertEqual(
            ranks[self.e_alice.id]["rank"], 1,
            "Alice 1차 90점 > Bob 70점 → 1등",
        )
        self.assertEqual(ranks[self.e_bob.id]["rank"], 2)
        # cohort_avg도 1차 점수 기반
        self.assertAlmostEqual(ranks[self.e_alice.id]["cohort_avg"], 80.0, places=1)

    def test_ranking_batch_uses_first_attempt_snapshot(self):
        """batch 버전도 1차 점수 기준이어야 한다."""
        from apps.domains.results.utils.ranking import compute_exam_rankings_batch
        batch = compute_exam_rankings_batch(
            exam_ids=[self.exam.id],
            tenant=self.tenant,
        )
        ranks = batch[self.exam.id]
        self.assertEqual(ranks[self.e_alice.id]["rank"], 1)
        self.assertEqual(ranks[self.e_bob.id]["rank"], 2)

    def test_ranking_falls_back_to_result_for_legacy(self):
        """initial_snapshot 없는 legacy attempt는 Result.total_score fallback."""
        from apps.domains.results.utils.ranking import compute_exam_rankings
        # Alice 1차 attempt의 initial_snapshot 제거 → legacy 상태 시뮬레이션
        self.a_alice_1st.meta = {}
        self.a_alice_1st.save(update_fields=["meta"])
        ranks = compute_exam_rankings(exam_id=self.exam.id, tenant=self.tenant)
        # Alice Result=50, Bob Result=70 → Bob 1등
        self.assertEqual(ranks[self.e_bob.id]["rank"], 1)
        self.assertEqual(ranks[self.e_alice.id]["rank"], 2)

    def test_batch_not_submitted_requires_exact_exam_enrollment_pair(self):
        """다른 시험의 미응시 attempt가 현재 시험 석차를 제거하면 안 된다."""
        from apps.domains.results.models import ExamAttempt
        from apps.domains.results.utils.ranking import compute_exam_rankings_batch

        other_exam = Exam.objects.create(
            tenant=self.tenant,
            title="OtherRankTest",
            max_score=100.0,
            pass_score=60.0,
        )
        foreign_pair_attempt = ExamAttempt.objects.create(
            exam=other_exam,
            enrollment=self.e_alice,
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={"status": "NOT_SUBMITTED"},
        )
        self.r_alice.attempt = foreign_pair_attempt
        self.r_alice.save(update_fields=["attempt"])

        ranks = compute_exam_rankings_batch(
            exam_ids=[self.exam.id],
            tenant=self.tenant,
        )[self.exam.id]

        self.assertEqual(ranks[self.e_alice.id]["rank"], 1)
        self.assertEqual(ranks[self.e_bob.id]["rank"], 2)

    def test_first_attempt_not_submitted_excludes_later_retake_from_rankings(self):
        """1차 미응시는 2차 대표 Result 점수가 있어도 석차에서 제외한다."""
        from apps.domains.results.utils.ranking import (
            compute_exam_rankings,
            compute_exam_rankings_batch,
        )

        self.a_alice_1st.meta = {"status": "NOT_SUBMITTED"}
        self.a_alice_1st.save(update_fields=["meta"])

        single = compute_exam_rankings(exam_id=self.exam.id, tenant=self.tenant)
        batch = compute_exam_rankings_batch(
            exam_ids=[self.exam.id],
            tenant=self.tenant,
        )[self.exam.id]

        for ranks in (single, batch):
            self.assertNotIn(self.e_alice.id, ranks)
            self.assertEqual(ranks[self.e_bob.id]["rank"], 1)
            self.assertEqual(ranks[self.e_bob.id]["cohort_size"], 1)

    def test_rankings_filter_invalid_values_and_fallback_from_invalid_snapshot(self):
        """NaN/Infinity/negative values never enter rank output or averages."""
        from apps.domains.results.utils.ranking import (
            compute_exam_rankings,
            compute_exam_rankings_batch,
        )

        self.a_bob_1st.meta = {}
        self.a_bob_1st.save(update_fields=["meta"])
        self.r_bob.total_score = float("inf")
        self.r_bob.save(update_fields=["total_score"])

        single = compute_exam_rankings(exam_id=self.exam.id, tenant=self.tenant)
        batch = compute_exam_rankings_batch(
            exam_ids=[self.exam.id],
            tenant=self.tenant,
        )[self.exam.id]
        for ranks in (single, batch):
            self.assertEqual(ranks[self.e_alice.id]["cohort_size"], 1)
            self.assertEqual(ranks[self.e_alice.id]["cohort_avg"], 90.0)
            self.assertNotIn(self.e_bob.id, ranks)

        self.r_bob.total_score = 70
        self.r_bob.save(update_fields=["total_score"])
        self.a_bob_1st.meta = {
            "initial_snapshot": {"total_score": "NaN"},
        }
        self.a_bob_1st.save(update_fields=["meta"])

        fallback = compute_exam_rankings(
            exam_id=self.exam.id,
            tenant=self.tenant,
        )
        self.assertEqual(fallback[self.e_bob.id]["rank"], 2)
        self.assertEqual(fallback[self.e_bob.id]["cohort_avg"], 80.0)

        self.a_bob_1st.meta = {}
        self.a_bob_1st.save(update_fields=["meta"])
        self.r_bob.total_score = -1
        self.r_bob.save(update_fields=["total_score"])
        negative = compute_exam_rankings(
            exam_id=self.exam.id,
            tenant=self.tenant,
        )
        self.assertNotIn(self.e_bob.id, negative)

    def test_single_and_batch_rankings_exclude_foreign_tenant_enrollment(self):
        from apps.domains.results.models import Result
        from apps.domains.results.utils.ranking import (
            compute_exam_rankings,
            compute_exam_rankings_batch,
        )

        other = self.setup_full_tenant("rank-first-foreign", student_count=1)
        Result.objects.create(
            target_type="exam",
            target_id=self.exam.id,
            enrollment=other["enrollments"][0],
            total_score=1000,
            max_score=100,
        )

        single = compute_exam_rankings(exam_id=self.exam.id, tenant=self.tenant)
        batch = compute_exam_rankings_batch(
            exam_ids=[self.exam.id],
            tenant=self.tenant,
        )[self.exam.id]
        for ranks in (single, batch):
            self.assertEqual(ranks[self.e_alice.id]["rank"], 1)
            self.assertEqual(ranks[self.e_alice.id]["cohort_size"], 2)
            self.assertNotIn(other["enrollments"][0].id, ranks)


class LegacyFallbackTest(TestCase, ClinicTestMixin):
    """Phase 1: legacy fallback 과매칭 차단"""

    def setUp(self):
        self.data = self.setup_full_tenant("legacy", student_count=1)
        self.tenant = self.data["tenant"]
        self.enrollment = self.data["enrollments"][0]
        self.lec_session = self.data["lec_session"]

    def test_legacy_without_matching_exam_id_is_skipped(self):
        """source_type=NULL + meta.exam_id 매칭 없으면 해소하지 않음"""
        # legacy 링크: exam_id 미상
        legacy = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.lec_session,
            reason="AUTO_FAILED",
            source_type=None,
            source_id=None,
            meta={"note": "legacy — no exam_id"},
        )
        count = ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=999,
        )
        self.assertEqual(count, 0, "meta.exam_id 매칭 실패 → skip")
        legacy.refresh_from_db()
        self.assertIsNone(legacy.resolved_at)

    def test_legacy_with_matching_exam_id_is_resolved(self):
        """source_type=NULL + meta.exam_id=999 → 999 통과 시 해소"""
        legacy = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.lec_session,
            reason="AUTO_FAILED",
            source_type=None,
            source_id=None,
            meta={"exam_id": 999, "score": 40, "pass_score": 60},
        )
        count = ClinicResolutionService.resolve_by_exam_pass(
            enrollment_id=self.enrollment.id,
            session_id=self.lec_session.id,
            exam_id=999,
            score=80.0, pass_score=60.0,
        )
        self.assertEqual(count, 1)
        legacy.refresh_from_db()
        self.assertIsNotNone(legacy.resolved_at)
        self.assertEqual(legacy.resolution_type, "EXAM_PASS")
