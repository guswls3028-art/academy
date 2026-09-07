from __future__ import annotations

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.core.models import Tenant, TenantMembership
from apps.domains.enrollment.models import Enrollment
from apps.domains.exams.models import (
    AnswerKey,
    Exam,
    ExamEnrollment,
    ExamQuestion,
    Sheet,
)
from apps.domains.lectures.models import Lecture, Session
from apps.domains.results.models import ExamAttempt, Result, ResultItem
from apps.domains.results.services.manual_exam_grading import (
    ManualExamGradingError,
    apply_manual_grading,
    build_manual_grading_sheet,
    plan_manual_grading,
)
from apps.domains.results.utils.ranking import compute_exam_rankings
from apps.domains.results.utils.initial_exam_score import load_initial_exam_scores
from apps.domains.students.models import Student


User = get_user_model()


class ManualExamGradingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Manual Grading",
            code="manual-grading",
            is_active=True,
        )
        self.admin = User.objects.create_user(
            username="manual-grading-admin",
            password="pw1234",
            tenant=self.tenant,
            is_staff=True,
        )
        TenantMembership.ensure_active(
            tenant=self.tenant,
            user=self.admin,
            role="admin",
        )
        self.lecture = Lecture.objects.create(
            tenant=self.tenant,
            title="대수",
            name="대수",
            subject="MATH",
        )
        self.session = Session.objects.create(
            lecture=self.lecture,
            order=1,
            title="1차시",
        )
        student_user = User.objects.create_user(
            username="manual-grading-student",
            password="pw1234",
            tenant=self.tenant,
        )
        student = Student.objects.create(
            tenant=self.tenant,
            user=student_user,
            name="김학생",
            ps_number="MG-001",
            omr_code="00000001",
        )
        self.enrollment = Enrollment.objects.create(
            tenant=self.tenant,
            lecture=self.lecture,
            student=student,
            status="ACTIVE",
        )

    def _exam(
        self,
        *,
        grading_mode: str,
        manual_method: str,
    ) -> tuple[Exam, ExamQuestion, ExamQuestion]:
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="직접 채점 시험",
            exam_type=Exam.ExamType.REGULAR,
            grading_mode=grading_mode,
            manual_grading_method=manual_method,
            max_score=100,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(
            exam=exam,
            enrollment=self.enrollment,
        )
        sheet = Sheet.objects.create(
            exam=exam,
            total_questions=2,
            choice_count=(
                2
                if grading_mode == Exam.GradingMode.CHOICE
                else 1
                if grading_mode == Exam.GradingMode.MIXED
                else 0
            ),
            essay_count=(
                0
                if grading_mode == Exam.GradingMode.CHOICE
                else 1
                if grading_mode == Exam.GradingMode.MIXED
                else 2
            ),
        )
        first = ExamQuestion.objects.create(
            sheet=sheet,
            number=1,
            score=40,
            question_kind=(
                ExamQuestion.QuestionKind.CHOICE
                if grading_mode
                in {
                    Exam.GradingMode.CHOICE,
                    Exam.GradingMode.MIXED,
                }
                else ExamQuestion.QuestionKind.ESSAY
            ),
        )
        second = ExamQuestion.objects.create(
            sheet=sheet,
            number=2,
            score=60,
            question_kind=(
                ExamQuestion.QuestionKind.CHOICE
                if grading_mode == Exam.GradingMode.CHOICE
                else ExamQuestion.QuestionKind.ESSAY
            ),
        )
        return exam, first, second

    def test_exam_without_questions_returns_quick_start_sheet(self):
        exam = Exam.objects.create(
            tenant=self.tenant,
            title="문항 없는 시험",
            exam_type=Exam.ExamType.REGULAR,
            grading_mode=Exam.GradingMode.CHOICE,
            manual_grading_method=Exam.ManualGradingMethod.CORRECTNESS,
            max_score=100,
        )
        exam.sessions.add(self.session)
        ExamEnrollment.objects.create(
            exam=exam,
            enrollment=self.enrollment,
        )

        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)

        self.assertFalse(sheet["has_manual_questions"])
        self.assertEqual(sheet["questions"], [])
        self.assertEqual(sheet["exam_max_score"], 100.0)
        self.assertEqual(sheet["question_score_total"], 0)
        self.assertEqual(len(sheet["rows"]), 1)

    def test_sheet_exposes_unordered_choice_and_numeric_answer_types(self):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.MIXED,
            manual_method=Exam.ManualGradingMethod.SCORE,
        )
        third = ExamQuestion.objects.create(
            sheet=second.sheet,
            number=3,
            score=0,
            question_kind=ExamQuestion.QuestionKind.CHOICE,
        )
        second.sheet.total_questions = 3
        second.sheet.choice_count = 2
        second.sheet.save(
            update_fields=[
                "total_questions",
                "choice_count",
                "updated_at",
            ]
        )
        AnswerKey.objects.create(
            exam=exam,
            answers={
                str(first.id): "2",
                str(second.id): "007",
                str(third.id): "4",
            },
        )

        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)

        self.assertEqual(
            [
                question["answer_type"]
                for question in sheet["questions"]
            ],
            ["choice", "numeric_short_answer", "choice"],
        )

    def test_correctness_preview_does_not_write_and_publish_keeps_review_semantics(
        self,
    ):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.WRITTEN,
            manual_method=Exam.ManualGradingMethod.CORRECTNESS,
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        row = sheet["rows"][0]
        payload = {
            "rows": [
                {
                    "enrollment_id": self.enrollment.id,
                    "expected_version": row["expected_version"],
                    "attendance": "present",
                    "cells": {
                        str(first.id): {"state": "review"},
                        str(second.id): {"state": "incorrect"},
                    },
                }
            ]
        }

        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload=payload,
        )

        self.assertTrue(plan.can_apply, plan.errors)
        self.assertEqual(plan.rows[0].review_question_numbers, (1,))
        self.assertEqual(plan.rows[0].wrong_question_numbers, (2,))
        self.assertEqual(plan.rows[0].total_score, 40.0)
        self.assertFalse(Result.objects.filter(target_id=exam.id).exists())

        apply_manual_grading(plan=plan)

        result = Result.objects.get(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
        )
        self.assertEqual(float(result.total_score), 40.0)
        review_item = ResultItem.objects.get(result=result, question=first)
        wrong_item = ResultItem.objects.get(result=result, question=second)
        self.assertTrue(review_item.is_correct)
        self.assertTrue(review_item.include_in_wrong_note)
        self.assertFalse(wrong_item.is_correct)
        self.assertTrue(wrong_item.include_in_wrong_note)

    def test_score_mode_accepts_partial_score(self):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.WRITTEN,
            manual_method=Exam.ManualGradingMethod.SCORE,
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "rows": [
                    {
                        "enrollment_id": self.enrollment.id,
                        "expected_version": sheet["rows"][0][
                            "expected_version"
                        ],
                        "attendance": "present",
                        "cells": {
                            str(first.id): {"score": 30},
                            str(second.id): {"score": 60},
                        },
                    }
                ]
            },
        )

        self.assertTrue(plan.can_apply, plan.errors)
        self.assertEqual(plan.rows[0].total_score, 90.0)
        self.assertEqual(plan.rows[0].wrong_question_numbers, (1,))

    def test_mixed_exam_preserves_omr_choice_item(self):
        exam, choice, essay = self._exam(
            grading_mode=Exam.GradingMode.MIXED,
            manual_method=Exam.ManualGradingMethod.SCORE,
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=40,
            max_score=100,
            objective_score=40,
        )
        ResultItem.objects.create(
            result=result,
            question=choice,
            answer="2",
            is_correct=True,
            score=40,
            max_score=40,
            source="omr",
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "rows": [
                    {
                        "enrollment_id": self.enrollment.id,
                        "expected_version": sheet["rows"][0][
                            "expected_version"
                        ],
                        "attendance": "present",
                        "cells": {str(essay.id): {"score": 50}},
                    }
                ]
            },
        )

        self.assertTrue(plan.can_apply, plan.errors)
        apply_manual_grading(plan=plan)

        result.refresh_from_db()
        self.assertEqual(float(result.objective_score), 40.0)
        self.assertEqual(float(result.total_score), 90.0)
        self.assertEqual(
            ResultItem.objects.get(result=result, question=choice).source,
            "omr",
        )

    def test_mixed_exam_projects_omr_plus_manual_essay_as_first_attempt(self):
        exam, choice, essay = self._exam(
            grading_mode=Exam.GradingMode.MIXED,
            manual_method=Exam.ManualGradingMethod.SCORE,
        )
        first_attempt = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            submission_id=1234,
            attempt_index=1,
            is_representative=True,
            status="done",
            meta={
                "total_score": 40.0,
                "max_score": 100.0,
                "initial_snapshot": {
                    "total_score": 40.0,
                    "max_score": 100.0,
                    "submitted_at": "2026-09-05T10:55:38+09:00",
                    "source": "submission_sync",
                    "submission_id": 1234,
                },
            },
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            attempt=first_attempt,
            total_score=40,
            max_score=100,
            objective_score=40,
        )
        ResultItem.objects.create(
            result=result,
            question=choice,
            answer="2",
            is_correct=True,
            score=40,
            max_score=40,
            source="omr",
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "rows": [{
                    "enrollment_id": self.enrollment.id,
                    "expected_version": sheet["rows"][0]["expected_version"],
                    "attendance": "present",
                    "cells": {str(essay.id): {"score": 50}},
                }],
            },
        )

        self.assertTrue(plan.can_apply, plan.errors)
        apply_manual_grading(plan=plan)

        first_attempt.refresh_from_db()
        snapshot = first_attempt.meta["initial_snapshot"]
        self.assertEqual(float(snapshot["total_score"]), 90.0)
        self.assertEqual(float(snapshot["max_score"]), 100.0)
        self.assertEqual(snapshot["source"], "submission_sync")
        self.assertEqual(snapshot["submission_id"], 1234)

        first_attempt.is_representative = False
        first_attempt.save(update_fields=["is_representative", "updated_at"])
        retake = ExamAttempt.objects.create(
            exam=exam,
            enrollment=self.enrollment,
            submission_id=0,
            attempt_index=2,
            is_retake=True,
            is_representative=True,
            status="done",
            meta={"total_score": 95.0, "max_score": 100.0},
        )
        result.attempt = retake
        result.total_score = 95.0
        result.save(update_fields=["attempt", "total_score", "updated_at"])

        projected = load_initial_exam_scores(
            exam_ids=[exam.id],
            enrollment_ids=[self.enrollment.id],
        )[(exam.id, self.enrollment.id)]
        self.assertEqual(projected.total_score, 90.0)
        self.assertEqual(projected.max_score, 100.0)

    def test_choice_exam_is_read_only_and_keeps_omr_as_correction_source(self):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.CHOICE,
            manual_method=Exam.ManualGradingMethod.CORRECTNESS,
        )
        result = Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=0,
            max_score=100,
            objective_score=0,
        )
        ResultItem.objects.create(
            result=result,
            question=first,
            answer="3",
            is_correct=False,
            score=0,
            max_score=40,
            source="omr",
        )
        ResultItem.objects.create(
            result=result,
            question=second,
            answer="4",
            is_correct=False,
            score=0,
            max_score=60,
            source="omr",
        )

        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)

        self.assertFalse(sheet["has_manual_questions"])
        self.assertTrue(
            all(not question["editable"] for question in sheet["questions"])
        )
        self.assertEqual(
            sheet["rows"][0]["cells"][str(first.id)]["state"],
            "incorrect",
        )

        payload = {
            "question_scores": {
                str(first.id): 30,
                str(second.id): 70,
            },
            "expected_question_scores": {
                str(first.id): 40,
                str(second.id): 60,
            },
            "rows": [
                {
                    "enrollment_id": self.enrollment.id,
                    "expected_version": sheet["rows"][0]["expected_version"],
                    "attendance": "present",
                    "cells": {
                        str(first.id): {"state": "correct"},
                        str(second.id): {"state": "incorrect"},
                    },
                }
            ],
        }
        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload=payload,
        )

        self.assertFalse(plan.can_apply)
        self.assertIn("OMR 채점 대상", str(plan.errors))
        first_item = ResultItem.objects.get(result=result, question=first)
        second_item = ResultItem.objects.get(result=result, question=second)
        self.assertEqual(first_item.answer, "3")
        self.assertEqual(second_item.answer, "4")
        self.assertFalse(first_item.is_correct)
        self.assertFalse(second_item.is_correct)
        self.assertEqual(first_item.source, "omr")
        self.assertEqual(second_item.source, "omr")
        self.assertEqual((float(first.score), float(second.score)), (40.0, 60.0))
        self.assertEqual(float(result.total_score), 0.0)

    def test_question_score_changes_must_keep_exam_total(self):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.WRITTEN,
            manual_method=Exam.ManualGradingMethod.CORRECTNESS,
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)

        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "question_scores": {
                    str(first.id): 20,
                    str(second.id): 60,
                },
                "expected_question_scores": {
                    str(first.id): 40,
                    str(second.id): 60,
                },
                "rows": [
                    {
                        "enrollment_id": self.enrollment.id,
                        "expected_version": sheet["rows"][0][
                            "expected_version"
                        ],
                        "attendance": "present",
                        "cells": {
                            str(first.id): {"state": "correct"},
                            str(second.id): {"state": "correct"},
                        },
                    }
                ],
            },
        )

        self.assertFalse(plan.can_apply)
        self.assertIn("시험 만점 100점", str(plan.errors))

    def test_publish_rejects_stale_question_score(self):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.WRITTEN,
            manual_method=Exam.ManualGradingMethod.CORRECTNESS,
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "question_scores": {
                    str(first.id): 30,
                    str(second.id): 70,
                },
                "expected_question_scores": {
                    str(first.id): 40,
                    str(second.id): 60,
                },
                "rows": [
                    {
                        "enrollment_id": self.enrollment.id,
                        "expected_version": sheet["rows"][0][
                            "expected_version"
                        ],
                        "attendance": "present",
                        "cells": {
                            str(first.id): {"state": "correct"},
                            str(second.id): {"state": "incorrect"},
                        },
                    }
                ],
            },
        )
        self.assertTrue(plan.can_apply, plan.errors)
        first.score = 35
        first.save(update_fields=["score", "updated_at"])

        with self.assertRaisesRegex(
            ManualExamGradingError,
            "다른 화면에서 변경",
        ):
            apply_manual_grading(plan=plan)

        second.refresh_from_db()
        self.assertEqual(float(second.score), 60.0)

    def test_publish_rejects_stale_result_version(self):
        exam, first, second = self._exam(
            grading_mode=Exam.GradingMode.WRITTEN,
            manual_method=Exam.ManualGradingMethod.CORRECTNESS,
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "rows": [
                    {
                        "enrollment_id": self.enrollment.id,
                        "expected_version": sheet["rows"][0][
                            "expected_version"
                        ],
                        "attendance": "present",
                        "cells": {
                            str(first.id): {"state": "correct"},
                            str(second.id): {"state": "correct"},
                        },
                    }
                ]
            },
        )
        Result.objects.create(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
            total_score=0,
            max_score=100,
        )

        with self.assertRaises(ManualExamGradingError):
            apply_manual_grading(plan=plan)

    def test_absent_row_round_trips_without_cells_or_ranking(self):
        exam, _, _ = self._exam(
            grading_mode=Exam.GradingMode.WRITTEN,
            manual_method=Exam.ManualGradingMethod.CORRECTNESS,
        )
        sheet = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        ClinicLink = django_apps.get_model("progress", "ClinicLink")
        clinic_link = ClinicLink.objects.create(
            tenant=self.tenant,
            enrollment=self.enrollment,
            session=self.session,
            source_type="exam",
            source_id=exam.id,
            reason=ClinicLink.Reason.AUTO_FAILED,
            is_auto=True,
        )

        plan = plan_manual_grading(
            exam=exam,
            tenant=self.tenant,
            payload={
                "rows": [
                    {
                        "enrollment_id": self.enrollment.id,
                        "expected_version": sheet["rows"][0]["expected_version"],
                        "attendance": "absent",
                    }
                ]
            },
        )

        self.assertTrue(plan.can_apply, plan.errors)
        self.assertEqual(plan.as_payload()["not_submitted_count"], 1)
        apply_manual_grading(plan=plan, user_id=self.admin.id)

        result = Result.objects.get(
            target_type="exam",
            target_id=exam.id,
            enrollment=self.enrollment,
        )
        attempt = ExamAttempt.objects.get(id=result.attempt_id)
        self.assertEqual((attempt.meta or {}).get("status"), "NOT_SUBMITTED")
        self.assertFalse(ResultItem.objects.filter(result=result).exists())
        clinic_link.refresh_from_db()
        self.assertEqual(
            clinic_link.resolution_type,
            ClinicLink.ResolutionType.NOT_SUBMITTED,
        )
        self.assertEqual(
            (clinic_link.resolution_evidence or {}).get("user_id"),
            self.admin.id,
        )
        self.assertNotIn(
            self.enrollment.id,
            compute_exam_rankings(exam_id=exam.id, tenant=self.tenant),
        )
        refreshed = build_manual_grading_sheet(exam=exam, tenant=self.tenant)
        self.assertTrue(refreshed["rows"][0]["is_not_submitted"])
