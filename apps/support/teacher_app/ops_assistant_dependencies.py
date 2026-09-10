"""Cross-domain ports used by the teacher operations assistant."""

from apps.domains.attendance.models import Attendance
from apps.domains.attendance.services import ensure_session_roster_membership
from apps.domains.enrollment.models import Enrollment, SessionEnrollment
from apps.domains.enrollment.services.lifecycle import (
    assess_disposable_enrollment,
    bulk_create_enrollments,
    delete_disposable_enrollment,
)
from apps.domains.lectures.models import Lecture, Session
from apps.domains.messaging.models import NotificationLog, ScheduledNotification
from apps.domains.messaging.policy import get_owner_tenant_id
from apps.domains.messaging.selectors import get_auto_send_config
from apps.domains.parents.services import parent_account_needs_password
from apps.domains.students.models import Student
from apps.domains.students.services.account_notifications import (
    send_parent_account_credentials_notice,
)
from apps.domains.students.services.creation import create_student_account
from apps.domains.students.services.identity import StudentIdentityError, normalize_student_phone
from apps.domains.students.services.import_students import resolve_student_import_row
from apps.domains.students.services.profile import (
    StudentProfileUpdateError,
    update_student_profile,
)
from apps.domains.video.models import AccessMode, Video, VideoAccess
from apps.domains.video.services.access_resolver import resolve_access_mode

__all__ = [
    "AccessMode",
    "Attendance",
    "Enrollment",
    "Lecture",
    "NotificationLog",
    "ScheduledNotification",
    "Session",
    "SessionEnrollment",
    "Student",
    "StudentIdentityError",
    "StudentProfileUpdateError",
    "Video",
    "VideoAccess",
    "assess_disposable_enrollment",
    "bulk_create_enrollments",
    "create_student_account",
    "delete_disposable_enrollment",
    "ensure_session_roster_membership",
    "get_auto_send_config",
    "get_owner_tenant_id",
    "normalize_student_phone",
    "parent_account_needs_password",
    "resolve_access_mode",
    "resolve_student_import_row",
    "send_parent_account_credentials_notice",
    "update_student_profile",
]
