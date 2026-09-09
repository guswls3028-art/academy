from datetime import date, timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.domains.clinic.tests import ClinicTestMixin
from apps.core.models import TenantMembership
from apps.domains.results.models import ExamAttempt, Result
from apps.domains.results.views.admin_student_grades_view import (
    AdminStudentGradesView,
    _build_exam_progression,
)


User = get_user_model()
_DEFAULT_TENANT = object()


class AdminStudentGradesScopeTest(TestCase, ClinicTestMixin):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.data = self.setup_full_tenant("student-grades-scope", student_count=1)
        self.tenant = self.data["tenant"]
        self.student = self.data["students"][0]
        self.admin_user = User.objects.create_user(
            username="student_grades_scope_admin",
            password="test1234",
            is_staff=True,
            is_superuser=True,
        )
        if hasattr(self.admin_user, "tenant_id"):
            self.admin_user.tenant_id = self.tenant.id
            self.admin_user.save(update_fields=["tenant_id"])
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin_user,
            role="admin",
        )

    def _get(self, student_id, *, user=None, tenant=_DEFAULT_TENANT):
        request = self.factory.get(
            "/results/admin/student-grades/",
            {"student_id": student_id},
        )
        request.tenant = self.tenant if tenant is _DEFAULT_TENANT else tenant
        force_authenticate(request, user=user or self.admin_user)
        return AdminStudentGradesView.as_view()(request)

    def _score(
        self,
        *,
        title: str,
        order: int,
        score: float,
        max_score: float,
        submitted_offset: int = 0,
        is_active: bool = True,
        not_submitted: bool = False,
        session_type: str = "REGULAR",
    ) -> tuple[object, Result]:
        session_model = self.data["lec_session"].__class__
        exam_model = self.data["lec_session"].exams.model
        if order == 1:
            session = self.data["lec_session"]
            session.title = "1차시"
            session.date = date(2026, 7, 1)
            session.save(update_fields=["title", "date"])
        else:
            session = session_model.objects.create(
                lecture=self.data["lecture"],
                order=order,
                title=f"{order}차시",
                date=date(2026, 7, order),
                session_type=session_type,
            )
        exam = exam_model.objects.create(
            tenant=self.tenant,
            title=title,
            exam_type=exam_model.ExamType.REGULAR,
            is_active=is_active,
            pass_score=max_score * 0.6,
            max_score=max_score,
        )
        exam.sessions.add(session)
        attempt = None
        if not_submitted:
            attempt = ExamAttempt.objects.create(
                exam=exam,
                enrollment=self.data["enrollments"][0],
                attempt_index=1,
                is_representative=True,
                status="done",
                meta={"status": "NOT_SUBMITTED"},
            )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.data["enrollments"][0],
            attempt=attempt,
            total_score=score,
            max_score=max_score,
            submitted_at=timezone.now() + timedelta(days=submitted_offset),
        )
        return exam, result

    def _member(self, *, suffix: str, role: str, tenant=None, active: bool = True):
        user = User.objects.create_user(
            username=f"student_grades_{suffix}",
            password="test1234",
        )
        membership = TenantMembership.ensure_active(
            tenant=tenant or self.tenant,
            user=user,
            role=role,
        )
        if not active:
            membership.is_active = False
            membership.save(update_fields=["is_active"])
        return user

    def test_invalid_student_id_returns_400(self):
        response = self._get("abc")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "student_id must be integer")

    def test_active_same_tenant_student_returns_empty_payload_when_no_scores(self):
        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["exams"], [])
        self.assertEqual(response.data["homeworks"], [])
        self.assertEqual(response.data["exam_trend"], [])
        self.assertEqual(response.data["exam_summary"]["scored_count"], 0)

    def test_permission_matrix_is_bound_to_active_tenant_staff_membership(self):
        teacher = self._member(suffix="teacher", role="teacher")
        student_user = self.student.user
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=student_user,
            role="student",
        )
        inactive_teacher = self._member(
            suffix="inactive_teacher",
            role="teacher",
            active=False,
        )
        other = self.setup_full_tenant("student-grades-permission-other", student_count=1)
        foreign_teacher = self._member(
            suffix="foreign_teacher",
            role="teacher",
            tenant=other["tenant"],
        )

        self.assertEqual(self._get(self.student.id, user=teacher).status_code, 200)
        self.assertEqual(self._get(self.student.id, user=student_user).status_code, 403)
        self.assertEqual(self._get(self.student.id, user=inactive_teacher).status_code, 403)
        self.assertEqual(self._get(self.student.id, user=foreign_teacher).status_code, 403)
        self.assertEqual(self._get(self.student.id, tenant=None).status_code, 403)

    def test_cross_tenant_student_returns_404(self):
        other = self.setup_full_tenant("student-grades-other", student_count=1)
        other_student = other["students"][0]

        response = self._get(other_student.id)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["detail"], "student not found")

    def test_soft_deleted_same_tenant_student_returns_404(self):
        self.student.deleted_at = timezone.now()
        self.student.save(update_fields=["deleted_at"])

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["detail"], "student not found")

    def test_exam_results_auto_accumulate_in_academic_order_and_normalize_max_scores(self):
        first_exam, _ = self._score(
            title="Ymath 주간 테스트 1회",
            order=1,
            score=40,
            max_score=50,
            submitted_offset=10,
            is_active=False,
        )
        self._score(
            title="Ymath 주간 테스트 2회",
            order=2,
            score=90,
            max_score=100,
            submitted_offset=1,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["title"] for row in response.data["exam_trend"]],
            ["Ymath 주간 테스트 1회", "Ymath 주간 테스트 2회"],
        )
        self.assertEqual(
            [row["round_index"] for row in response.data["exam_trend"]],
            [1, 2],
        )
        self.assertEqual(
            [row["score_pct"] for row in response.data["exam_trend"]],
            [80.0, 90.0],
        )
        self.assertTrue(response.data["exam_trend"][0]["archived"])
        self.assertEqual(response.data["exam_trend"][0]["exam_id"], first_exam.id)
        self.assertEqual(response.data["exam_summary"]["average_score_pct"], 85.0)
        self.assertEqual(response.data["exam_summary"]["change_pct_points"], 10.0)

        self._score(
            title="Ymath 주간 테스트 3회",
            order=3,
            score=48,
            max_score=50,
        )
        refreshed = self._get(self.student.id)

        self.assertEqual(
            [row["round_index"] for row in refreshed.data["exam_trend"]],
            [1, 2, 3],
        )
        self.assertEqual(refreshed.data["exam_summary"]["latest_score_pct"], 96.0)
        self.assertEqual(refreshed.data["exam_summary"]["best_score_pct"], 96.0)

    def test_exam_history_exposes_regular_and_supplement_session_types(self):
        self._score(
            title="본수업 대수",
            order=2,
            score=80,
            max_score=100,
        )
        self._score(
            title="주말 보강 공수",
            order=3,
            score=70,
            max_score=100,
            session_type="SUPPLEMENT",
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["session_type"] for row in response.data["exam_trend"]],
            ["REGULAR", "SUPPLEMENT"],
        )
        self.assertEqual(
            {row["title"]: row["session_type"] for row in response.data["exams"]},
            {
                "본수업 대수": "REGULAR",
                "주말 보강 공수": "SUPPLEMENT",
            },
        )

    def test_exam_history_does_not_guess_when_one_exam_links_both_session_types(self):
        exam, _result = self._score(
            title="정규 보강 혼합 시험",
            order=2,
            score=85,
            max_score=100,
        )
        supplement = self.data["lec_session"].__class__.objects.create(
            lecture=self.data["lecture"],
            order=90,
            title="혼합 시험 보강",
            session_type="SUPPLEMENT",
        )
        exam.sessions.add(supplement)

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["exams"][0]["session_type"])
        self.assertIsNone(response.data["exam_trend"][0]["session_type"])

    def test_exam_history_keeps_type_but_not_session_when_multiple_same_type_sessions_link(self):
        exam, _result = self._score(
            title="여러 정규 차시 공통 시험",
            order=2,
            score=85,
            max_score=100,
        )
        another_regular = self.data["lec_session"].__class__.objects.create(
            lecture=self.data["lecture"],
            order=3,
            title="공통 시험 두 번째 정규 차시",
            session_type="REGULAR",
        )
        exam.sessions.add(another_regular)

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        for row in (response.data["exams"][0], response.data["exam_trend"][0]):
            self.assertEqual(row["session_type"], "REGULAR")
            self.assertIsNone(row["session_id"])
            self.assertIsNone(row["session_title"])
            self.assertIsNone(row["session_order"])
            self.assertIsNone(row["session_regular_order"])
            self.assertIsNone(row["session_date"])

    def test_exam_history_exposes_rank_and_top_percentile_in_list_and_trend(self):
        exam, _ = self._score(
            title="석차 추이 테스트",
            order=2,
            score=80,
            max_score=100,
        )
        peer_user = User.objects.create_user(
            username="student_grades_rank_peer",
            password="test1234",
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=peer_user,
            role="student",
        )
        peer_student = self.student.__class__.objects.create(
            tenant=self.tenant,
            user=peer_user,
            name="비교 학생",
            ps_number="RANK-PEER",
            omr_code="88000001",
            phone="01088000001",
            parent_phone="01088000002",
        )
        peer_enrollment = self.data["enrollments"][0].__class__.objects.create(
            tenant=self.tenant,
            student=peer_student,
            lecture=self.data["lecture"],
            status="ACTIVE",
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=peer_enrollment,
            total_score=70,
            max_score=100,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        for row in (response.data["exams"][0], response.data["exam_trend"][0]):
            self.assertEqual(row["rank"], 1)
            self.assertEqual(row["percentile"], 50.0)
            self.assertEqual(row["cohort_size"], 2)
            self.assertEqual(row["cohort_avg"], 75.0)

    def test_not_submitted_is_null_in_list_and_excluded_from_trend(self):
        exam, _ = self._score(
            title="Ymath 주간 테스트 미응시",
            order=2,
            score=0,
            max_score=100,
            not_submitted=True,
        )

        poisoned_rank = {
            exam.id: {
                self.data["enrollments"][0].id: {
                    "rank": 1,
                    "percentile": 1.0,
                    "cohort_size": 99,
                    "cohort_avg": 100.0,
                }
            }
        }
        with patch(
            "apps.domains.results.views.admin_student_grades_view.compute_exam_rankings_batch",
            return_value=poisoned_rank,
        ):
            response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["exams"]), 1)
        row = response.data["exams"][0]
        self.assertIsNone(row["total_score"])
        self.assertIsNone(row["rank"])
        self.assertIsNone(row["percentile"])
        self.assertIsNone(row["cohort_size"])
        self.assertIsNone(row["cohort_avg"])
        self.assertEqual(response.data["exam_trend"], [])
        self.assertEqual(response.data["exam_summary"]["scored_count"], 0)

    def test_corrupt_foreign_exam_target_is_not_exposed(self):
        other = self.setup_full_tenant("student-grades-corrupt-target", student_count=1)
        exam_model = other["lec_session"].exams.model
        foreign_exam = exam_model.objects.create(
            tenant=other["tenant"],
            title="다른 학원 비공개 테스트",
            exam_type=exam_model.ExamType.REGULAR,
            is_active=True,
        )
        foreign_exam.sessions.add(other["lec_session"])
        Result.objects.create(
            target_type="exam",
            target_id=foreign_exam.id,
            enrollment=self.data["enrollments"][0],
            total_score=100,
            max_score=100,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"], [])
        self.assertEqual(response.data["exam_trend"], [])

    def test_corrupt_foreign_attempt_status_is_ignored(self):
        local_exam, local_result = self._score(
            title="로컬 정상 시험",
            order=2,
            score=80,
            max_score=100,
        )
        other = self.setup_full_tenant("student-grades-corrupt-attempt", student_count=1)
        exam_model = other["lec_session"].exams.model
        foreign_exam = exam_model.objects.create(
            tenant=other["tenant"],
            title="다른 학원 미응시 시험",
            exam_type=exam_model.ExamType.REGULAR,
            is_active=True,
        )
        foreign_attempt = ExamAttempt.objects.create(
            exam=foreign_exam,
            enrollment=other["enrollments"][0],
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={"status": "NOT_SUBMITTED"},
        )
        local_result.attempt = foreign_attempt
        local_result.save(update_fields=["attempt"])

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"][0]["exam_id"], local_exam.id)
        self.assertEqual(response.data["exams"][0]["total_score"], 80)
        self.assertIsNone(response.data["exams"][0]["meta_status"])
        self.assertEqual(response.data["exams"][0]["achievement"], "PASS")
        self.assertEqual(response.data["exam_summary"]["scored_count"], 1)

    def test_multi_lecture_exam_uses_result_enrollment_lecture_and_system_policy(self):
        self.data["lecture"].is_system = True
        self.data["lecture"].save(update_fields=["is_system"])
        normal_lecture = self.make_lecture(self.tenant, title="Ymath 정상반")
        normal_session = self.make_lecture_session(normal_lecture, order=2, title="정상반 2차시")
        normal_session.date = date(2026, 7, 2)
        normal_session.save(update_fields=["date"])
        normal_enrollment = self.make_enrollment(
            self.tenant,
            self.student,
            normal_lecture,
        )
        exam_model = self.data["lec_session"].exams.model
        shared_exam = exam_model.objects.create(
            tenant=self.tenant,
            title="여러 강의 공용 시험",
            exam_type=exam_model.ExamType.REGULAR,
            is_active=True,
            pass_score=60,
        )
        shared_exam.sessions.add(self.data["lec_session"], normal_session)
        Result.objects.create(
            target_type="exam",
            target_id=shared_exam.id,
            enrollment=normal_enrollment,
            total_score=88,
            max_score=100,
            submitted_at=timezone.now() - timedelta(minutes=1),
        )
        # 더 최신인 시스템 강의 결과가 같은 시험의 정상 결과를 suppress하면 안 된다.
        Result.objects.create(
            target_type="exam",
            target_id=shared_exam.id,
            enrollment=self.data["enrollments"][0],
            total_score=100,
            max_score=100,
            submitted_at=timezone.now(),
        )

        system_sessionless_exam = exam_model.objects.create(
            tenant=self.tenant,
            title="시스템 강의 세션없는 시험",
            exam_type=exam_model.ExamType.REGULAR,
            is_active=True,
            pass_score=60,
        )
        Result.objects.create(
            target_type="exam",
            target_id=system_sessionless_exam.id,
            enrollment=self.data["enrollments"][0],
            total_score=99,
            max_score=100,
            submitted_at=timezone.now() + timedelta(minutes=1),
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["title"] for row in response.data["exams"]], ["여러 강의 공용 시험"])
        self.assertEqual(response.data["exams"][0]["lecture_id"], normal_lecture.id)
        self.assertEqual(response.data["exams"][0]["session_id"], normal_session.id)

    def test_corrupt_enrollment_foreign_lecture_metadata_is_not_exposed(self):
        other = self.setup_full_tenant("student-grades-corrupt-enrollment", student_count=1)
        enrollment = self.data["enrollments"][0]
        enrollment.lecture = other["lecture"]
        enrollment.save(update_fields=["lecture"])
        exam_model = self.data["lec_session"].exams.model
        local_exam = exam_model.objects.create(
            tenant=self.tenant,
            title="로컬 세션없는 시험",
            exam_type=exam_model.ExamType.REGULAR,
            is_active=True,
            pass_score=60,
        )
        Result.objects.create(
            target_type="exam",
            target_id=local_exam.id,
            enrollment=enrollment,
            total_score=90,
            max_score=100,
            submitted_at=timezone.now(),
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"], [])
        self.assertEqual(response.data["exam_trend"], [])

    def test_homework_metadata_is_scoped_through_homework_and_session_tenant(self):
        score_model = self.data["enrollments"][0].homework_scores.model
        homework_model = score_model._meta.get_field("homework").remote_field.model
        local_homework = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title="로컬 정상 과제",
        )
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=self.data["lec_session"],
            homework=local_homework,
            score=10,
            max_score=10,
            passed=True,
        )

        other = self.setup_full_tenant("student-grades-corrupt-homework", student_count=1)
        foreign_homework = homework_model.objects.create(
            tenant=other["tenant"],
            homework_type=homework_model.HomeworkType.REGULAR,
            session=other["lec_session"],
            title="다른 학원 비공개 과제",
        )
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=other["lec_session"],
            homework=foreign_homework,
            score=100,
            max_score=100,
            passed=True,
        )
        # 같은 로컬 enrollment/homework를 쓰더라도 다른 tenant session의
        # 손상된 재시도 row가 정상 과제의 재응시 횟수를 부풀리면 안 된다.
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=other["lec_session"],
            homework=local_homework,
            attempt_index=2,
            score=10,
            max_score=10,
            passed=True,
        )
        system_lecture = self.make_lecture(self.tenant, title="시스템 영상함")
        system_lecture.is_system = True
        system_lecture.save(update_fields=["is_system"])
        system_session = self.make_lecture_session(
            system_lecture,
            order=1,
            title="시스템 차시",
        )
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=system_session,
            homework=local_homework,
            attempt_index=3,
            score=10,
            max_score=10,
            passed=True,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["title"] for row in response.data["homeworks"]],
            ["로컬 정상 과제"],
        )
        homework_row = response.data["homeworks"][0]
        self.assertEqual(homework_row["retake_count"], 1)
        self.assertEqual(homework_row["grading_mode"], "SCORE")
        self.assertFalse(homework_row["is_locked"])
        self.assertIsNotNone(homework_row["score_updated_at"])
        self.assertEqual(homework_row["session_order"], self.data["lec_session"].order)
        self.assertEqual(homework_row["session_type"], "REGULAR")

    def test_removed_and_recreated_same_title_homework_returns_only_replacement(self):
        score_model = self.data["enrollments"][0].homework_scores.model
        homework_model = score_model._meta.get_field("homework").remote_field.model
        removed = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title="동일 제목 재생성 과제",
            meta={
                "default_max_score": 30,
                "removed_from_session_at": "2026-08-08T22:52:04+09:00",
            },
        )
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=self.data["lec_session"],
            homework=removed,
            score=27,
            max_score=30,
            passed=False,
        )
        replacement = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title=removed.title,
            meta={"default_max_score": 30},
        )
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=self.data["lec_session"],
            homework=replacement,
            score=30,
            max_score=30,
            passed=True,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["homeworks"]), 1)
        row = response.data["homeworks"][0]
        self.assertEqual(row["homework_id"], replacement.id)
        self.assertEqual(row["title"], removed.title)
        self.assertEqual(row["score"], 30.0)

    def test_homework_history_defaults_to_latest_session_and_exposes_scope(self):
        score_model = self.data["enrollments"][0].homework_scores.model
        homework_model = score_model._meta.get_field("homework").remote_field.model
        session_model = self.data["lec_session"].__class__
        self.data["lec_session"].date = date(2026, 7, 1)
        self.data["lec_session"].save(update_fields=["date"])
        supplement = session_model.objects.create(
            lecture=self.data["lecture"],
            order=9,
            session_type="SUPPLEMENT",
            title="주말 보강",
            date=date(2026, 7, 8),
        )
        latest_regular = session_model.objects.create(
            lecture=self.data["lecture"],
            order=3,
            regular_order=2,
            session_type="REGULAR",
            title="2차시",
            date=date(2026, 7, 15),
        )
        for session, title in (
            (self.data["lec_session"], "첫 과제"),
            (supplement, "보강 과제"),
            (latest_regular, "최근 과제"),
        ):
            homework = homework_model.objects.create(
                tenant=self.tenant,
                homework_type=homework_model.HomeworkType.REGULAR,
                session=session,
                title=title,
            )
            score_model.objects.create(
                enrollment=self.data["enrollments"][0],
                session=session,
                homework=homework,
                score=10,
                max_score=100,
                passed=False,
            )

        response = self._get(self.student.id)

        self.assertEqual(
            [row["title"] for row in response.data["homeworks"]],
            ["최근 과제", "보강 과제", "첫 과제"],
        )
        rows = {row["title"]: row for row in response.data["homeworks"]}
        self.assertEqual(rows["최근 과제"]["session_regular_order"], 2)
        self.assertEqual(rows["최근 과제"]["session_type"], "REGULAR")
        self.assertEqual(rows["최근 과제"]["session_date"], "2026-07-15")
        self.assertEqual(rows["보강 과제"]["session_date"], "2026-07-08")
        self.assertEqual(rows["첫 과제"]["session_date"], "2026-07-01")
        self.assertIsNone(rows["보강 과제"]["session_regular_order"])
        self.assertEqual(rows["보강 과제"]["session_type"], "SUPPLEMENT")

    def test_assigned_homeworks_include_not_submitted_and_ungraded_rows(self):
        homework_assignment_model = apps.get_model("homework", "HomeworkAssignment")
        submission_model = apps.get_model("submissions", "Submission")
        score_model = self.data["enrollments"][0].homework_scores.model
        homework_model = score_model._meta.get_field("homework").remote_field.model
        enrollment = self.data["enrollments"][0]

        not_submitted = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title="완료형 미제출 과제",
            grading_mode=homework_model.GradingMode.COMPLETION,
        )
        homework_assignment_model.objects.create(
            tenant=self.tenant,
            homework=not_submitted,
            session=self.data["lec_session"],
            enrollment=enrollment,
        )
        score_model.objects.create(
            enrollment=enrollment,
            session=self.data["lec_session"],
            homework=not_submitted,
            score=None,
            max_score=1,
            passed=False,
            meta={"status": score_model.MetaStatus.NOT_SUBMITTED},
        )

        missing = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title="점수형 미제출 과제",
            grading_mode=homework_model.GradingMode.SCORE,
            meta={"default_max_score": 30},
        )
        homework_assignment_model.objects.create(
            tenant=self.tenant,
            homework=missing,
            session=self.data["lec_session"],
            enrollment=enrollment,
        )

        ungraded = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title="점수형 검사 전 과제",
            grading_mode=homework_model.GradingMode.SCORE,
            meta={"default_max_score": 30},
        )
        homework_assignment_model.objects.create(
            tenant=self.tenant,
            homework=ungraded,
            session=self.data["lec_session"],
            enrollment=enrollment,
        )
        submission_model.objects.create(
            tenant=self.tenant,
            user=self.admin_user,
            enrollment=enrollment,
            target_type=submission_model.TargetType.HOMEWORK,
            target_id=ungraded.id,
            source=submission_model.Source.ONLINE,
            status=submission_model.Status.SUBMITTED,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        rows = {row["title"]: row for row in response.data["homeworks"]}
        self.assertEqual(
            set(rows),
            {"완료형 미제출 과제", "점수형 미제출 과제", "점수형 검사 전 과제"},
        )

        not_submitted_row = rows["완료형 미제출 과제"]
        self.assertEqual(not_submitted_row["grading_mode"], "COMPLETION")
        self.assertIsNone(not_submitted_row["score"])
        self.assertEqual(not_submitted_row["max_score"], 1)
        self.assertEqual(not_submitted_row["meta_status"], "NOT_SUBMITTED")
        self.assertEqual(not_submitted_row["achievement"], "NOT_SUBMITTED")
        self.assertEqual(not_submitted_row["retake_count"], 1)
        self.assertIsNotNone(not_submitted_row["score_updated_at"])

        missing_row = rows["점수형 미제출 과제"]
        self.assertEqual(missing_row["grading_mode"], "SCORE")
        self.assertIsNone(missing_row["score"])
        self.assertEqual(missing_row["max_score"], 30)
        self.assertEqual(missing_row["meta_status"], "NOT_SUBMITTED")
        self.assertEqual(missing_row["achievement"], "NOT_SUBMITTED")
        self.assertEqual(missing_row["retake_count"], 0)
        self.assertIsNone(missing_row["score_updated_at"])

        ungraded_row = rows["점수형 검사 전 과제"]
        self.assertEqual(ungraded_row["grading_mode"], "SCORE")
        self.assertIsNone(ungraded_row["score"])
        self.assertEqual(ungraded_row["max_score"], 30)
        self.assertIsNone(ungraded_row["meta_status"])
        self.assertIsNone(ungraded_row["achievement"])
        self.assertEqual(ungraded_row["retake_count"], 0)
        self.assertIsNone(ungraded_row["score_updated_at"])

    def test_non_finite_homework_is_excluded_and_response_json_still_renders(self):
        score_model = self.data["enrollments"][0].homework_scores.model
        homework_model = score_model._meta.get_field("homework").remote_field.model
        homework = homework_model.objects.create(
            tenant=self.tenant,
            homework_type=homework_model.HomeworkType.REGULAR,
            session=self.data["lec_session"],
            title="손상된 무한대 과제",
        )
        score_model.objects.create(
            enrollment=self.data["enrollments"][0],
            session=self.data["lec_session"],
            homework=homework,
            score=float("inf"),
            max_score=10,
            passed=False,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["homeworks"], [])
        response.render()
        self.assertNotIn(b"Infinity", response.content)

    def test_latest_recorded_result_wins_same_exam_across_enrollments(self):
        other_lecture = self.make_lecture(self.tenant, title="Ymath 중복수강반")
        other_session = self.make_lecture_session(other_lecture, order=2, title="중복 2차시")
        other_enrollment = self.make_enrollment(
            self.tenant,
            self.student,
            other_lecture,
        )
        exam_model = self.data["lec_session"].exams.model
        exam = exam_model.objects.create(
            tenant=self.tenant,
            title="중복 결과 정렬 시험",
            exam_type=exam_model.ExamType.REGULAR,
            is_active=True,
            pass_score=60,
        )
        exam.sessions.add(self.data["lec_session"], other_session)
        stale = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=other_enrollment,
            total_score=10,
            max_score=100,
            submitted_at=None,
        )
        Result.objects.filter(id=stale.id).update(
            created_at=timezone.now() - timedelta(days=2),
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.data["enrollments"][0],
            total_score=90,
            max_score=100,
            submitted_at=timezone.now() - timedelta(days=1),
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["exams"]), 1)
        self.assertEqual(response.data["exams"][0]["total_score"], 90)

    def test_progression_excludes_invalid_numbers_but_keeps_zero_and_bonus_scores(self):
        def row(exam_id, score, max_score):
            return {
                "exam_id": exam_id,
                "enrollment_id": 1,
                "title": f"시험 {exam_id}",
                "total_score": score,
                "max_score": max_score,
                "recorded_at": f"2026-07-{exam_id:02d}T00:00:00+00:00",
            }

        trend, summary = _build_exam_progression([
            row(1, 0, 100),
            row(2, 120, 100),
            row(3, -1, 100),
            row(4, float("nan"), 100),
            row(5, float("inf"), 100),
            row(6, 50, float("inf")),
            row(7, 50, 0),
        ])

        self.assertEqual([point["score_pct"] for point in trend], [0.0, 120.0])
        self.assertEqual(summary["scored_count"], 2)
        self.assertEqual(summary["average_score_pct"], 60.0)
        self.assertEqual(summary["best_score_pct"], 120.0)

    def test_non_finite_result_is_excluded_and_response_json_still_renders(self):
        self._score(
            title="손상된 무한대 점수",
            order=2,
            score=float("inf"),
            max_score=100,
        )

        response = self._get(self.student.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["exams"], [])
        self.assertEqual(response.data["exam_trend"], [])
        response.render()
        self.assertNotIn(b"Infinity", response.content)

    def test_student_grades_query_count_does_not_scale_per_exam(self):
        self._score(title="쿼리 테스트 1회", order=2, score=70, max_score=100)
        with CaptureQueriesContext(connection) as initial_queries:
            first_response = self._get(self.student.id)
        self.assertEqual(first_response.status_code, 200)

        for order in range(3, 8):
            self._score(
                title=f"쿼리 테스트 {order - 1}회",
                order=order,
                score=70 + order,
                max_score=100,
            )
        with CaptureQueriesContext(connection) as expanded_queries:
            expanded_response = self._get(self.student.id)

        self.assertEqual(expanded_response.status_code, 200)
        self.assertEqual(len(expanded_response.data["exam_trend"]), 6)
        self.assertLessEqual(len(expanded_queries), len(initial_queries) + 2)
