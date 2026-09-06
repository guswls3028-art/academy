# PATH: apps/domains/results/services/clinic_target_service.py
"""
역할
- Admin/Teacher용 "클리닉 대상자" 리스트를 생성한다.
- clinic_required의 단일 진실: progress.SessionProgress.completed가 아닌 미해소 ClinicLink(is_auto=True)

설계 계약 (중요)
1) 단일 진실: enrollment_id (학생 식별은 enrollment_id로만)
2) 현재 clinic_required 판단은 미해소 ClinicLink(자동 트리거)와 미완료 진행 상태 기준
3) 점수/커트라인/사유(reason)는 source별 시험·과제 정책에서 파생
4) Session ↔ Exam 매핑은 results.utils.session_exam.get_exams_for_session() 단일 진실 사용
- source가 있는 현재 링크는 정확한 시험·과제를 사용한다. source 없는 legacy 링크만
  세션의 가장 작은 exam id를 호환 표시 대상으로 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from academy.adapters.db.django.repositories_clinic_targets import (
    clinic_links_for_admin_targets,
    enrollment_map_for_ids,
    explicit_not_submitted_exam_targets,
    filter_links_by_section,
    first_homework_score,
    homework_cutline_settings_for_target,
    homework_scores_for_target,
    linked_bookings_for_clinic_links,
    regular_exam_for_source,
    regular_homework_for_clinic_target,
)
from apps.domains.results.models import Result, ResultFact, ExamAttempt
from apps.support.results.exam_policy_dependencies import (
    effective_exam_pass_score,
)
from apps.support.attendance.learning_todo_eligibility import (
    learning_todo_eligible_pairs,
)

# ✅ 단일 진실 유틸
from apps.domains.results.utils.clinic import (
    filter_current_clinic_links,
    filter_tenant_consistent_source_links,
)
from apps.domains.results.utils.session_exam import get_exams_for_session
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)


def _safe_str(v: Any, default: str = "-") -> str:
    try:
        s = str(v)
        return s if s.strip() else default
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _newest_first_key(row: Dict[str, Any]) -> tuple[str, int, int]:
    created_at = row.get("created_at")
    created_text = (
        created_at.isoformat()
        if hasattr(created_at, "isoformat")
        else str(created_at or "")
    )
    return (
        created_text,
        int(row.get("clinic_link_id") or 0),
        int(row.get("source_id") or 0),
    )


def _get_student_photo_url(student: Any) -> Optional[str]:
    """R2 presigned URL 생성 (StudentListSerializer.get_profile_photo_url과 동일 로직)"""
    if not student:
        return None
    r2_key = getattr(student, "profile_photo_r2_key", None) or ""
    if not r2_key:
        return None
    try:
        from django.conf import settings
        from academy.adapters.storage.r2_presign import create_presigned_get_url
        return create_presigned_get_url(r2_key, expires_in=3600, bucket=settings.R2_STORAGE_BUCKET)
    except Exception:
        return None


def _extract_invalid_reason_from_meta(meta: Any) -> Optional[str]:
    """
    ResultFact.meta / Attempt.meta / SubmissionAnswer.meta 등 다양한 위치에 있을 수 있는
    invalid_reason을 방어적으로 추출한다.

    기대 가능한 형태:
    meta = { "grading": { "invalid_reason": "LOW_CONFIDENCE" } }
    """
    if not isinstance(meta, dict):
        return None
    grading = meta.get("grading")
    if isinstance(grading, dict):
        v = grading.get("invalid_reason")
        return str(v) if v else None
    return None


def _is_low_confidence_for_attempt(*, exam_id: int, enrollment_id: int, attempt_id: Optional[int]) -> bool:
    """
    "신뢰도 낮음" 판정은 프로젝트 구현에 따라:
    - Attempt.meta.grading.invalid_reason (가능)
    - ResultFact.meta.grading.invalid_reason (가능, 현재 grader는 문항 meta에 심는 형태)
    둘 다 방어적으로 체크한다.
    """
    # 1) Attempt.meta (있으면 최우선)
    if attempt_id:
        a = ExamAttempt.objects.filter(id=int(attempt_id)).first()
        if a and hasattr(a, "meta"):
            reason = _extract_invalid_reason_from_meta(getattr(a, "meta", None))
            if (reason or "").upper() in ("LOW_CONFIDENCE", "AMBIGUOUS_SINGLE"):
                return True

    # 2) ResultFact.meta (대표 attempt 기준)
    if attempt_id:
        qs = (
            ResultFact.objects.filter(
                target_type="exam",
                target_id=int(exam_id),
                enrollment_id=int(enrollment_id),
                attempt_id=int(attempt_id),
            )
            .exclude(meta__isnull=True)
            .order_by("-id")[:200]  # 방어: 너무 큰 scan 방지
        )
        for f in qs:
            r = _extract_invalid_reason_from_meta(getattr(f, "meta", None))
            if (r or "").upper() == "LOW_CONFIDENCE":
                return True

    return False


def _get_session_title(session: Any) -> str:
    """
    세션 타이틀은 프로젝트마다 표현이 달라서:
    - __str__ 우선
    - lecture/title/order 등 후보를 방어적으로 조합
    """
    s = _safe_str(session, "-")
    if s != "-" and s.lower() != "session object":
        return s

    lecture = getattr(session, "lecture", None)
    lecture_title = _safe_str(getattr(lecture, "title", None), "")
    order = getattr(session, "order", None)

    if lecture_title and order is not None:
        return f"{lecture_title} {int(order)}회차"
    if lecture_title:
        return lecture_title

    return f"Session#{int(getattr(session, 'id', 0) or 0)}"


@dataclass(frozen=True)
class ClinicTargetRow:
    enrollment_id: int
    student_name: str
    session_title: str
    reason: str  # "score" | "confidence"
    exam_score: float
    cutline_score: float
    created_at: Any


class ClinicTargetService:
    """
    Admin Clinic Targets

    단일 진실:
    - 대상자 모수: ClinicLink(is_auto=True)
    - enrollment_id 기준
    """

    @staticmethod
    def list_admin_targets(tenant: Any = None, include_resolved: bool = False, section_id: Optional[int] = None) -> List[Dict[str, Any]]:
        # tenant 격리: tenant가 None이면 빈 결과 반환 (cross-tenant 누출 방지)
        if tenant is None:
            return []
        links = clinic_links_for_admin_targets(
            tenant=tenant,
            include_resolved=include_resolved,
        )

        # section 필터: SectionAssignment 기반으로 해당 반 학생만 필터
        if section_id:
            links = filter_links_by_section(
                links,
                tenant=tenant,
                section_id=int(section_id),
            )

        links_list = list(links)
        if links_list:
            links_list = filter_tenant_consistent_source_links(
                links_list,
                tenant=tenant,
            )
        if not include_resolved and links_list:
            links_list = filter_current_clinic_links(links_list, tenant=tenant)
        unresolved_links = [link for link in links_list if link.resolved_at is None]
        if unresolved_links:
            eligible_pairs = learning_todo_eligible_pairs(
                tenant=tenant,
                enrollment_session_pairs={
                    (int(link.enrollment_id), int(link.session_id))
                    for link in unresolved_links
                },
            )
            links_list = [
                link
                for link in links_list
                if link.resolved_at is not None
                or (int(link.enrollment_id), int(link.session_id)) in eligible_pairs
            ]

        # ✅ enrollment 일괄 조회 (N+1 방지 + 학생 SSOT 표시 필드)
        # 🔐 tenant 강제 — links는 tenant 스코프이지만 enrollment_id 참조는 강제 제약 없음.
        all_enrollment_ids = list({int(getattr(lk, "enrollment_id", 0) or 0) for lk in links_list} - {0})
        enrollment_map = enrollment_map_for_ids(
            tenant=tenant,
            enrollment_ids=all_enrollment_ids,
        )
        source_exam_ids = {
            int(getattr(link, "source_id", 0) or 0)
            for link in links_list
            if getattr(link, "source_type", None) == "exam"
            and int(getattr(link, "source_id", 0) or 0) > 0
        }
        initial_exam_scores = load_initial_exam_scores(
            exam_ids=source_exam_ids,
            enrollment_ids=all_enrollment_ids,
        )
        linked_bookings = linked_bookings_for_clinic_links(
            tenant=tenant,
            clinic_link_ids=[int(link.id) for link in links_list],
        )

        # ✅ 클리닉 하이라이트 (미출석 대상자 노란 형광펜)
        from apps.domains.results.utils.clinic_highlight import compute_clinic_highlight_map
        highlight_map = compute_clinic_highlight_map(
            tenant=tenant,
            enrollment_ids=set(all_enrollment_ids),
        ) if tenant else {}

        out: List[Dict[str, Any]] = []

        # 세션별 exam 후보 캐시 (쿼리 절약)
        exams_cache: Dict[int, Optional[Any]] = {}

        for link in links_list:
            session = getattr(link, "session", None)
            if not session:
                continue

            session_id = int(getattr(session, "id", 0) or 0)
            enrollment_id = int(getattr(link, "enrollment_id", 0) or 0)
            if not session_id or not enrollment_id:
                continue

            # clinic_reason 판정
            source_type = getattr(link, "source_type", None)
            clinic_reason = source_type or "exam"

            lecture = getattr(session, "lecture", None)
            lecture_id = int(getattr(session, "lecture_id", 0) or 0)
            lecture_title = _safe_str(getattr(lecture, "title", None), "") if lecture else ""

            # 학생 이름: enrollment_map에서 일괄 조회 (N+1 방지)
            enr = enrollment_map.get(enrollment_id)
            if enr is None:
                continue
            student = getattr(enr, "student", None)
            if student is None:
                continue
            student_name = _safe_str(getattr(student, "name", None), "-")

            # 학생 프로필 필드 (ClinicTargetSelectModal 테이블 컬럼용)
            parent_phone = getattr(student, "parent_phone", None) if student else None
            student_phone = getattr(student, "phone", None) if student else None
            school_type = getattr(student, "school_type", "HIGH") if student else "HIGH"
            if school_type == "ELEMENTARY":
                school_name = getattr(student, "elementary_school", None) if student else None
            elif school_type == "HIGH":
                school_name = getattr(student, "high_school", None) if student else None
            else:
                school_name = getattr(student, "middle_school", None) if student else None
            grade_val = getattr(student, "grade", None) if student else None
            profile_photo_url = _get_student_photo_url(student)

            # 공통 base row
            student_id = int(student.id) if student else None
            base_row = {
                "enrollment_id": enrollment_id,
                "student_id": student_id,
                "_session_id": session_id,
                "session_id": session_id,
                "lecture_id": lecture_id,
                "lecture_title": lecture_title,
                "lecture_color": getattr(lecture, "color", None) if lecture else None,
                "lecture_chip_label": getattr(lecture, "chip_label", None) if lecture else None,
                "clinic_link_id": int(link.id),
                "cycle_no": int(getattr(link, "cycle_no", 1) or 1),
                "resolution_type": getattr(link, "resolution_type", None),
                "resolved_at": getattr(link, "resolved_at", None),
                "resolution_evidence": getattr(link, "resolution_evidence", None),
                "resolution_history": list(getattr(link, "resolution_history", None) or []),
                "linked_bookings": linked_bookings.get(int(link.id), []),
                "student_name": student_name,
                "session_title": _get_session_title(session),
                "source_type": source_type,
                "source_id": getattr(link, "source_id", None),
                "source_scope": None,
                "created_at": getattr(link, "created_at", None),
                "name_highlight_clinic_target": highlight_map.get(enrollment_id, False),
                "parent_phone": parent_phone or "",
                "student_phone": student_phone or "",
                "school": school_name or "",
                "school_type": school_type,
                "grade": grade_val,
                "profile_photo_url": profile_photo_url,
            }

            # ── Homework source ──
            if source_type == "homework":
                source_id = getattr(link, "source_id", None)
                hw = (
                    regular_homework_for_clinic_target(
                        homework_id=int(source_id),
                        tenant=tenant,
                        session_id=session_id,
                    )
                    if source_id else None
                )
                hw_title = _safe_str(getattr(hw, "title", None), "-") if hw else "-"

                # 1차 점수 (성적 산출 대상)
                first_hw_score = first_homework_score(
                    enrollment_id=enrollment_id,
                    session_id=session_id,
                    homework_id=int(source_id) if source_id else 0,
                )

                original_score = float(first_hw_score.score or 0) if first_hw_score and first_hw_score.score is not None else None
                hw_max_score = (
                    float(first_hw_score.max_score)
                    if first_hw_score and first_hw_score.max_score is not None
                    else float(getattr(hw, "default_max_score", 100.0) or 100.0)
                )
                meta_status = (
                    (first_hw_score.meta or {}).get("status")
                    if first_hw_score and isinstance(first_hw_score.meta, dict)
                    else None
                )

                cutline_settings = homework_cutline_settings_for_target(
                    session=session,
                    homework=hw,
                )
                cutline_mode = str(cutline_settings.mode)
                cutline_value = float(cutline_settings.value)
                cutline = (
                    cutline_value
                    if cutline_mode == "COUNT"
                    else hw_max_score * cutline_value / 100.0
                )

                # 재시도 이력
                all_hw_scores = homework_scores_for_target(
                    enrollment_id=enrollment_id,
                    session_id=session_id,
                    homework_id=int(source_id) if source_id else 0,
                )

                attempt_history = []
                latest_attempt_index = 1
                for hs in all_hw_scores:
                    attempt_history.append({
                        "attempt_index": hs.attempt_index,
                        "score": float(hs.score) if hs.score is not None else None,
                        "max_score": float(hs.max_score) if hs.max_score is not None else None,
                        "passed": bool(hs.passed),
                        "at": hs.created_at.isoformat() if hs.created_at else None,
                    })
                    latest_attempt_index = max(latest_attempt_index, hs.attempt_index)

                out.append({
                    **base_row,
                    "exam_id": None,
                    "reason": "missing" if meta_status == "NOT_SUBMITTED" else "score",
                    "clinic_reason": "homework",
                    "exam_score": None,
                    "cutline_score": None,
                    "homework_score": original_score,
                    "homework_cutline": float(cutline),
                    "homework_cutline_mode": cutline_mode,
                    "homework_cutline_value": cutline_value,
                    "homework_round_unit_percent": int(
                        cutline_settings.round_unit_percent
                    ),
                    "meta_status": meta_status,
                    "max_score": hw_max_score,
                    "source_title": hw_title,
                    "latest_attempt_index": latest_attempt_index,
                    "attempt_history": attempt_history,
                })
                continue

            # ── Exam source (기존 로직 + 확장) ──
            source_id = getattr(link, "source_id", None)
            if source_type == "exam" and source_id:
                exam = regular_exam_for_source(
                    exam_id=int(source_id),
                    tenant=tenant,
                    session_id=session_id,
                )
            else:
                # Legacy fallback: 세션의 대표 exam
                if session_id not in exams_cache:
                    exams = list(get_exams_for_session(session))
                    exams_cache[session_id] = sorted(exams, key=lambda x: x.id)[0] if exams else None
                exam = exams_cache.get(session_id)

            if not exam:
                out.append({
                    **base_row,
                    "exam_id": None,
                    "reason": "score",
                    "clinic_reason": clinic_reason,
                    "exam_score": 0.0,
                    "cutline_score": 0.0,
                    "max_score": 0.0,
                    "source_title": "-",
                    "latest_attempt_index": 1,
                    "attempt_history": [],
                })
                continue

            exam_id = int(getattr(exam, "id", 0) or 0)
            cutline = effective_exam_pass_score(
                exam=exam,
                lecture_id=getattr(session, "lecture_id", None),
            )
            exam_max_score = _safe_float(getattr(exam, "max_score", 100.0), 100.0)
            exam_title = _safe_str(getattr(exam, "title", None), "-")

            # 대표 스냅샷 Result (1차 시험 결과 = 성적 산출 대상)
            result = (
                Result.objects.filter(
                    target_type="exam",
                    target_id=exam_id,
                    enrollment_id=enrollment_id,
                )
                .select_related("attempt")
                .order_by("-id")
                .first()
            )

            initial_state = initial_exam_scores.get((exam_id, enrollment_id))
            if initial_state is None and exam_id not in source_exam_ids:
                initial_state = load_initial_exam_scores(
                    exam_ids=[exam_id],
                    enrollment_ids=[enrollment_id],
                ).get((exam_id, enrollment_id))
            initial_score = project_initial_exam_score(
                state=initial_state,
                fallback_score=(getattr(result, "total_score", None) if result else None),
                fallback_max_score=(getattr(result, "max_score", None) if result else exam_max_score),
                fallback_not_submitted=bool(
                    result
                    and result.attempt_id
                    and isinstance(result.attempt.meta, dict)
                    and result.attempt.meta.get("status") == "NOT_SUBMITTED"
                ),
            )
            exam_score = _safe_float(initial_score.total_score, 0.0)
            visible_max_score = _safe_float(initial_score.max_score, exam_max_score)
            attempt_id = int(initial_score.attempt_id or 0)
            attempt_meta_status = "NOT_SUBMITTED" if initial_score.not_submitted else None

            # 재시도 이력 (ExamAttempt 전체)
            all_attempts = ExamAttempt.objects.filter(
                exam_id=exam_id,
                enrollment_id=enrollment_id,
            ).order_by("attempt_index")

            attempt_history = []
            latest_attempt_index = 1
            for att in all_attempts:
                att_score = None
                att_passed = False
                meta = att.meta or {}
                if "total_score" in meta:
                    att_score = float(meta["total_score"])
                    att_passed = att_score >= cutline if cutline > 0 else True
                elif att.attempt_index == 1 and result:
                    att_score = float(exam_score)
                    att_passed = att_score >= cutline if cutline > 0 else True

                attempt_history.append({
                    "attempt_index": att.attempt_index,
                    "score": att_score,
                    "max_score": _safe_float(meta.get("max_score"), visible_max_score),
                    "passed": att_passed,
                    "at": att.created_at.isoformat() if att.created_at else None,
                })
                latest_attempt_index = max(latest_attempt_index, att.attempt_index)

            # reason 판정
            if attempt_meta_status == "NOT_SUBMITTED":
                reason = "missing"
            else:
                reason = "confidence" if _is_low_confidence_for_attempt(
                    exam_id=exam_id,
                    enrollment_id=enrollment_id,
                    attempt_id=attempt_id if attempt_id else None,
                ) else "score"

            out.append({
                **base_row,
                "exam_id": exam_id,
                "reason": reason,
                "clinic_reason": clinic_reason,
                "exam_score": float(exam_score),
                "cutline_score": float(cutline),
                "meta_status": attempt_meta_status,
                "max_score": visible_max_score,
                "source_title": exam_title,
                "latest_attempt_index": latest_attempt_index,
                "attempt_history": attempt_history,
            })

        # 명시적으로 미응시 처리된 시험은 점수 미달과 구분된 "판정 대기" 행이다.
        # 조회가 ClinicLink를 만들지는 않는다. 사용자가 면제 사유를 확정할 때만
        # source-specific WAIVED 이력을 생성한다.
        explicit_missing_targets = list(
            explicit_not_submitted_exam_targets(
                tenant=tenant,
                section_id=section_id,
            )
        )
        eligible_explicit_pairs = learning_todo_eligible_pairs(
            tenant=tenant,
            enrollment_session_pairs={
                (int(result.enrollment_id), int(session.id))
                for result, session in explicit_missing_targets
            },
        )
        for result, session in explicit_missing_targets:
            if (int(result.enrollment_id), int(session.id)) not in eligible_explicit_pairs:
                continue
            enrollment = result.enrollment
            student = getattr(enrollment, "student", None)
            exam = result.attempt.exam
            lecture = session.lecture
            school_type = getattr(student, "school_type", "HIGH") if student else "HIGH"
            if school_type == "ELEMENTARY":
                school_name = getattr(student, "elementary_school", None) if student else None
            elif school_type == "HIGH":
                school_name = getattr(student, "high_school", None) if student else None
            else:
                school_name = getattr(student, "middle_school", None) if student else None
            out.append({
                "enrollment_id": int(enrollment.id),
                "student_id": int(student.id) if student else None,
                "student_name": _safe_str(getattr(student, "name", None), "-"),
                "session_title": _get_session_title(session),
                "reason": "missing",
                "clinic_reason": "exam",
                "exam_score": None,
                "cutline_score": effective_exam_pass_score(
                    exam=exam,
                    lecture_id=getattr(session, "lecture_id", None),
                ),
                "meta_status": "NOT_SUBMITTED",
                "clinic_link_id": None,
                "cycle_no": 1,
                "resolution_type": None,
                "resolved_at": None,
                "session_id": int(session.id),
                "lecture_id": int(lecture.id),
                "exam_id": int(exam.id),
                "source_type": "exam",
                "source_id": int(exam.id),
                "source_title": _safe_str(exam.title, "-"),
                "source_scope": None,
                "lecture_title": _safe_str(lecture.title, ""),
                "lecture_color": getattr(lecture, "color", None),
                "lecture_chip_label": getattr(lecture, "chip_label", None),
                "name_highlight_clinic_target": False,
                "parent_phone": getattr(student, "parent_phone", None) or "",
                "student_phone": getattr(student, "phone", None) or "",
                "school": school_name or "",
                "school_type": school_type,
                "grade": getattr(student, "grade", None) if student else None,
                "profile_photo_url": _get_student_photo_url(student),
                "max_score": _safe_float(exam.max_score, 100.0),
                "latest_attempt_index": 0,
                "attempt_history": [],
                "created_at": getattr(result.attempt, "created_at", None),
            })

        return sorted(out, key=_newest_first_key, reverse=True)
