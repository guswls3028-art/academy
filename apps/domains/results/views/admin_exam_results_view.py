# apps/domains/results/views/admin_exam_results_view.py
from __future__ import annotations

from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from apps.api.common.query_params import parse_query_int
from apps.core.permissions import TenantResolvedAndStaff
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_scores,
    sessions_by_id_for_tenant,
)
from apps.domains.results.models import Result, ResultFact, ExamAttempt
from apps.domains.results.serializers.admin_exam_result_row import (
    AdminExamResultRowSerializer,
)
from apps.domains.results.services.assessment_correction_status import (
    assessment_correction_payload,
    exam_correction_fingerprint,
)
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids,
)

from apps.support.results.admin_exam_dependencies import (
    get_enrollments_for_tenant_by_id,
    get_regular_active_exam_for_tenant_or_none,
    get_submission_status_by_id_for_tenant,
    regular_active_exam_with_session_exists,
)

from apps.domains.results.utils.clinic import get_clinic_enrollment_ids_for_session
from apps.domains.results.utils.result_queries import latest_results_per_enrollment
from apps.domains.results.views.session_scores_view import _safe_student_name, _get_enrollment_display_fields
from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map
from apps.domains.results.utils.exam_achievement import compute_exam_achievement_bulk
from apps.domains.results.utils.exam_absence import current_exam_absence_counts
from apps.domains.results.utils.initial_exam_score import (
    InitialExamScore,
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.support.results.admin_student_grades_dependencies import (
    primary_session_metadata_by_exam_and_lecture,
)
from apps.support.results.assessment_correction_dependencies import AssessmentCorrection


_FAILED_SUBMISSION_STATUSES = {"failed", "error"}
_DONE_SUBMISSION_STATUSES = {"done", "completed", "success"}
_PROCESSING_SUBMISSION_STATUSES = {
    "pending",
    "submitted",
    "dispatched",
    "extracting",
    "answers_ready",
    "grading",
    "running",
    "processing",
}


def _result_display_status(
    *,
    meta_status: str | None,
    submission_status: str | None,
    visible_total_score: float | None,
    is_provisional: bool,
) -> str:
    """Return the backend-owned status shown in the admin result list."""
    if meta_status == "NOT_SUBMITTED":
        return "NOT_SUBMITTED"

    raw_submission_status = str(submission_status or "").strip().lower()
    if raw_submission_status in _FAILED_SUBMISSION_STATUSES:
        return "FAILED"
    if raw_submission_status in _PROCESSING_SUBMISSION_STATUSES:
        return "PROCESSING"
    if is_provisional:
        return "PARTIAL"
    if raw_submission_status in _DONE_SUBMISSION_STATUSES:
        return "DONE"
    if visible_total_score is not None:
        return "DONE"
    if raw_submission_status:
        return "PROCESSING"
    return "NOT_SUBMITTED"


class AdminExamResultsView(ListAPIView):
    """
    GET /results/admin/exams/<exam_id>/results/

    ✅ 목표(원본 유지 + 정합성 강화)
    - Result(스냅샷) 기반 점수 리스트
    - Attempt/Submission 상태 연결
    - Clinic 기준 통일(ClinicLink)
    - Session↔Exam 매핑 단일화(utils.session_exam)

    ⚠️ pass 기준 정의:
    - 강의별 커트라인이 있으면 해당 기준, 없으면 Exam.pass_score를 제공한다.
    - 세션 종합 통과(SessionProgress.exam_passed)는
      /admin/sessions/... summary API에서 제공하는 것이 정석.

    응답: { "count", "next", "previous", "results": AdminExamResultRow[] }
    ?page=1 로 페이지 접근 가능.
    """

    permission_classes = [IsAuthenticated, TenantResolvedAndStaff]
    pagination_class = None  # 전체 반환: 시험당 응시자 수십~수백명, rank 정렬 위해 페이지네이션 제거
    serializer_class = AdminExamResultRowSerializer

    def get_queryset(self):
        exam_id = self.kwargs.get("exam_id")
        if exam_id is None:
            return Result.objects.none()

        # ✅ tenant isolation: verify exam belongs to tenant
        if not regular_active_exam_with_session_exists(
            exam_id=int(exam_id),
            tenant=self.request.tenant,
        ):
            return Result.objects.none()

        exam = get_regular_active_exam_for_tenant_or_none(
            exam_id=int(exam_id),
            tenant=self.request.tenant,
        )
        if exam is None:
            return Result.objects.none()
        linked_lecture_ids = set(
            exam.sessions.filter(lecture__tenant=self.request.tenant)
            .values_list("lecture_id", flat=True)
        )
        selected_lecture_id = parse_query_int(
            self.request.query_params,
            "lecture_id",
            min_value=1,
        )
        if (
            selected_lecture_id is not None
            and selected_lecture_id not in linked_lecture_ids
        ):
            raise ValidationError(
                {"lecture_id": "이 시험에 연결되지 않은 강의입니다."}
            )
        self.linked_lecture_ids = linked_lecture_ids
        self.selected_lecture_id = selected_lecture_id
        scoped_lecture_ids = (
            {selected_lecture_id}
            if selected_lecture_id is not None
            else linked_lecture_ids
        )

        return (
            latest_results_per_enrollment(
                target_type="exam",
                target_id=int(exam_id),
            )
            .filter(
                enrollment__tenant=self.request.tenant,
                enrollment__lecture_id__in=scoped_lecture_ids,
            )
            .exclude(enrollment_id__isnull=True)
            .prefetch_related("items")
            .order_by("enrollment_id")
        )

    def list(self, request, *args, **kwargs):
        exam_id = int(self.kwargs["exam_id"])

        exam = get_regular_active_exam_for_tenant_or_none(
            exam_id=exam_id,
            tenant=request.tenant,
        )
        queryset = self.get_queryset()
        results = list(queryset)
        pending_result_ids = pending_omr_result_ids(results)
        pending_enrollment_ids = {
            int(result.enrollment_id)
            for result in results
            if int(result.id) in pending_result_ids
        }
        linked_lecture_ids = getattr(self, "linked_lecture_ids", set())
        selected_lecture_id = getattr(self, "selected_lecture_id", None)
        pass_scores_by_lecture = effective_exam_pass_scores(
            exam=exam,
            lecture_ids=linked_lecture_ids,
        ) if exam else {}
        initial_scores = load_initial_exam_scores(
            exam_ids=[exam_id],
            enrollment_ids=[result.enrollment_id for result in results],
        )
        projected_scores = {}
        for result in results:
            enrollment_id = int(result.enrollment_id)
            projected = project_initial_exam_score(
                state=initial_scores.get((exam_id, int(result.enrollment_id))),
                fallback_score=result.total_score,
                fallback_max_score=result.max_score,
                fallback_recorded_at=result.submitted_at or result.created_at,
            )
            if result.attempt_id == projected.attempt_id:
                projected = InitialExamScore(
                    total_score=float(result.total_score or 0.0),
                    max_score=float(result.max_score or 0.0),
                    not_submitted=projected.not_submitted,
                    attempt_id=projected.attempt_id,
                    recorded_at=projected.recorded_at,
                )
            projected_scores[enrollment_id] = projected

        # -------------------------------------------------
        # enrollment_id → student_name (Enrollment 단일 진실)
        # 🔐 tenant 강제: Result row가 exam tenant 스코프(get_queryset에서 검증)지만
        # enrollment_id 참조 자체에는 제약이 없으므로 명시적으로 차단.
        # -------------------------------------------------
        enrollment_ids_page = [int(r.enrollment_id) for r in results]
        enrollment_map = get_enrollments_for_tenant_by_id(
            enrollment_ids=enrollment_ids_page,
            tenant=request.tenant,
        )
        student_name_map = {
            eid: _safe_student_name(enrollment_map.get(eid))
            for eid in enrollment_ids_page
        }

        # 시험이 여러 강의/차시에 재사용될 수 있으므로 수강 강의까지 일치하는
        # 차시가 정확히 하나일 때만 교사 오답 확인 상태를 연결한다.
        exam_lecture_pairs = {
            (exam_id, int(enrollment.lecture_id))
            for enrollment in enrollment_map.values()
            if getattr(enrollment, "lecture_id", None) is not None
        }
        correction_session_meta = primary_session_metadata_by_exam_and_lecture(
            tenant=request.tenant,
            exam_lecture_pairs=exam_lecture_pairs,
        )
        correction_session_ids = {
            int(meta["session_id"])
            for meta in correction_session_meta.values()
            if meta.get("session_id") is not None
        }
        correction_map = {
            (int(correction.enrollment_id), int(correction.session_id)): correction
            for correction in AssessmentCorrection.objects.filter(
                tenant=request.tenant,
                enrollment_id__in=enrollment_ids_page,
                session_id__in=correction_session_ids,
                source_type=AssessmentCorrection.SourceType.EXAM,
                source_id=exam_id,
            )
        }

        session_ids_by_lecture = {
            lecture_id: int(meta["session_id"])
            for (_mapped_exam_id, lecture_id), meta in correction_session_meta.items()
            if meta.get("session_id") is not None
        }
        sessions_by_id = sessions_by_id_for_tenant(
            session_ids=set(session_ids_by_lecture.values()),
            tenant=request.tenant,
        )
        sessions_by_lecture = {
            lecture_id: sessions_by_id.get(session_id)
            for lecture_id, session_id in session_ids_by_lecture.items()
        }

        # -------------------------------------------------
        # enrollment_id → 최신 attempt/submission 맵 (exam 전체 기준)
        # -------------------------------------------------
        fact_qs = (
            ResultFact.objects
            .filter(target_type="exam", target_id=exam_id, enrollment__tenant=request.tenant)
            .exclude(attempt_id__isnull=True)
            .order_by("-attempt_id", "-id")
            .values("enrollment_id", "attempt_id", "submission_id")
        )

        latest_map = {}
        for row in fact_qs:
            eid = int(row["enrollment_id"])
            if eid not in latest_map:
                latest_map[eid] = {
                    "attempt_id": int(row["attempt_id"]),
                    "submission_id": int(row["submission_id"]) if row["submission_id"] is not None else 0,
                }

        # Result.attempt_id fallback (현재 페이지 결과만)
        attempt_ids = [r.attempt_id for r in results if getattr(r, "attempt_id", None)]
        attempt_ids.extend(
            state.attempt_id
            for state in initial_scores.values()
            if state.attempt_id is not None
        )
        attempt_map = {
            a.id: a
            for a in ExamAttempt.objects.filter(id__in=attempt_ids, exam_id=exam_id)
        }

        for r in results:
            eid = int(r.enrollment_id)
            aid = getattr(r, "attempt_id", None)
            if not aid:
                continue
            a = attempt_map.get(int(aid))
            if not a:
                continue
            if (eid not in latest_map) or (not latest_map[eid].get("submission_id")):
                latest_map[eid] = {
                    "attempt_id": int(a.id),
                    "submission_id": int(a.submission_id) if a.submission_id is not None else 0,
                }

        # 기본 성적 행의 제출/처리 상태도 2차+ 최신 제출이 아니라 1차 시도를 따른다.
        for (state_exam_id, enrollment_id), state in initial_scores.items():
            if state_exam_id != exam_id or state.attempt_id is None:
                continue
            attempt = attempt_map.get(int(state.attempt_id))
            if attempt is None:
                continue
            latest_map[int(enrollment_id)] = {
                "attempt_id": int(attempt.id),
                "submission_id": (
                    int(attempt.submission_id)
                    if attempt.submission_id is not None
                    else 0
                ),
            }

        # Submission.status (현재 페이지에서 참조하는 submission만)
        submission_ids = [
            v["submission_id"]
            for v in latest_map.values()
            if v.get("submission_id")
        ]
        submission_status_map = get_submission_status_by_id_for_tenant(
            submission_ids=submission_ids,
            tenant=request.tenant,
        )

        # -------------------------------------------------
        # 클리닉 하이라이트 (SSOT 유틸)
        # -------------------------------------------------
        highlight_map = {}
        clinic_required_ids = set()
        enrollment_ids_by_lecture = {}
        for enrollment_id, enrollment in enrollment_map.items():
            lecture_id = getattr(enrollment, "lecture_id", None)
            if lecture_id is not None:
                enrollment_ids_by_lecture.setdefault(int(lecture_id), set()).add(
                    int(enrollment_id)
                )
        for lecture_id, lecture_enrollment_ids in enrollment_ids_by_lecture.items():
            session = sessions_by_lecture.get(lecture_id)
            if session is None:
                continue
            highlight_map.update(
                compute_clinic_highlight_map(
                    tenant=request.tenant,
                    enrollment_ids=lecture_enrollment_ids,
                    session=session,
                )
            )
            clinic_required_ids.update(
                get_clinic_enrollment_ids_for_session(
                    session=session,
                    include_manual=False,
                )
                & lecture_enrollment_ids
            )

        # -------------------------------------------------
        # 석차 계산 (전체 응시자 대상, 페이지와 무관)
        # -------------------------------------------------
        ranked_scores = sorted(
            (
                (enrollment_id, float(projected.total_score))
                for enrollment_id, projected in projected_scores.items()
                if not projected.not_submitted and projected.total_score is not None
                and enrollment_id not in pending_enrollment_ids
            ),
            key=lambda row: (-row[1], row[0]),
        )
        rank_map = {}
        cohort_size = len(ranked_scores)
        cohort_avg = (
            round(sum(score for _, score in ranked_scores) / cohort_size, 2)
            if cohort_size
            else 0.0
        )
        previous_score = None
        current_rank = 0
        for position, (enrollment_id, score) in enumerate(
            ranked_scores,
            start=1,
        ):
            if previous_score is None or score != previous_score:
                current_rank = position
                previous_score = score
            rank_map[enrollment_id] = {
                "rank": current_rank,
                "ranking_score": score,
                "percentile": (
                    100.0
                    if cohort_size <= 1
                    else round((current_rank / cohort_size) * 100, 1)
                ),
                "cohort_size": cohort_size,
                "cohort_avg": cohort_avg,
            }

        # -------------------------------------------------
        # 성취/클리닉 상태 일괄 계산 (N+1 방지)
        # -------------------------------------------------
        achievement_items = []
        for r in results:
            enrollment = enrollment_map.get(int(r.enrollment_id))
            lecture_id = int(getattr(enrollment, "lecture_id", 0) or 0)
            achievement_items.append({
                "enrollment_id": int(r.enrollment_id),
                "exam_id": exam_id,
                "total_score": projected_scores[int(r.enrollment_id)].total_score,
                "pass_score": pass_scores_by_lecture.get(
                    lecture_id,
                    float(getattr(exam, "pass_score", 0.0) or 0.0),
                ),
                "attempt_id": getattr(r, "attempt_id", None),
                "session": sessions_by_lecture.get(lecture_id),
            })
        achievement_map = compute_exam_achievement_bulk(
            items=achievement_items,
            tenant=request.tenant,
        )
        exam_absence_count_map = current_exam_absence_counts(
            tenant=request.tenant,
            enrollment_ids=enrollment_ids_page,
        )
        # -------------------------------------------------
        # rows 구성 (기존 로직 유지 + 성취 SSOT 필드 주입)
        # -------------------------------------------------
        rows = []
        for r in results:
            enrollment_id = int(r.enrollment_id)
            student_name = student_name_map.get(enrollment_id, "-")

            latest = latest_map.get(enrollment_id, {})
            submission_id = latest.get("submission_id")
            submission_status = (
                submission_status_map.get(submission_id) if submission_id else None
            )

            # ✅ 성취 SSOT 계산: student_result_service와 동일 유틸 사용으로
            #    관리자 목록과 학생 상세 뷰의 드리프트를 구조적으로 차단.
            # 미응시·미채점 케이스에서 0 coerce 금지 — None 그대로 유지해
            # achievement 판정과 화면 표시가 모순되지 않도록.
            initial_score = projected_scores[enrollment_id]
            raw_total_score = initial_score.total_score
            raw_max_score = initial_score.max_score
            achievement_data = achievement_map[(enrollment_id, exam_id)]
            subjective_pending = int(r.id) in pending_result_ids
            visible_total_score = (
                None
                if achievement_data["meta_status"] == "NOT_SUBMITTED"
                else raw_total_score
            )
            # passed = 1차 합격(석차 판정용). 기존 응답 호환.
            passed = achievement_data["is_pass"]

            clinic_required = (
                enrollment_id in clinic_required_ids and not subjective_pending
            )

            # 학생 SSOT 표시용 필드 (아바타 + 강의 딱지)
            display = _get_enrollment_display_fields(enrollment_map.get(enrollment_id))

            rank_info = (
                {}
                if achievement_data["meta_status"] == "NOT_SUBMITTED"
                else rank_map.get(enrollment_id, {})
            )
            result_status = _result_display_status(
                meta_status=achievement_data["meta_status"],
                submission_status=submission_status,
                visible_total_score=visible_total_score,
                is_provisional=bool(
                    achievement_data["is_provisional"] or subjective_pending
                ),
            )
            enrollment = enrollment_map.get(enrollment_id)
            lecture_id = getattr(enrollment, "lecture_id", None)
            correction_meta = (
                correction_session_meta.get((exam_id, int(lecture_id)))
                if lecture_id is not None
                else None
            ) or {}
            correction_session_id = correction_meta.get("session_id")
            correction = (
                correction_map.get((enrollment_id, int(correction_session_id)))
                if correction_session_id is not None
                else None
            )
            correction_status = None
            if correction_session_id is not None and not subjective_pending:
                current_exam_max_score = float(
                    getattr(exam, "max_score", 100.0) or 100.0
                )
                correction_status = assessment_correction_payload(
                    source_type=AssessmentCorrection.SourceType.EXAM,
                    score=visible_total_score,
                    max_score=current_exam_max_score,
                    source_fingerprint=exam_correction_fingerprint(
                        result=r,
                        items=r.items.all(),
                        current_max_score=current_exam_max_score,
                    ),
                    correction=correction,
                )["correction_status"]

            rows.append({
                "enrollment_id": enrollment_id,
                "student_name": student_name,

                # None 보존: 미응시·미채점 행은 점수 셀이 "미응시/미채점"로 표시되어야 함.
                "exam_score": visible_total_score,
                "exam_max_score": raw_max_score,
                # Backward-compatible aliases for older/mobile consumers.
                "total_score": visible_total_score,
                "max_score": raw_max_score,

                "final_score": visible_total_score,

                "passed": passed,
                "lecture_id": int(lecture_id),
                "pass_score": pass_scores_by_lecture.get(
                    int(lecture_id),
                    float(getattr(exam, "pass_score", 0.0) or 0.0),
                ),
                "clinic_required": clinic_required,

                # 성취 SSOT 필드
                "remediated": achievement_data["remediated"],
                "final_pass": achievement_data["final_pass"],
                "achievement": achievement_data["achievement"],
                "clinic_retake": achievement_data["clinic_retake"],
                "is_provisional": bool(
                    achievement_data["is_provisional"] or subjective_pending
                ),
                "grading_status": (
                    "subjective_pending" if subjective_pending else None
                ),
                "meta_status": achievement_data["meta_status"],

                "submitted_at": initial_score.recorded_at,

                "submission_id": submission_id,
                "submission_status": submission_status,
                "result_status": result_status,
                "correction_session_id": correction_session_id,
                "correction_status": correction_status,
                "name_highlight_clinic_target": (
                    highlight_map.get(enrollment_id, False)
                    and not subjective_pending
                ),
                "exam_not_submitted_count": exam_absence_count_map.get(enrollment_id, 0),

                # 석차 정보
                "rank": rank_info.get("rank"),
                "ranking_score": rank_info.get("ranking_score"),
                "percentile": rank_info.get("percentile"),
                "cohort_size": rank_info.get("cohort_size"),
                "cohort_avg": rank_info.get("cohort_avg"),

                **display,
            })

        rows.sort(
            key=lambda row: (
                row["rank"] is None,
                row["rank"] if row["rank"] is not None else 0,
                -float(row["ranking_score"] or 0.0),
                str(row["student_name"] or ""),
                int(row["enrollment_id"]),
            )
        )

        serializer = AdminExamResultRowSerializer(rows, many=True)
        return Response({
            "count": len(rows),
            "next": None,
            "previous": None,
            "selected_lecture_id": selected_lecture_id,
            "results": serializer.data,
        })
