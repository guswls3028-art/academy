"""Cross-domain assessment-correction dependencies for result read models."""

from apps.domains.progress.models import AssessmentCorrection


def set_teacher_assessment_resolution(**kwargs):
    from apps.domains.progress.dispatcher import (
        set_teacher_assessment_resolution as set_resolution,
    )

    return set_resolution(**kwargs)


def homework_media_set_fingerprint(**kwargs) -> str:
    from apps.domains.submissions.services.homework_media import (
        homework_media_set_fingerprint as fingerprint,
    )

    return fingerprint(**kwargs)


__all__ = [
    "AssessmentCorrection",
    "homework_media_set_fingerprint",
    "set_teacher_assessment_resolution",
]
