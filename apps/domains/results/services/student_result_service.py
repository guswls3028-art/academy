# PATH: apps/domains/results/services/student_result_service.py
"""
단일 진실: 학생용 시험 결과 조회
- GET /results/me/exams/<exam_id>/ (results 앱)
- GET /student/results/me/exams/<exam_id>/ (student_app)
둘 다 이 서비스의 동일 데이터를 반환하도록 사용.
"""
from __future__ import annotations

from django.http import Http404

from academy.adapters.db.django import repositories_exams as exams_repo
from apps.domains.results.models import Result, ExamAttempt
from apps.domains.results.serializers.student_exam_result import StudentExamResultSerializer
from apps.domains.results.utils.session_exam import get_sessions_for_exam
from apps.domains.results.utils.clinic import is_clinic_required
from apps.domains.results.utils.ranking import compute_exam_rankings
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.domains.results.utils.exam_achievement import compute_exam_achievement
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_score,
)
from apps.domains.results.services.assessment_correction_status import (
    assessment_correction_payload,
    exam_correction_fingerprint,
)
from apps.domains.results.services.answer_matching import format_answer_for_display
from apps.domains.results.aggregations.exam_report import summarize_result_items
from apps.domains.enrollment.selectors import learning_history_enrollments_for_student
from apps.support.results.student_result_dependencies import (
    active_enrollments_for_student,
    get_request_student,
)
from apps.support.results.assessment_correction_dependencies import AssessmentCorrection


def active_exam_enrollment_ids_for_student(*, tenant, student, exam_id: int) -> list[int]:
    """Return active enrollment ids that connect one student to one tenant exam."""
    allowed_enrollment_ids = exams_repo.exam_enrollment_ids_for_tenant_exam(exam_id, tenant)
    return list(
        active_enrollments_for_student(
            tenant=tenant,
            student=student,
        )
        .filter(id__in=allowed_enrollment_ids)
        .values_list("id", flat=True)
    )


def _history_exam_enrollment_ids_for_student(*, tenant, student, exam_id: int) -> list[int]:
    allowed_enrollment_ids = exams_repo.exam_enrollment_ids_for_tenant_exam(exam_id, tenant)
    return list(
        learning_history_enrollments_for_student(
            tenant=tenant,
            student=student,
        )
        .filter(id__in=allowed_enrollment_ids)
        .values_list("id", flat=True)
    )


def get_my_exam_result_data(request, exam_id: int, tenant=None) -> dict:
    """
    현재 사용자의 시험 결과 스냅샷 + 재시험/클리닉 정책.
    enrollment/result 없으면 Http404.
    tenant: 테넌트 격리를 위해 반드시 전달해야 함.
    """
    if tenant is None:
        tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404("tenant resolution failed")
    exam_id = int(exam_id)
    exam = exams_repo.regular_active_exam_for_tenant(exam_id, tenant)
    if not exam:
        raise Http404("exam not found")
    student = get_request_student(request)
    if not student:
        raise Http404("student not found")

    enrollment_ids = _history_exam_enrollment_ids_for_student(
        tenant=tenant,
        student=student,
        exam_id=exam_id,
    )
    enrollment = (
        learning_history_enrollments_for_student(tenant=tenant, student=student)
        .filter(id__in=enrollment_ids)
        .order_by("id")
        .first()
    )
    if not enrollment:
        raise Http404("enrollment not found")

    enrollment_id = int(enrollment.id)
    session_candidates = list(
        get_sessions_for_exam(exam_id)
        .filter(
            lecture_id=enrollment.lecture_id,
            lecture__tenant=tenant,
        )
        .order_by("order", "id")[:2]
    )
    # 다른 강의/차시의 확인 기록을 빌려오지 않는다. 같은 수강 강의에서도
    # 후보가 둘 이상이면 상세 화면의 상태는 실패 폐쇄한다.
    session = session_candidates[0] if len(session_candidates) == 1 else None

    result = (
        Result.objects
        .filter(target_type="exam", target_id=exam_id, enrollment_id=enrollment_id)
        .prefetch_related("items")
        .order_by("-id")
        .first()
    )
    if not result:
        raise Http404("result not found")

    allow_retake = bool(getattr(exam, "allow_retake", False))
    max_attempts = int(getattr(exam, "max_attempts", 1) or 1)
    attempt_count = ExamAttempt.objects.filter(
        exam_id=exam_id,
        enrollment_id=enrollment_id,
    ).count()
    can_retake = bool(
        enrollment.lecture.is_active
        and allow_retake
        and attempt_count < max_attempts
    )

    # 시험 응시 기록과 교직원 성적 운영은 유지하되, 학생·학부모에게는
    # 공개 전 점수·문항·석차를 전혀 직렬화하지 않는다. 재응시 가능 여부는
    # 결과 비공개 상태에서도 서버가 계속 소유해야 중복 응시를 막을 수 있다.
    if not bool(getattr(exam, "student_results_published", True)):
        return {
            "exam_id": exam_id,
            "student_results_published": False,
            "allow_retake": allow_retake,
            "max_attempts": max_attempts,
            "can_retake": can_retake,
        }

    clinic_required = False
    if session:
        clinic_required = is_clinic_required(
            session=session,
            enrollment_id=enrollment_id,
            include_manual=False,
        )

    initial_scores = load_initial_exam_scores(
        exam_ids=[exam_id],
        enrollment_ids=[enrollment_id],
    )
    initial_state = initial_scores.get((exam_id, enrollment_id))
    initial_score = project_initial_exam_score(
        state=initial_state,
        fallback_score=result.total_score,
        fallback_max_score=result.max_score,
        fallback_recorded_at=result.submitted_at or result.created_at,
    )

    data = StudentExamResultSerializer(result).data
    representative_is_initial = bool(
        (
            initial_state is not None
            and initial_state.attempt_id is not None
            and result.attempt_id == initial_state.attempt_id
        )
        or (initial_state is None and result.attempt_id is None)
    )
    if not representative_is_initial:
        # ResultItem은 최신 대표 Result 스냅샷이라 2차+가 덮어쓸 수 있다.
        # 1차 문항을 확실히 복원할 수 없으면 재시험 상세를 1차 원점수에 섞지 않는다.
        data["items"] = []
    data["total_score"] = initial_score.total_score
    data["max_score"] = initial_score.max_score
    data["submitted_at"] = initial_score.recorded_at
    data["student_results_published"] = True
    data["allow_retake"] = allow_retake
    data["max_attempts"] = max_attempts
    data["can_retake"] = can_retake
    data["clinic_required"] = bool(clinic_required)

    # ✅ 성취 계산 (SSOT: utils/exam_achievement)
    # student/admin 뷰가 동일 유틸을 사용해 드리프트 재발을 구조적으로 방지.
    pass_score = effective_exam_pass_score(
        exam=exam,
        lecture_id=getattr(enrollment, "lecture_id", None),
    )
    achievement_data = compute_exam_achievement(
        enrollment_id=enrollment_id,
        exam_id=exam_id,
        session=session,
        total_score=float(initial_score.total_score or 0.0),
        pass_score=pass_score,
        attempt_id=result.attempt_id,
        tenant=tenant,
    )
    data["exam_id"] = exam_id
    data["meta_status"] = achievement_data["meta_status"]
    data["is_pass"] = achievement_data["is_pass"]
    data["remediated"] = achievement_data["remediated"]
    data["clinic_retake"] = achievement_data["clinic_retake"]
    data["final_pass"] = achievement_data["final_pass"]
    data["is_provisional"] = achievement_data["is_provisional"]
    data["achievement"] = achievement_data["achievement"]

    is_not_submitted = achievement_data["meta_status"] == "NOT_SUBMITTED"
    is_provisional = achievement_data["is_provisional"]
    if is_not_submitted:
        data["total_score"] = None

    correction = None
    if session is not None:
        correction = AssessmentCorrection.objects.filter(
            tenant=tenant,
            enrollment_id=enrollment_id,
            session=session,
            source_type=AssessmentCorrection.SourceType.EXAM,
            source_id=exam_id,
        ).first()
    data["correction_status"] = (
        assessment_correction_payload(
            source_type=AssessmentCorrection.SourceType.EXAM,
            score=data.get("total_score"),
            max_score=float(getattr(exam, "max_score", 100.0) or 100.0),
            source_fingerprint=exam_correction_fingerprint(
                result=result,
                items=result.items.all(),
                current_max_score=float(
                    getattr(exam, "max_score", 100.0) or 100.0
                ),
            ),
            correction=correction,
        )["correction_status"]
        if session is not None
        else None
    )

    # 정답 공개 정책 적용
    # provisional/미응시/불합격 → 비공개, 합격/기준없음 → 정책 따름
    is_pass = data["is_pass"]
    if is_provisional or is_not_submitted:
        show_answers = False
    elif is_pass is None or is_pass:
        show_answers = exam.should_show_answers()
    else:
        show_answers = False
    data["answer_visibility"] = getattr(exam, "answer_visibility", "hidden")
    data["answers_visible"] = show_answers

    # question_id → question_number 매핑 (ExamQuestion.number 사용)
    item_question_ids = [
        item.get("question_id") for item in (data.get("items") or [])
        if item.get("question_id")
    ]
    template_exam_id = exam.effective_template_exam_id
    question_number_map = exams_repo.exam_question_number_map(
        item_question_ids,
        exam_id=template_exam_id,
        tenant=tenant,
    )
    data["items"] = [
        item for item in (data.get("items") or [])
        if item.get("question_id") in question_number_map
    ]

    # 정답 공개 시 answer key에서 correct_answer 주입
    correct_answer_map = {}
    if show_answers:
        correct_answer_map = exams_repo.answer_key_answers_for_exam(
            template_exam_id,
            tenant=tenant,
        )

    for item in data.get("items") or []:
        q_id = item.get("question_id")
        item["question_number"] = question_number_map.get(q_id)
        item.setdefault("student_answer", item.get("answer"))
        if show_answers:
            correct = correct_answer_map.get(str(q_id or ""))
            item["correct_answer"] = format_answer_for_display(correct) if correct else None
        else:
            item["correct_answer"] = None

    data["analysis"] = summarize_result_items(data.get("items") or [])

    # 석차 정보 추가
    rank_map = compute_exam_rankings(
        exam_id=exam_id,
        tenant=tenant,
        lecture_ids={int(enrollment.lecture_id)},
    )
    rank_info = {} if is_not_submitted else rank_map.get(enrollment_id, {})
    data["rank"] = rank_info.get("rank")
    data["percentile"] = rank_info.get("percentile")
    data["cohort_size"] = rank_info.get("cohort_size")
    data["cohort_avg"] = rank_info.get("cohort_avg")

    return data
