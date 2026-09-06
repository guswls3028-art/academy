from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.core.models import Tenant, TenantMembership
from apps.domains.results.models import ScoreEditDraft
from apps.domains.results.guards.score_edit_lease_guard import (
    ScoreEditLeaseConflict,
    ScoreEditLeaseStale,
    invalidate_score_edit_leases_for_exam,
    require_homework_score_edit_lease,
    require_score_edit_lease,
)
from apps.domains.results.views.score_draft_view import (
    ScoreDraftCommitView,
    ScoreDraftView,
)


User = get_user_model()
Lecture = apps.get_model("lectures", "Lecture")
Session = apps.get_model("lectures", "Session")
Exam = apps.get_model("exams", "Exam")


class ScoreDraftEditLeaseTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.tenant = Tenant.objects.create(
            name="Score Lease Academy",
            code="score-lease",
            is_active=True,
        )
        self.admin_a = self._staff("score-lease-a")
        self.admin_b = self._staff("score-lease-b")
        lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="Score Lease Lecture",
            name="Score Lease Lecture",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=lecture,
            order=1,
            title="Session 1",
        )

    def _staff(self, username):
        user = User.objects.create_user(
            username=username,
            password="pw1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=user,
            role="admin",
        )
        return user

    def _request(self, method, user, client_id, data=None):
        request = getattr(self.factory, method)(
            "/api/v1/results/admin/sessions/1/score-draft/",
            data or {},
            format="json",
            HTTP_X_SCORE_EDITOR_CLIENT=client_id,
        )
        request.tenant = self.tenant
        force_authenticate(request, user=user)
        request.user = user
        return request

    def _put(
        self,
        user,
        client_id,
        changes=None,
        *,
        session=None,
        acknowledge_stale=False,
        active_cell=None,
        take_over_same_user=False,
    ):
        return ScoreDraftView.as_view()(
            self._request(
                "put",
                user,
                client_id,
                {
                    "changes": changes or [],
                    "acknowledge_stale": acknowledge_stale,
                    "active_cell": active_cell,
                    "take_over_same_user": take_over_same_user,
                },
            ),
            session_id=(session or self.session).id,
        )

    def _get(self, user, client_id, *, session=None):
        return ScoreDraftView.as_view()(
            self._request("get", user, client_id),
            session_id=(session or self.session).id,
        )

    def _commit(self, user, client_id, *, release_lease):
        return ScoreDraftCommitView.as_view()(
            self._request(
                "post",
                user,
                client_id,
                {"release_lease": release_lease},
            ),
            session_id=self.session.id,
        )

    def test_same_account_tabs_can_select_disjoint_homework_cells_and_see_presence(self):
        first_cell = {
            "type": "homework",
            "enrollmentId": 21,
            "homeworkId": 31,
        }
        second_cell = {
            "type": "homework",
            "enrollmentId": 22,
            "homeworkId": 31,
        }
        first_change = {**first_cell, "score": 80}
        second_change = {**second_cell, "score": 90}

        first = self._put(
            self.admin_a,
            "tab-a",
            [first_change],
            active_cell=first_cell,
        )
        second = self._put(
            self.admin_a,
            "tab-a-2",
            [second_change],
            active_cell=second_cell,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.data["active_editors"],
            [
                {
                    "client_id": "tab-a",
                    "editor_user_id": self.admin_a.id,
                    "editor_name": "score-lease-a",
                    "active_cell": first_cell,
                }
            ],
        )
        self.assertEqual(
            ScoreEditDraft.objects.filter(
                session=self.session,
                editor_user=self.admin_a,
            ).count(),
            2,
        )
        for client_id, enrollment_id in (("tab-a", 21), ("tab-a-2", 22)):
            request = self._request("patch", self.admin_a, client_id)
            with transaction.atomic():
                self.assertEqual(
                    require_homework_score_edit_lease(
                        request,
                        session_id=self.session.id,
                        enrollment_id=enrollment_id,
                        homework_id=31,
                    ).id,
                    self.session.id,
                )

    def test_same_homework_cell_presence_conflicts_but_other_cells_remain_available(self):
        occupied_cell = {
            "type": "homework",
            "enrollmentId": 21,
            "homeworkId": 31,
        }
        self.assertEqual(
            self._put(
                self.admin_a,
                "tab-a",
                active_cell=occupied_cell,
            ).status_code,
            200,
        )

        conflict = self._put(
            self.admin_b,
            "tab-b",
            active_cell=occupied_cell,
        )
        available = self._put(
            self.admin_b,
            "tab-b",
            active_cell={
                **occupied_cell,
                "enrollmentId": 22,
            },
        )

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "SCORE_EDIT_LOCKED")
        self.assertEqual(available.status_code, 200)

    def test_exam_presence_blocks_only_the_same_score_cell(self):
        occupied_cell = {
            "type": "exam",
            "enrollmentId": 21,
            "examId": 11,
            "sub": "subjective",
        }
        self.assertEqual(
            self._put(
                self.admin_a,
                "tab-a",
                active_cell=occupied_cell,
            ).status_code,
            200,
        )

        conflict = self._put(
            self.admin_b,
            "tab-b",
            active_cell=occupied_cell,
        )
        available = self._put(
            self.admin_b,
            "tab-b",
            active_cell={**occupied_cell, "enrollmentId": 22},
        )

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "SCORE_EDIT_LOCKED")
        self.assertEqual(available.status_code, 200)
        self.assertEqual(available.data["active_editors"][0]["active_cell"], occupied_cell)

    def test_active_homework_cell_does_not_block_subjective_score_save(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Subjective Exam",
            exam_type=Exam.ExamType.REGULAR,
        )
        exam.sessions.add(self.session)
        homework_cell = {
            "type": "homework",
            "enrollmentId": 21,
            "homeworkId": 31,
        }
        subjective_cell = {
            "type": "exam",
            "enrollmentId": 22,
            "examId": exam.id,
            "sub": "subjective",
        }
        subjective_change = {
            "type": "examSubjective",
            "examId": exam.id,
            "enrollmentId": 22,
            "score": 7,
        }
        self.assertEqual(
            self._put(
                self.admin_a,
                "homework-tab",
                active_cell=homework_cell,
            ).status_code,
            200,
        )
        self.assertEqual(
            self._put(
                self.admin_b,
                "subjective-tab",
                [subjective_change],
                active_cell=subjective_cell,
            ).status_code,
            200,
        )

        request = self._request("patch", self.admin_b, "subjective-tab")
        with transaction.atomic():
            self.assertEqual(
                require_score_edit_lease(
                    request,
                    session_id=self.session.id,
                    exam_id=exam.id,
                    target_cell=subjective_cell,
                ).id,
                self.session.id,
            )

    def test_draft_and_commit_reject_ambiguous_boolean_values(self):
        put_response = self._put(
            self.admin_a,
            "tab-invalid-put",
            acknowledge_stale="sometimes",
        )
        commit_response = self._commit(
            self.admin_a,
            "tab-invalid-commit",
            release_lease="sometimes",
        )
        handoff_response = self._put(
            self.admin_a,
            "tab-invalid-handoff",
            take_over_same_user="sometimes",
        )

        self.assertEqual(put_response.status_code, 400)
        self.assertEqual(commit_response.status_code, 400)
        self.assertEqual(handoff_response.status_code, 400)

    def test_disjoint_homework_cells_coexist_but_same_cell_conflicts(self):
        first = {
            "type": "homework",
            "enrollmentId": 21,
            "homeworkId": 31,
            "score": 80,
        }
        disjoint = {
            "type": "homework",
            "enrollmentId": 22,
            "homeworkId": 31,
            "score": 90,
        }
        self.assertEqual(self._put(self.admin_a, "tab-a", [first]).status_code, 200)
        self.assertEqual(self._put(self.admin_b, "tab-b", [disjoint]).status_code, 200)

        request_a = self._request("patch", self.admin_a, "tab-a")
        request_b = self._request("patch", self.admin_b, "tab-b")
        with transaction.atomic():
            self.assertEqual(
                require_homework_score_edit_lease(
                    request_a,
                    session_id=self.session.id,
                    enrollment_id=21,
                    homework_id=31,
                ).id,
                self.session.id,
            )
        with transaction.atomic():
            self.assertEqual(
                require_homework_score_edit_lease(
                    request_b,
                    session_id=self.session.id,
                    enrollment_id=22,
                    homework_id=31,
                ).id,
                self.session.id,
            )
        with self.assertRaises(ScoreEditLeaseConflict):
            with transaction.atomic():
                require_homework_score_edit_lease(
                    request_b,
                    session_id=self.session.id,
                    enrollment_id=21,
                    homework_id=31,
                )

        conflict = self._put(self.admin_b, "tab-b", [first])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "SCORE_EDIT_LOCKED")

    def test_exam_changes_are_scoped_to_their_exact_cells(self):
        self.assertEqual(self._put(self.admin_a, "tab-a").status_code, 200)
        self.assertEqual(self._put(self.admin_b, "tab-b").status_code, 200)
        exam_change_a = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 21,
            "score": 70,
        }
        exam_change_b = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 22,
            "score": 80,
        }
        self.assertEqual(
            self._put(self.admin_a, "tab-a", [exam_change_a]).status_code,
            200,
        )
        available = self._put(self.admin_b, "tab-b", [exam_change_b])
        conflict = self._put(self.admin_b, "tab-b", [exam_change_a])
        self.assertEqual(available.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "SCORE_EDIT_LOCKED")

    def test_same_account_mobile_score_handoff_preserves_and_fences_old_draft(self):
        desktop_change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 21,
            "score": 70,
        }
        mobile_change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 21,
            "score": 80,
        }
        self.assertEqual(
            self._put(self.admin_a, "desktop", [desktop_change]).status_code,
            200,
        )

        handoff = self._put(
            self.admin_a,
            "iphone",
            [mobile_change],
            take_over_same_user=True,
        )

        self.assertEqual(handoff.status_code, 200)
        desktop_draft = ScoreEditDraft.objects.get(
            session=self.session,
            editor_user=self.admin_a,
            client_id="desktop",
        )
        self.assertTrue(desktop_draft.payload["invalidated"])
        self.assertEqual(
            desktop_draft.payload["invalidated_reason"],
            "SAME_ACCOUNT_HANDOFF",
        )
        self.assertEqual(desktop_draft.payload["changes"], [desktop_change])

        mobile_request = self._request("patch", self.admin_a, "iphone")
        with transaction.atomic():
            self.assertEqual(
                require_score_edit_lease(
                    mobile_request,
                    session_id=self.session.id,
                ).id,
                self.session.id,
            )

        desktop_request = self._request("patch", self.admin_a, "desktop")
        with self.assertRaises(ScoreEditLeaseStale):
            with transaction.atomic():
                require_score_edit_lease(
                    desktop_request,
                    session_id=self.session.id,
                )

    def test_active_changed_draft_is_recoverable_only_by_same_account_new_device(self):
        desktop_change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 21,
            "score": 70,
        }
        self.assertEqual(
            self._put(self.admin_a, "desktop", [desktop_change]).status_code,
            200,
        )

        same_account = self._get(self.admin_a, "iphone")
        other_account = self._get(self.admin_b, "iphone")

        self.assertEqual(same_account.status_code, 200)
        self.assertEqual(same_account.data["changes"], [desktop_change])
        self.assertFalse(same_account.data["stale"])
        self.assertEqual(other_account.status_code, 200)
        self.assertEqual(other_account.data["changes"], [])

    def test_other_account_can_edit_disjoint_exam_cell_but_not_same_cell(self):
        change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 21,
            "score": 70,
        }
        self.assertEqual(
            self._put(self.admin_a, "desktop", [change]).status_code,
            200,
        )

        available = self._put(
            self.admin_b,
            "iphone",
            [{**change, "enrollmentId": 22}],
            take_over_same_user=True,
        )
        conflict = self._put(
            self.admin_b,
            "iphone",
            [change],
            take_over_same_user=True,
        )

        self.assertEqual(available.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "SCORE_EDIT_LOCKED")

    def test_autosave_commit_keeps_lease_until_explicit_release(self):
        change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 22,
            "score": 74,
        }
        self.assertEqual(self._put(self.admin_a, "tab-a", [change]).status_code, 200)
        self.assertEqual(
            self._commit(self.admin_a, "tab-a", release_lease=False).status_code,
            204,
        )

        draft = ScoreEditDraft.objects.get(
            session=self.session,
            editor_user=self.admin_a,
        )
        self.assertEqual(draft.payload["changes"], [])
        self.assertEqual(self._put(self.admin_b, "tab-b").status_code, 200)
        other_tab = self._get(self.admin_a, "tab-a-2")
        self.assertEqual(other_tab.status_code, 200)
        self.assertEqual(other_tab.data["changes"], [])

        self.assertEqual(
            self._commit(self.admin_a, "tab-a", release_lease=True).status_code,
            204,
        )
        self.assertFalse(
            ScoreEditDraft.objects.filter(
                session=self.session,
                editor_user=self.admin_a,
            ).exists()
        )
        self.assertEqual(self._put(self.admin_b, "tab-b").status_code, 200)

    def test_expired_lease_can_be_taken_over(self):
        change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 22,
            "score": 74,
        }
        self.assertEqual(self._put(self.admin_a, "tab-a", [change]).status_code, 200)
        ScoreEditDraft.objects.filter(
            session=self.session,
            editor_user=self.admin_a,
        ).update(updated_at=timezone.now() - timedelta(minutes=3))

        self.assertEqual(self._put(self.admin_b, "tab-b").status_code, 200)

    def test_expired_empty_lease_can_be_taken_over_by_same_account_new_tab(self):
        self.assertEqual(self._put(self.admin_a, "tab-a").status_code, 200)
        ScoreEditDraft.objects.filter(
            session=self.session,
            editor_user=self.admin_a,
        ).update(updated_at=timezone.now() - timedelta(minutes=3))

        response = self._put(self.admin_a, "tab-b")

        self.assertEqual(response.status_code, 200)
        draft = ScoreEditDraft.objects.get(
            session=self.session,
            editor_user=self.admin_a,
        )
        self.assertEqual(draft.payload["client_id"], "tab-b")
        self.assertEqual(draft.payload["changes"], [])

    def test_expired_changed_lease_is_recoverable_by_same_account_new_tab(self):
        change = {
            "type": "examTotal",
            "examId": 11,
            "enrollmentId": 22,
            "score": 74,
        }
        self.assertEqual(self._put(self.admin_a, "tab-a", [change]).status_code, 200)
        ScoreEditDraft.objects.filter(
            session=self.session,
            editor_user=self.admin_a,
        ).update(updated_at=timezone.now() - timedelta(minutes=3))

        recovery = self._get(self.admin_a, "tab-b")
        blocked_without_choice = self._put(self.admin_a, "tab-b")
        restored = self._put(
            self.admin_a,
            "tab-b",
            [change],
            acknowledge_stale=True,
            take_over_same_user=True,
        )

        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.data["changes"], [change])
        self.assertEqual(blocked_without_choice.status_code, 409)
        self.assertEqual(restored.status_code, 200)
        old_draft = ScoreEditDraft.objects.get(
            session=self.session,
            editor_user=self.admin_a,
            client_id="tab-a",
        )
        self.assertTrue(old_draft.payload["invalidated"])
        self.assertEqual(old_draft.payload["changes"], [change])
        new_draft = ScoreEditDraft.objects.get(
            session=self.session,
            editor_user=self.admin_a,
            client_id="tab-b",
        )
        self.assertEqual(new_draft.payload["changes"], [change])

    def test_expired_invalidated_empty_lease_does_not_strand_new_device(self):
        active_cell = {
            "type": "homework",
            "enrollmentId": 21,
            "homeworkId": 31,
        }
        self.assertEqual(
            self._put(self.admin_a, "old-device", active_cell=active_cell).status_code,
            200,
        )
        draft = ScoreEditDraft.objects.get(session=self.session, editor_user=self.admin_a)
        draft.payload = {**draft.payload, "invalidated": True, "invalidated_reason": "AUTOMATIC_GRADING_COMPLETED"}
        draft.save(update_fields=["payload"])
        ScoreEditDraft.objects.filter(id=draft.id).update(
            updated_at=timezone.now() - timedelta(minutes=3),
        )

        response = self._put(self.admin_a, "new-device")

        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.client_id, "new-device")
        self.assertEqual(draft.payload["changes"], [])
        self.assertIsNone(draft.payload["active_cell"])
        self.assertNotIn("invalidated", draft.payload)

    def test_legacy_list_payload_remains_readable(self):
        changes = [{"type": "homework", "enrollmentId": 3, "homeworkId": 4, "score": 5}]
        ScoreEditDraft.objects.create(
            session=self.session,
            tenant=self.tenant,
            editor_user=self.admin_a,
            payload=changes,
        )

        response = self._get(self.admin_a, "tab-a")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["changes"], changes)

    def test_score_mutation_guard_requires_matching_active_client(self):
        missing = self._request("patch", self.admin_a, "tab-a-2")
        with self.assertRaises(ScoreEditLeaseConflict):
            with transaction.atomic():
                require_score_edit_lease(missing, session_id=self.session.id)

        self.assertEqual(self._put(self.admin_a, "tab-a").status_code, 200)
        matching = self._request("patch", self.admin_a, "tab-a")
        with transaction.atomic():
            self.assertEqual(
                require_score_edit_lease(matching, session_id=self.session.id).id,
                self.session.id,
            )

        other_tab = self._request("patch", self.admin_a, "tab-a-2")
        with self.assertRaises(ScoreEditLeaseConflict):
            with transaction.atomic():
                require_score_edit_lease(other_tab, session_id=self.session.id)

    def test_shared_exam_sessions_still_conflict_only_on_same_score_cell(self):
        sibling = Session.objects.create(
            lecture=self.session.lecture,
            order=2,
            title="Session 2",
        )
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Shared Exam",
            exam_type=Exam.ExamType.REGULAR,
        )
        exam.sessions.add(self.session, sibling)

        change = {
            "type": "examTotal",
            "examId": exam.id,
            "enrollmentId": 22,
            "score": 74,
        }

        self.assertEqual(
            self._put(self.admin_a, "tab-a", [change]).status_code,
            200,
        )

        available = self._put(
            self.admin_b,
            "tab-b",
            [{**change, "enrollmentId": 23}],
            session=sibling,
        )
        conflict = self._put(
            self.admin_b,
            "tab-b",
            [change],
            session=sibling,
        )
        self.assertEqual(available.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "SCORE_EDIT_LOCKED")

    def test_authoritative_update_preserves_and_stales_manual_draft(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="Automatically Graded Exam",
            exam_type=Exam.ExamType.REGULAR,
        )
        exam.sessions.add(self.session)
        change = {
            "type": "examTotal",
            "examId": exam.id,
            "enrollmentId": 22,
            "score": 74.5,
        }
        self.assertEqual(self._put(self.admin_a, "tab-a", [change]).status_code, 200)

        with transaction.atomic():
            self.assertEqual(
                invalidate_score_edit_leases_for_exam(
                    exam=exam,
                    tenant=self.tenant,
                    reason="AUTOMATIC_GRADING_COMPLETED",
                ),
                1,
            )

        recovery = self._get(self.admin_a, "tab-a")
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.data["changes"], [change])
        self.assertTrue(recovery.data["stale"])

        mutation = self._request("patch", self.admin_a, "tab-a")
        with self.assertRaises(ScoreEditLeaseStale):
            with transaction.atomic():
                require_score_edit_lease(
                    mutation,
                    session_id=self.session.id,
                    exam_id=exam.id,
                )

        rejected = self._put(self.admin_a, "tab-a", [change])
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.data["code"], "SCORE_EDIT_STALE")
        empty_heartbeat = self._put(self.admin_a, "tab-a")
        self.assertEqual(empty_heartbeat.status_code, 409)
        self.assertEqual(empty_heartbeat.data["code"], "SCORE_EDIT_STALE")
        duplicated_tab = self._put(self.admin_a, "tab-a-2")
        self.assertEqual(duplicated_tab.status_code, 200)
        self.assertEqual(duplicated_tab.data["changes"], [])
        stale_commit = self._commit(
            self.admin_a,
            "tab-a",
            release_lease=False,
        )
        self.assertEqual(stale_commit.status_code, 409)
        self.assertEqual(stale_commit.data["code"], "SCORE_EDIT_STALE")

        restored = self._put(
            self.admin_a,
            "tab-a",
            [change],
            acknowledge_stale=True,
        )
        self.assertEqual(restored.status_code, 200)
