# apps/domains/results/utils/exam_achievement.py
"""
시험 성취(achievement) 계산 SSOT.

정책 (SSOT):
- is_pass: 1차 시험만 기준 (석차 계산용). pass_score=0이면 None.
- remediated: 1차 불합격 후 ClinicLink(source_type=exam, resolution_type∈{EXAM_PASS, MANUAL_OVERRIDE})로 해소.
  (WAIVED=면제는 remediated=False; 성취로 인정하지 않음)
- final_pass: 1차 합격 OR remediated. 미응시 + 미해소 → None.
- achievement: "PASS" | "REMEDIATED" | "FAIL" | "NOT_SUBMITTED" | None
  · PASS: 1차 합격
  · REMEDIATED: 1차 불합격 + 해소(EXAM_PASS/MANUAL_OVERRIDE)
  · FAIL: 1차 불합격 + 미해소
  · NOT_SUBMITTED: 미응시(+미해소)
  · None: pass_score=0 등 판정 기준 없음
- is_provisional: ExamResult.status ≠ FINAL → 채점 미확정

여러 뷰에서 공통 사용해 드리프트 재발 방지.
"""
from __future__ import annotations

from typing import Any

from apps.domains.results.models import ExamResult, ExamAttempt, Result
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids,
)
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.support.results.progress_read_dependencies import (
    exam_remediation_link_values,
)
from apps.support.progress.assessment_correction_dependencies import (
    stale_teacher_exam_resolution_link_ids,
)


def compute_first_pass(
    *,
    total_score: float | None,
    pass_score: float | None,
    is_not_submitted: bool,
) -> bool | None:
    """1차 합격 판정. 석차/성취 분리용 단일 진실."""
    if is_not_submitted:
        return None
    if pass_score is None or float(pass_score) <= 0:
        return None
    return float(total_score or 0.0) >= float(pass_score)


def compute_final_pass(
    *,
    is_pass: bool | None,
    remediated: bool,
    is_not_submitted: bool,
) -> bool | None:
    """최종 합격 = 1차 합격 OR 클리닉 해소. 미응시+미해소 → None."""
    if is_not_submitted and not remediated:
        return None
    if is_pass is True or remediated:
        return True
    if is_pass is False:
        return False
    return None


def compute_achievement(
    *,
    is_pass: bool | None,
    remediated: bool,
    is_not_submitted: bool,
) -> str | None:
    """
    성취 분류. 프론트 뱃지/통계 공용 라벨.

    우선순위: REMEDIATED > PASS > NOT_SUBMITTED > FAIL
    (보강합격이 1차 합격보다 우선하는 건 아니지만, 판정 흐름상 remediated=True면 1차는 false)
    """
    if remediated:
        return "REMEDIATED"
    if is_pass is True:
        return "PASS"
    if is_not_submitted:
        return "NOT_SUBMITTED"
    if is_pass is False:
        return "FAIL"
    return None


def compute_exam_achievement(
    *,
    enrollment_id: int,
    exam_id: int,
    session,
    total_score: float | None,
    pass_score: float | None,
    attempt_id: int | None = None,
    meta_status: str | None = None,
    tenant: Any,
) -> dict[str, Any]:
    """
    시험 하나에 대해 학생의 성취 상태를 통합 계산.

    student_result_service와 admin_exam_results_view가 공통 사용.
    입력:
      - enrollment_id, exam_id: 대상 학생/시험
      - session: 해당 시험의 대표 session (없으면 None — 클리닉 판정 스킵)
      - total_score, pass_score: Result / Exam 기반 점수
      - attempt_id: 대표 attempt (is_provisional / meta_status 판정용)
      - meta_status: 정확한 attempt에 상태가 없을 때만 쓰는 명시적 fallback
      - tenant: attempt/submission/clinic 관계를 검증할 필수 tenant

    반환: {
        is_pass, remediated, final_pass, clinic_retake, achievement,
        is_provisional, meta_status
    }
    """
    item = {
        "enrollment_id": enrollment_id,
        "exam_id": exam_id,
        "total_score": total_score,
        "pass_score": pass_score,
        "attempt_id": attempt_id,
        "session": session,
    }
    data = compute_exam_achievement_bulk(
        items=[item],
        use_session_filter=True,
        tenant=tenant,
    )[(int(enrollment_id), int(exam_id))]
    if meta_status is not None and data["meta_status"] is None:
        is_not_submitted = meta_status == "NOT_SUBMITTED"
        data["meta_status"] = meta_status
        data["is_pass"] = compute_first_pass(
            total_score=total_score,
            pass_score=pass_score,
            is_not_submitted=is_not_submitted,
        )
        data["final_pass"] = compute_final_pass(
            is_pass=data["is_pass"],
            remediated=data["remediated"],
            is_not_submitted=is_not_submitted,
        )
        data["achievement"] = compute_achievement(
            is_pass=data["is_pass"],
            remediated=data["remediated"],
            is_not_submitted=is_not_submitted,
        )
    return data


def _build_clinic_retake_info(link_or_dict) -> dict[str, Any]:
    """ClinicLink 인스턴스 or values() dict 에서 clinic_retake info 생성."""
    if hasattr(link_or_dict, "resolution_evidence"):
        evidence = link_or_dict.resolution_evidence or {}
        resolved_at = link_or_dict.resolved_at
        resolution_type = link_or_dict.resolution_type
    else:
        evidence = link_or_dict.get("resolution_evidence") or {}
        resolved_at = link_or_dict.get("resolved_at")
        resolution_type = link_or_dict.get("resolution_type")
    return {
        "score": evidence.get("score"),
        "pass_score": evidence.get("pass_score"),
        "attempt_id": evidence.get("attempt_id"),
        "resolution_type": resolution_type,
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
    }


def compute_exam_achievement_bulk(
    *,
    items: list[dict[str, Any]],
    use_session_filter: bool = True,
    tenant: Any,
) -> dict[tuple[int, int], dict[str, Any]]:
    """
    여러 (enrollment, exam) 쌍의 성취를 한 번에 계산 (N+1 방지).

    입력 (items 각각):
      {
        "enrollment_id": int,
        "exam_id": int,
        "total_score": float | None,
        "pass_score": float | None,
        "attempt_id": int | None,
        "session": Session | None,  # None 이면 클리닉 판정 스킵
      }

    use_session_filter=True (기본):
        ClinicLink 조회 시 session_id 도 조건에 포함 —
        동일 (enrollment, exam)이 여러 session 을 거칠 때 session 일치만 인정.
    use_session_filter=False:
        session 무관하게 (enrollment, exam)로만 매칭 —
        admin_student_grades_view 처럼 이미 exam 별로 대표 session 을 고른 뷰용.

    반환: { (enrollment_id, exam_id): achievement_data }
    compute_exam_achievement 와 동일한 키 셋 (is_pass/remediated/final_pass/
    clinic_retake/achievement/is_provisional/meta_status).

    쿼리 수: items 수와 무관하게 ClinicLink/ExamAttempt/ExamResult 각 1회 (최대 3개).
    """
    if not items:
        return {}

    enrollment_ids = {int(i["enrollment_id"]) for i in items}
    exam_ids = {int(i["exam_id"]) for i in items}
    attempt_ids = {int(i["attempt_id"]) for i in items if i.get("attempt_id")}
    session_ids = {
        int(i["session"].id) for i in items
        if use_session_filter and i.get("session") is not None
    }
    initial_scores = load_initial_exam_scores(
        exam_ids=exam_ids,
        enrollment_ids=enrollment_ids,
    )
    attempt_ids.update(
        state.attempt_id
        for state in initial_scores.values()
        if state.attempt_id is not None
    )

    # 1) ClinicLink bulk fetch (remediated 판정)
    # key 구조:
    #   use_session_filter=True  → (enrollment_id, exam_id, session_id or None)
    #   use_session_filter=False → (enrollment_id, exam_id, None) — session 무관
    # 동일 key 에 link 가 여러 개면 resolved_at 최신만 보관.
    link_map: dict[tuple[int, int, int | None], dict[str, Any]] = {}
    remediation_links = exam_remediation_link_values(
        enrollment_ids=enrollment_ids,
        exam_ids=exam_ids,
        session_ids=session_ids,
        use_session_filter=use_session_filter,
        tenant=tenant,
    )
    stale_teacher_link_ids = stale_teacher_exam_resolution_link_ids(
        links=remediation_links,
        tenant_id=int(tenant.id),
    )
    for cl in remediation_links:
        if int(cl["id"]) in stale_teacher_link_ids:
            continue
        if use_session_filter:
            sid = int(cl["session_id"]) if cl.get("session_id") is not None else None
        else:
            sid = None
        key = (int(cl["enrollment_id"]), int(cl["source_id"]), sid)
        prev = link_map.get(key)
        if prev is None:
            link_map[key] = cl
            continue
        # order by -resolved_at (latest wins)
        prev_at = prev.get("resolved_at")
        cur_at = cl.get("resolved_at")
        if cur_at and (prev_at is None or cur_at > prev_at):
            link_map[key] = cl

    # 2) ExamAttempt.meta.status + submission_id bulk
    meta_status_by_attempt: dict[int, str | None] = {}
    submission_by_attempt: dict[int, int] = {}
    pair_by_attempt: dict[int, tuple[int, int]] = {}
    if attempt_ids:
        for a in ExamAttempt.objects.filter(id__in=attempt_ids).only(
            "id", "meta", "submission_id", "exam_id", "enrollment_id",
        ):
            m = a.meta if isinstance(a.meta, dict) else {}
            meta_status_by_attempt[int(a.id)] = m.get("status")
            pair_by_attempt[int(a.id)] = (int(a.enrollment_id), int(a.exam_id))
            if a.submission_id:
                submission_by_attempt[int(a.id)] = int(a.submission_id)

    result_rows = list(
        Result.objects.filter(
            target_type="exam",
            target_id__in=exam_ids,
            enrollment_id__in=enrollment_ids,
            enrollment__tenant_id=int(tenant.id),
        )
    )
    pending_result_ids = pending_omr_result_ids(result_rows)
    pending_result_pairs = {
        (int(result.enrollment_id), int(result.target_id))
        for result in result_rows
        if result.enrollment_id is not None
        and int(result.id) in pending_result_ids
    }

    # 3) ExamResult.status bulk (is_provisional 판정)
    er_status_by_pair: dict[tuple[int, int, int], str] = {}
    if submission_by_attempt:
        sub_ids = set(submission_by_attempt.values())
        for er in ExamResult.objects.filter(
            submission_id__in=sub_ids,
            exam_id__in=exam_ids,
            submission__tenant=tenant,
            submission__enrollment__tenant=tenant,
            submission__target_type="exam",
        ).values(
            "submission_id",
            "submission__enrollment_id",
            "submission__target_id",
            "exam_id",
            "status",
        ):
            enrollment_id = er["submission__enrollment_id"]
            target_exam_id = er["submission__target_id"]
            exam_id = er["exam_id"]
            if enrollment_id is None or int(target_exam_id) != int(exam_id):
                continue
            er_status_by_pair[(
                int(er["submission_id"]),
                int(enrollment_id),
                int(exam_id),
            )] = er["status"]

    # 4) per-item 계산 (쿼리 없음)
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for i in items:
        e_id = int(i["enrollment_id"])
        x_id = int(i["exam_id"])
        supplied_attempt_id = int(i["attempt_id"]) if i.get("attempt_id") else None
        initial_state = initial_scores.get((x_id, e_id))
        initial_attempt_id = initial_state.attempt_id if initial_state else None
        # 1차 상태가 있으면 Result가 가리키는 최신 대표 재시험이 아니라 1차의
        # 미응시/확정 상태를 쓴다. 첫 시도가 없는 legacy만 Result.attempt로 호환한다.
        candidate_attempt_id = initial_attempt_id or supplied_attempt_id
        a_id = candidate_attempt_id if (
            candidate_attempt_id is not None
            and pair_by_attempt.get(candidate_attempt_id) == (e_id, x_id)
        ) else None
        session = i.get("session")
        sid = int(session.id) if (use_session_filter and session is not None) else None

        meta_status = meta_status_by_attempt.get(a_id) if a_id else None
        initial_score = project_initial_exam_score(
            state=initial_state,
            fallback_score=i.get("total_score"),
            fallback_max_score=None,
            fallback_not_submitted=meta_status == "NOT_SUBMITTED",
        )
        is_not_submitted = initial_score.not_submitted
        if is_not_submitted:
            meta_status = "NOT_SUBMITTED"

        is_pass = compute_first_pass(
            total_score=initial_score.total_score,
            pass_score=i.get("pass_score"),
            is_not_submitted=is_not_submitted,
        )

        # remediated 조회 키:
        #  - use_session_filter=True: session 이 있을 때만 (e, x, session.id) 로 조회
        #  - use_session_filter=False: session 유무 무관하게 (e, x, None) 으로 조회
        link_info = None
        if use_session_filter:
            if session is not None:
                link_info = link_map.get((e_id, x_id, sid))
        else:
            link_info = link_map.get((e_id, x_id, None))
        if link_info is not None:
            remediated = True
            clinic_retake = _build_clinic_retake_info(link_info)
        else:
            remediated = False
            clinic_retake = None

        final_pass = compute_final_pass(
            is_pass=is_pass, remediated=remediated, is_not_submitted=is_not_submitted,
        )
        achievement = compute_achievement(
            is_pass=is_pass, remediated=remediated, is_not_submitted=is_not_submitted,
        )

        is_provisional = False
        if a_id and a_id in submission_by_attempt:
            er_status = er_status_by_pair.get((
                submission_by_attempt[a_id],
                e_id,
                x_id,
            ))
            is_provisional = bool(er_status and er_status != ExamResult.Status.FINAL)

        # First-attempt scoring remains the achievement SSOT, but a newer
        # representative OMR result can still be awaiting essay grading. In
        # that state no final pass/remediation label may be projected yet.
        if (e_id, x_id) in pending_result_pairs:
            is_pass = None
            remediated = False
            clinic_retake = None
            final_pass = None
            achievement = None
            is_provisional = True

        out[(e_id, x_id)] = {
            "is_pass": is_pass,
            "remediated": remediated,
            "final_pass": final_pass,
            "clinic_retake": clinic_retake,
            "achievement": achievement,
            "is_provisional": is_provisional,
            "meta_status": meta_status,
        }

    return out
