"""Cross-domain dependencies for the session scores BFF view."""

from __future__ import annotations

from apps.domains.attendance.models import Attendance
from apps.domains.clinic.models import SessionParticipant
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.exams.models import Exam, ExamEnrollment, ExamQuestion
from apps.domains.exams.models.sheet import Sheet
from apps.domains.exams.services.template_resolver import resolve_template_exam
from apps.domains.homework.models import HomeworkAssignment
from apps.domains.homework_results.models import Homework, HomeworkScore
from apps.domains.lectures.models import Session
from apps.domains.progress.models import AssessmentCorrection, ClinicLink, SessionProgress
from apps.domains.submissions.models import Submission
