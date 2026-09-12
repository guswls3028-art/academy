"""Cross-domain fixtures for the mixed OMR results contract tests."""

from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import (
    AnswerKey,
    Exam,
    ExamEnrollment,
    ExamQuestion,
    Sheet,
)
from apps.domains.exams.views.exam_recalculate_view import ExamRecalculateView
from apps.domains.lectures.models import Lecture, Session
from apps.domains.progress.dispatcher import dispatch_progress_pipeline
from apps.domains.progress.models import (
    AssessmentCorrection,
    ClinicLink,
    ProgressPolicy,
    SessionProgress,
)
from apps.domains.progress.services.session_calculator import SessionProgressCalculator
from apps.domains.students.models import Student
from apps.domains.submissions.models import Submission, SubmissionAnswer
from apps.domains.submissions.views.submission_view import SubmissionViewSet

__all__ = [
    "AnswerKey",
    "AssessmentCorrection",
    "ClinicLink",
    "Enrollment",
    "Exam",
    "ExamEnrollment",
    "ExamQuestion",
    "ExamRecalculateView",
    "Lecture",
    "ProgressPolicy",
    "Session",
    "SessionEnrollment",
    "SessionProgress",
    "SessionProgressCalculator",
    "Sheet",
    "Student",
    "Submission",
    "SubmissionAnswer",
    "SubmissionViewSet",
    "dispatch_progress_pipeline",
]
