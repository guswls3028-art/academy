from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.apps import apps
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.enrollment.models import Enrollment
from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework_results.models import Homework, HomeworkScore
from apps.domains.homework_results.views.homework_score_viewset import HomeworkScoreViewSet
from apps.domains.lectures.models import Lecture, Session
from apps.domains.students.models import Student


User = get_user_model()
ScoreEditDraft = apps.get_model("results", "ScoreEditDraft")


class HomeworkQuickPatchScopeTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="Homework Scope",
            code="hwscope",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username="hwscope-admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="admin")

        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Lecture",
            name="Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(lecture=self.lecture, order=1, title="1회차")
        self.other_session = Session.objects.create(lecture=self.lecture, order=2, title="2회차")
        self.homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="Daily homework",
        )
        self.assigned_enrollment = self._create_enrollment("assigned")
        self.unassigned_enrollment = self._create_enrollment("unassigned")
        HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
        )
        ScoreEditDraft.objects.create(
            session=self.session,
            tenant=self.tenant,
            editor_user=self.admin,
            payload={"client_id": "test-score-tab", "changes": []},
        )
        ScoreEditDraft.objects.create(
            session=self.other_session,
            tenant=self.tenant,
            editor_user=self.admin,
            payload={"client_id": "test-score-tab", "changes": []},
        )

    def _create_enrollment(self, suffix: str) -> Enrollment:
        user = User.objects.create_user(
            username=f"hwscope-{suffix}",
            password="test1234",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=user,
            name=f"Student {suffix}",
            ps_number=f"HW-{suffix}",
            omr_code=f"HW{suffix.upper()}"[:8],
        )
        return Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=student,
            status="ACTIVE",
        )

    def _quick_patch(self, data, *, expected_status=200):
        request = self.factory.patch(
            "/homework/scores/quick/",
            data,
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="test-score-tab",
            HTTP_X_SCORE_SESSION_ID=str(data.get("session_id") or ""),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = HomeworkScoreViewSet.as_view({"patch": "quick_patch"})(request)
        self.assertEqual(response.status_code, expected_status, response.data)
        return response

    def _partial_update(self, score, data, *, expected_status=200):
        request = self.factory.patch(
            f"/homework/scores/{score.id}/",
            data,
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="test-score-tab",
            HTTP_X_SCORE_SESSION_ID=str(score.session_id),
        )
        request.tenant = self.tenant
        force_authenticate(request, user=self.admin)
        response = HomeworkScoreViewSet.as_view({"patch": "partial_update"})(request, pk=score.id)
        self.assertEqual(response.status_code, expected_status, response.data)
        return response

    def test_rejects_unassigned_enrollment_without_side_effects(self):
        self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.unassigned_enrollment.id,
                "score": 80,
            },
            expected_status=400,
        )

        self.assertFalse(
            HomeworkScore.objects.filter(
                homework=self.homework,
                enrollment=self.unassigned_enrollment,
            ).exists()
        )

    def test_rejects_session_mismatch_without_side_effects(self):
        self._quick_patch(
            {
                "session_id": self.other_session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 80,
            },
            expected_status=400,
        )

        self.assertFalse(
            HomeworkScore.objects.filter(
                homework=self.homework,
                enrollment=self.assigned_enrollment,
            ).exists()
        )

    def test_accepts_assigned_enrollment_for_homework_session(self):
        response = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 80,
            }
        )

        score = HomeworkScore.objects.get(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
        )
        self.assertEqual(response.data["id"], score.id)
        self.assertEqual(score.score, 80)

    def test_quick_patch_uses_cell_version_cas_and_preserves_server_value_on_conflict(self):
        first = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 70,
                "expected_updated_at": None,
            }
        )
        original_version = first.data["updated_at"]

        winner = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 80,
                "expected_updated_at": original_version,
            }
        )
        self.assertNotEqual(winner.data["updated_at"], original_version)

        conflict = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 90,
                "expected_updated_at": original_version,
            },
            expected_status=409,
        )

        score = HomeworkScore.objects.get(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
        )
        self.assertEqual(score.score, 80)
        self.assertEqual(conflict.data["code"], "SCORE_CELL_CONFLICT")
        self.assertEqual(conflict.data["server_value"]["score"], 80)
        self.assertEqual(
            conflict.data["server_value"]["updated_at"],
            winner.data["updated_at"],
        )
        self.assertEqual(conflict.data["expected_updated_at"], original_version)

    def test_quick_patch_expected_empty_cell_conflicts_with_existing_score(self):
        existing = HomeworkScore.objects.create(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
            score=65,
            max_score=100,
        )

        conflict = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 75,
                "expected_updated_at": None,
            },
            expected_status=409,
        )

        existing.refresh_from_db()
        self.assertEqual(existing.score, 65)
        self.assertEqual(conflict.data["server_value"]["score"], 65)

    def test_uses_configured_homework_max_score_when_request_omits_it(self):
        self.homework.meta = {"default_max_score": 43}
        self.homework.save(update_fields=["meta", "updated_at"])

        response = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 41,
            }
        )

        score = HomeworkScore.objects.get(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
        )
        self.assertEqual(response.data["max_score"], 43.0)
        self.assertEqual(score.max_score, 43.0)

    def test_quick_patch_uses_homework_specific_cutline(self):
        self.homework.cutline_mode = Homework.CutlineMode.PERCENT
        self.homework.cutline_value = 70
        self.homework.round_unit_percent = 5
        self.homework.save(
            update_fields=[
                "cutline_mode",
                "cutline_value",
                "round_unit_percent",
                "updated_at",
            ]
        )

        self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 75,
            }
        )

        score = HomeworkScore.objects.get(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
        )
        self.assertTrue(score.passed)
        self.assertFalse(score.clinic_required)

    def test_rejects_score_above_configured_homework_max_score(self):
        self.homework.meta = {"default_max_score": 43}
        self.homework.save(update_fields=["meta", "updated_at"])

        self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 44,
            },
            expected_status=400,
        )

        self.assertFalse(
            HomeworkScore.objects.filter(
                homework=self.homework,
                enrollment=self.assigned_enrollment,
            ).exists()
        )

    def test_completion_homework_accepts_only_incomplete_or_complete(self):
        self.homework.grading_mode = Homework.GradingMode.COMPLETION
        self.homework.save(update_fields=["grading_mode", "updated_at"])

        incomplete = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 0,
            }
        )
        self.assertEqual(incomplete.data["max_score"], 1.0)
        self.assertFalse(incomplete.data["passed"])

        complete = self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 1,
            }
        )
        self.assertEqual(complete.data["score"], 1.0)
        self.assertTrue(complete.data["passed"])

        self._quick_patch(
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.assigned_enrollment.id,
                "score": 0.5,
            },
            expected_status=400,
        )
        score = HomeworkScore.objects.get(
            homework=self.homework,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
        )
        self.assertEqual(score.score, 1.0)

    def test_partial_update_does_not_move_score_relationships(self):
        score = HomeworkScore.objects.create(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
            score=70,
        )

        self._partial_update(
            score,
            {
                "session": self.other_session.id,
                "homework": self.homework.id + 9999,
                "score": 75,
            },
        )

        score.refresh_from_db()
        self.assertEqual(score.session_id, self.session.id)
        self.assertEqual(score.homework_id, self.homework.id)
        self.assertEqual(score.score, 75)

    def test_partial_update_status_not_submitted_persists_meta_and_clears_score(self):
        score = HomeworkScore.objects.create(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
            score=70,
            max_score=100,
        )

        self._partial_update(score, {"status": HomeworkScore.MetaStatus.NOT_SUBMITTED})

        score.refresh_from_db()
        self.assertIsNone(score.score)
        self.assertIsNone(score.max_score)
        self.assertEqual(score.meta, {"status": HomeworkScore.MetaStatus.NOT_SUBMITTED})

    def test_partial_update_status_null_clears_not_submitted_meta(self):
        score = HomeworkScore.objects.create(
            homework=self.homework,
            session=self.session,
            enrollment=self.assigned_enrollment,
            attempt_index=1,
            score=None,
            max_score=None,
            meta={"status": HomeworkScore.MetaStatus.NOT_SUBMITTED},
        )

        self._partial_update(score, {"status": None, "score": 55})

        score.refresh_from_db()
        self.assertEqual(score.score, 55)
        self.assertIsNone(score.meta)


class HomeworkQuickPatchCreateRaceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(name="CAS Race", code="casrace", is_active=True)
        self.admin = User.objects.create_user(
            username="casrace-admin",
            password="test1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(tenant=self.tenant, user=self.admin, role="admin")
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="CAS Race Lecture",
            name="CAS Race Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(lecture=self.lecture, order=1, title="1회차")
        self.homework = Homework.objects.create(
            tenant=self.tenant,
            session=self.session,
            title="CAS Race Homework",
        )
        student_user = User.objects.create_user(
            username="casrace-student",
            password="test1234",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="CAS Race Student",
            ps_number="CAS-RACE",
            omr_code="CASRACE",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=student,
            status="ACTIVE",
        )
        HomeworkAssignment.objects.create(
            tenant=self.tenant,
            homework=self.homework,
            session=self.session,
            enrollment=self.enrollment,
        )
        ScoreEditDraft.objects.create(
            session=self.session,
            tenant=self.tenant,
            editor_user=self.admin,
            payload={"client_id": "cas-race-tab", "changes": []},
        )

    def _request(self, score):
        close_old_connections()
        tenant = Tenant.objects.get(pk=self.tenant.pk)
        user = User.objects.get(pk=self.admin.pk)
        request = self.factory.patch(
            "/homework/scores/quick/",
            {
                "session_id": self.session.id,
                "homework_id": self.homework.id,
                "enrollment_id": self.enrollment.id,
                "score": score,
                "expected_updated_at": None,
            },
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT="cas-race-tab",
            HTTP_X_SCORE_SESSION_ID=str(self.session.id),
        )
        request.tenant = tenant
        force_authenticate(request, user=user)
        try:
            response = HomeworkScoreViewSet.as_view({"patch": "quick_patch"})(request)
            return response.status_code, dict(response.data)
        finally:
            close_old_connections()

    @skipUnless(connection.vendor == "postgresql", "unique create race requires PostgreSQL")
    def test_expected_empty_cell_create_race_allows_one_winner_and_one_conflict(self):
        # The lease contract is covered separately. The canonical assignment lock
        # now serializes the empty-cell create window itself, so forcing both calls
        # to pause inside create would deadlock the test behind that intended lock.
        with patch(
            "apps.domains.homework_results.views.homework_score_viewset."
            "require_homework_score_edit_lease"
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(self._request, [71, 82]))

        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        conflict = next(data for status, data in results if status == 409)
        score = HomeworkScore.objects.get(
            homework=self.homework,
            session=self.session,
            enrollment=self.enrollment,
            attempt_index=1,
        )
        self.assertEqual(score.score, winner["score"])
        self.assertEqual(conflict["code"], "SCORE_CELL_CONFLICT")
        self.assertEqual(conflict["server_value"]["score"], winner["score"])
        self.assertEqual(HomeworkScore.objects.count(), 1)
