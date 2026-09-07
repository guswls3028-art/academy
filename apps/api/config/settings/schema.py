"""Deterministic OpenAPI generation settings.

This module extends the isolated test environment and is never used by the
production runtime. drf-yasg remains the live documentation surface while the
committed OpenAPI 3 schema is adopted by generated clients.
"""

from .test import *  # noqa: F401,F403

INSTALLED_APPS = [*INSTALLED_APPS, "drf_spectacular"]  # noqa: F405
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "HakwonPlus API",
    "DESCRIPTION": "Generated contract for the Academy v1 API.",
    "VERSION": "v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "DISABLE_ERRORS_AND_WARNINGS": True,
    "ENUM_NAME_OVERRIDES": {
        "ExamGradingMode": "apps.domains.exams.models.Exam.GradingMode",
        "HomeworkGradingMode": "apps.domains.homework_results.models.Homework.GradingMode",
        "InactiveVideoEntitlementAccessMode": [
            ("FREE_REVIEW", "복습"),
            ("PROCTORED_CLASS", "온라인 수업 대체"),
        ],
        "StudentVideoEffectiveAccessMode": [
            "FREE_REVIEW",
            "PROCTORED_CLASS",
        ],
        "StudentActivityCategory":
            "apps.domains.students.views.support_views.ACTIVITY_CATEGORIES",
        "InactiveVideoEntitlementSource": [
            ("STAFF_AUTHORIZATION", "교직원 명시 승인"),
        ],
        "ManualMessageRecipient": [
            ("student", "학생"),
            ("parent", "학부모"),
            ("staff", "직원"),
        ],
        "SendToEnum": [
            ("student", "student"),
            ("parent", "parent"),
            ("both", "both"),
        ],
    },
}

# Register custom authentication extensions after the schema settings exist.
from apps.api import schema_extensions as _schema_extensions  # noqa: E402,F401
