"""Cross-domain fixtures for the mixed OMR results contract tests."""

from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import (
    AnswerKey,
    Exam,
    ExamEnrollment,
    ExamQuestion,
    Sheet,
)
from apps.domains.lectures.models import Lecture, Session
from apps.domains.progress.dispatcher import dispatch_progress_pipeline
from apps.domains.progress.models import ClinicLink, ProgressPolicy
from apps.domains.students.models import Student
from apps.domains.submissions.models import Submission, SubmissionAnswer

__all__ = [
    "AnswerKey",
    "ClinicLink",
    "Enrollment",
    "Exam",
    "ExamEnrollment",
    "ExamQuestion",
    "Lecture",
    "ProgressPolicy",
    "Session",
    "SessionEnrollment",
    "Sheet",
    "Student",
    "Submission",
    "SubmissionAnswer",
    "dispatch_progress_pipeline",
]
