# PATH: apps/domains/results/views/admin_student_grades_view.py
"""
GET /results/admin/student-grades/?student_id=<int>

Admin/Teacher용 학생 개인 성적 요약과 전체 기간 시험 추이.
시험 결과가 추가될 때마다 별도 집계 작업 없이 회차가 자동 누적된다.
"""
from django.db.models.functions import Coalesce
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.domains.results.permissions import IsTeacherOrAdmin
from apps.domains.results.models import Result
from apps.domains.results.services.omr_subjective_completion import (
    pending_omr_result_ids_for_ids,
)
from apps.domains.results.utils.exam_achievement import compute_exam_achievement_bulk
from apps.domains.results.utils.ranking import compute_exam_rankings_batch
from apps.domains.results.utils.initial_exam_score import (
    load_initial_exam_scores,
    project_initial_exam_score,
)
from apps.support.results.admin_student_grades_dependencies import (
    active_student_for_grades,
    enrollment_ids_for_student,
    enrollment_lecture_metadata_by_id,
    exam_metadata_by_id,
    homework_assignments_for_grades,
    homework_retake_counts_by_key,
    homework_scores_for_grades,
    primary_session_metadata_by_exam_and_lecture,
    resolved_homework_link_types,
    submitted_homework_keys_for_grades,
)
from apps.support.results.student_grade_history import (
    build_exam_progression as _build_exam_progression,
    empty_exam_summary as _empty_exam_summary,
    is_json_safe_number as _is_json_safe_number,
)


def _empty_payload() -> dict:
    return {
        "exams": [],
        "homeworks": [],
        "exam_trend": [],
        "exam_summary": _empty_exam_summary(),
    }


class AdminStudentGradesView(APIView):
    """
    학생 개인 시험 + 과제 성적 요약 (admin/teacher 공용).
    Query params: student_id (required)
    """
    permission_classes = [IsAuthenticated, IsTeacherOrAdmin]

    def get(self, request):
        student_id = request.query_params.get("student_id")
        if not student_id:
            return Response({"detail": "student_id is required"}, status=400)
        try:
            parsed_student_id = int(student_id)
        except (TypeError, ValueError):
            return Response({"detail": "student_id must be integer"}, status=400)

        tenant = getattr(request, "tenant", None)
        if not tenant:
            return Response(_empty_payload())

        # tenant/deleted-state isolation: 해당 테넌트의 활성 학생인지 확인
        student = active_student_for_grades(tenant=tenant, student_id=parsed_student_id)
        if not student:
            return Response({"detail": "student not found"}, status=404)

        enrollment_ids = enrollment_ids_for_student(tenant=tenant, student_id=student.id)
        if not enrollment_ids:
            return Response(_empty_payload())

        # ── 시험 결과 ──
        results = list(
            Result.objects.filter(
                enrollment_id__in=enrollment_ids,
                target_type="exam",
            )
            .annotate(recorded_at=Coalesce("submitted_at", "created_at"))
            .order_by("-recorded_at", "-id")
            .values(
                "id",
                "target_id",
                "enrollment_id",
                "total_score",
                "max_score",
                "submitted_at",
                "recorded_at",
                "attempt_id",
            )
        )
        pending_result_ids = pending_omr_result_ids_for_ids(
            row["id"] for row in results
        )
        exam_ids = list({r["target_id"] for r in results})
        initial_scores = load_initial_exam_scores(
            exam_ids=exam_ids,
            enrollment_ids=enrollment_ids,
        )

        exams_map = {}
        if exam_ids:
            # 🔐 tenant 강제 — Exam.tenant FK 존재.
            exams_map = exam_metadata_by_id(tenant=tenant, exam_ids=exam_ids)
        # 재시도 횟수 (bulk)
        retake_counts = {}
        if exam_ids and enrollment_ids:
            from django.db.models import Max
            from apps.domains.results.models import ExamAttempt as _EA
            for att in _EA.objects.filter(
                exam_id__in=exam_ids,
                enrollment_id__in=enrollment_ids,
            ).values("exam_id", "enrollment_id").annotate(max_attempt=Max("attempt_index")):
                retake_counts[(att["enrollment_id"], att["exam_id"])] = att["max_attempt"]

        # 세션/강의 정보 — enrollment → lecture 매핑
        # 🔐 enrollment_ids는 line 44에서 이미 tenant 필터됨. 명시적으로 한 번 더.
        enrollment_lecture_map = {}
        if enrollment_ids:
            enrollment_lecture_map = enrollment_lecture_metadata_by_id(
                tenant=tenant,
                enrollment_ids=enrollment_ids,
            )
        exam_lecture_pairs = {
            (int(r["target_id"]), int(enrollment_lecture_map[r["enrollment_id"]]["lecture_id"]))
            for r in results
            if r["target_id"] in exams_map
            and r["enrollment_id"] in enrollment_lecture_map
            and enrollment_lecture_map[r["enrollment_id"]].get("lecture_id") is not None
        }
        primary_session_map = primary_session_metadata_by_exam_and_lecture(
            tenant=tenant,
            exam_lecture_pairs=exam_lecture_pairs,
        )

        # ── Stage 1: exam 단위 dedup + session lookup + 시스템 강의 스킵
        exam_rows = []  # [(r, eid, info, session, lecture_meta)]
        seen_exam_ids = set()
        for r in results:
            eid = r["target_id"]
            if eid in seen_exam_ids:
                continue
            # Generic Result.target_id가 손상되어 다른 tenant 시험을 가리켜도
            # 제목/차시/강의 metadata를 절대 fallback 노출하지 않는다.
            info = exams_map.get(eid)
            if not info:
                continue
            enroll_info = enrollment_lecture_map.get(r["enrollment_id"])
            if not enroll_info or enroll_info.get("lecture_is_system"):
                continue
            enrollment_lecture_id = enroll_info.get("lecture_id")
            session_meta = primary_session_map.get(
                (int(eid), int(enrollment_lecture_id)),
            ) if enrollment_lecture_id is not None else None
            session_meta = session_meta or {}

            # 시스템 강의(공개 영상 컨테이너)는 성적에서 제외
            if session_meta.get("lecture_is_system"):
                continue
            projected_score = project_initial_exam_score(
                state=initial_scores.get((int(eid), int(r["enrollment_id"]))),
                fallback_score=r.get("total_score"),
                fallback_max_score=r.get("max_score"),
            )
            if (
                projected_score.total_score is None
                and not projected_score.not_submitted
            ) or not _is_json_safe_number(projected_score.max_score):
                continue

            session_id = session_meta.get("session_id")
            session_title = session_meta.get("session_title")
            lecture_id = session_meta.get("lecture_id")
            lecture_title = session_meta.get("lecture_title")
            lecture_color = session_meta.get("lecture_color")
            lecture_chip_label = session_meta.get("lecture_chip_label")

            # session은 반드시 Result enrollment의 강의와 일치한다. 연결된
            # session이 없으면 같은 enrollment 강의 metadata로만 fallback한다.
            if not lecture_title:
                lecture_id = lecture_id or enroll_info["lecture_id"]
                lecture_title = lecture_title or enroll_info["lecture_title"]
                lecture_color = lecture_color or enroll_info["lecture_color"]
                lecture_chip_label = lecture_chip_label or enroll_info["lecture_chip_label"]

            seen_exam_ids.add(eid)
            exam_rows.append({
                "r": r, "eid": eid, "info": info, "session": None,
                "session_id": session_id, "session_title": session_title,
                "session_order": session_meta.get("session_order"),
                "session_regular_order": session_meta.get("session_regular_order"),
                "session_type": session_meta.get("session_type"),
                "session_date": session_meta.get("session_date"),
                "lecture_id": lecture_id, "lecture_title": lecture_title,
                "lecture_color": lecture_color, "lecture_chip_label": lecture_chip_label,
                "initial_score": projected_score,
                "subjective_pending": int(r["id"]) in pending_result_ids,
            })

        # ── Stage 2: SSOT 유틸로 성취 일괄 계산 (ClinicLink/ExamAttempt/ExamResult 각 1쿼리)
        bulk_items = [
            {
                "enrollment_id": row["r"]["enrollment_id"],
                "exam_id": row["eid"],
                "total_score": row["initial_score"].total_score or 0.0,
                "pass_score": row["info"]["pass_score_by_lecture"].get(
                    int(row["lecture_id"]),
                    row["info"]["pass_score"],
                ),
                "attempt_id": row["r"].get("attempt_id"),
                "session": row["session"],
            }
            for row in exam_rows
            if not row["subjective_pending"]
        ]
        # admin_student_grades_view 는 exam 별로 대표 session 을 이미 선택했으므로
        # (동일 exam 이 여러 session 에 걸려도 primary 만 사용), session-agnostic 매칭.
        # 정책 동등성(HOMEWORK_PASS 케이스)은 test_achievement_contract.py 가 보장.
        bulk_ach = compute_exam_achievement_bulk(
            items=bulk_items, use_session_filter=False, tenant=tenant,
        )
        exam_rank_maps = compute_exam_rankings_batch(
            exam_ids=[
                int(row["eid"])
                for row in exam_rows
                if not row["subjective_pending"]
            ],
            enrollment_ids=enrollment_ids,
            tenant=tenant,
        )

        # ── Stage 3: 응답 row 조립
        exam_list = []
        for row in exam_rows:
            r = row["r"]
            eid = row["eid"]
            info = row["info"]
            enroll_id = r["enrollment_id"]
            ach_data = bulk_ach.get((int(enroll_id), int(eid)), {})
            subjective_pending = bool(row["subjective_pending"])
            meta_status = None if subjective_pending else ach_data.get("meta_status")
            is_not_submitted = meta_status == "NOT_SUBMITTED"
            max_attempt = retake_counts.get((enroll_id, eid), 1)
            rank_info = (
                {}
                if is_not_submitted or subjective_pending
                else exam_rank_maps.get(int(eid), {}).get(int(enroll_id), {})
            )

            exam_list.append({
                "exam_id": eid,
                "enrollment_id": enroll_id,
                "title": info["title"],
                "total_score": (
                    None
                    if is_not_submitted or subjective_pending
                    else row["initial_score"].total_score
                ),
                "max_score": row["initial_score"].max_score,
                "is_pass": None if subjective_pending else ach_data.get("is_pass"),
                "achievement": None if subjective_pending else ach_data.get("achievement"),
                "meta_status": meta_status,
                "grading_status": "subjective_pending" if subjective_pending else None,
                "is_provisional": subjective_pending,
                "retake_count": max_attempt,
                "session_id": row["session_id"],
                "session_title": row["session_title"],
                "session_order": row["session_order"],
                "session_regular_order": row["session_regular_order"],
                "session_type": row["session_type"],
                "session_date": row["session_date"].isoformat() if row["session_date"] else None,
                "lecture_id": row["lecture_id"],
                "lecture_title": row["lecture_title"],
                "lecture_color": row["lecture_color"],
                "lecture_chip_label": row["lecture_chip_label"],
                "submitted_at": r["submitted_at"].isoformat() if r.get("submitted_at") else None,
                "recorded_at": r["recorded_at"].isoformat(),
                "archived": not info["is_active"],
                "rank": rank_info.get("rank"),
                "percentile": rank_info.get("percentile"),
                "cohort_size": rank_info.get("cohort_size"),
                "cohort_avg": rank_info.get("cohort_avg"),
            })

        exam_trend, exam_summary = _build_exam_progression(exam_list)

        # ── 과제 성적 ──
        hw_scores = list(homework_scores_for_grades(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        ))
        hw_assignments = list(homework_assignments_for_grades(
            tenant=tenant,
            enrollment_ids=enrollment_ids,
        ))

        # 점수 행을 우선 유지하고, 아직 채점 행이 없는 배정 과제를 뒤에 합친다.
        # 배정 테이블 도입 이전의 점수 데이터도 사라지지 않도록 score-only 행은
        # 호환 노출한다.
        homework_sources = []
        seen_hw_key = set()
        for hs in hw_scores:
            key = (hs.homework_id, hs.session_id, hs.enrollment_id)
            if key in seen_hw_key:
                continue
            seen_hw_key.add(key)
            homework_sources.append((hs.homework, hs.session, hs.enrollment_id, hs))
        for assignment in hw_assignments:
            key = (
                assignment.homework_id,
                assignment.session_id,
                assignment.enrollment_id,
            )
            if key in seen_hw_key:
                continue
            seen_hw_key.add(key)
            homework_sources.append((
                assignment.homework,
                assignment.session,
                assignment.enrollment_id,
                None,
            ))

        hw_ids = list({homework.id for homework, _session, _enrollment_id, _hs in homework_sources})
        submitted_hw_keys = set()
        if hw_ids and enrollment_ids:
            submitted_hw_keys = submitted_homework_keys_for_grades(
                tenant=tenant,
                enrollment_ids=enrollment_ids,
                homework_ids=hw_ids,
            )
        resolved_hw_links = {}
        if hw_ids and enrollment_ids:
            resolved_hw_links = resolved_homework_link_types(
                tenant=tenant,
                enrollment_ids=enrollment_ids,
                homework_ids=hw_ids,
            )

        hw_retake_counts = {}
        if hw_ids and enrollment_ids:
            hw_retake_counts = homework_retake_counts_by_key(
                tenant=tenant,
                enrollment_ids=enrollment_ids,
                homework_ids=hw_ids,
            )

        homework_list = []
        for homework, session, enrollment_id, hs in homework_sources:
            score = hs.score if hs is not None else None
            max_score = homework.default_max_score
            if not _is_json_safe_number(score) or not _is_json_safe_number(max_score):
                continue
            session_id_hw = None
            session_title = None
            session_order_hw = None
            session_regular_order_hw = None
            session_type_hw = None
            session_date_hw = None
            lecture_id_hw = None
            lecture_title = None
            lecture_color = None
            lecture_chip_label = None
            if session:
                session_id_hw = session.id
                session_title = getattr(session, "title", None) or f"{getattr(session, 'order', '')}차시"
                session_order_hw = int(session.order)
                session_regular_order_hw = (
                    int(session.regular_order)
                    if getattr(session, "regular_order", None) is not None
                    else None
                )
                session_type_hw = getattr(session, "session_type", None)
                session_date_hw = getattr(session, "date", None)
                if hasattr(session, "lecture") and session.lecture:
                    lecture_id_hw = session.lecture_id
                    lecture_title = getattr(session.lecture, "title", None)
                    lecture_color = getattr(session.lecture, "color", None)
                    lecture_chip_label = getattr(session.lecture, "chip_label", None)

            meta = hs.meta if hs is not None and isinstance(hs.meta, dict) else {}
            meta_status = meta.get("status")
            if (
                meta_status is None
                and score is None
                and (enrollment_id, homework.id) not in submitted_hw_keys
            ):
                meta_status = "NOT_SUBMITTED"
            is_pass_1st = bool(hs.passed) if hs is not None else None
            resolution = resolved_hw_links.get((enrollment_id, homework.id))
            max_attempt = hw_retake_counts.get((enrollment_id, homework.id), 0)

            if meta_status == "NOT_SUBMITTED":
                achievement = "NOT_SUBMITTED"
            elif hs is None or score is None:
                achievement = None
            elif is_pass_1st:
                achievement = "PASS"
            elif resolution in ("EXAM_PASS", "HOMEWORK_PASS", "MANUAL_OVERRIDE"):
                achievement = "REMEDIATED"
            else:
                achievement = "FAIL"

            homework_list.append({
                "homework_id": homework.id,
                "enrollment_id": enrollment_id,
                "title": homework.title or f"과제 #{homework.id}",
                "grading_mode": homework.grading_mode,
                "score": score,
                "max_score": max_score,
                "passed": is_pass_1st,
                "is_locked": bool(hs.is_locked) if hs is not None else False,
                "lock_reason": hs.lock_reason if hs is not None else None,
                "score_updated_at": hs.updated_at.isoformat() if hs is not None else None,
                "meta_status": meta_status,
                "achievement": achievement,
                "retake_count": max_attempt,
                "display_order": homework.display_order,
                "session_id": session_id_hw,
                "session_title": session_title,
                "session_order": session_order_hw,
                "session_regular_order": session_regular_order_hw,
                "session_type": session_type_hw,
                "session_date": session_date_hw.isoformat() if session_date_hw else None,
                "lecture_id": lecture_id_hw,
                "lecture_title": lecture_title,
                "lecture_color": lecture_color,
                "lecture_chip_label": lecture_chip_label,
            })

        homework_list.sort(key=lambda row: (
            row.get("session_date") is None,
            -int(row["session_date"].replace("-", "")) if row.get("session_date") else 0,
            row.get("session_order") is None,
            -(int(row["session_order"]) if row.get("session_order") is not None else 0),
            (row.get("lecture_title") or "").casefold(),
            int(row.get("display_order") or 0),
            -int(row["homework_id"]),
        ))

        return Response({
            "exams": exam_list,
            "homeworks": homework_list,
            "exam_trend": exam_trend,
            "exam_summary": exam_summary,
        })
