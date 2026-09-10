# apps/domains/progress/services/session_calculator.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone
from django.db.models import Count

from apps.domains.progress.models import SessionProgress, ProgressPolicy
from apps.support.progress.exam_policy_dependencies import (
    exam_pass_score_overrides,
)
from apps.support.progress.session_calculator_dependencies import (
    get_target_exam_ids_for_session_enrollment,
    get_exam_model,
    get_result_attempt_models,
    homework_score_exists,
)


@dataclass(frozen=True)
class ExamAggregationReadSources:
    """Already scoped, snapshot-consistent inputs; contains no alternate policy."""

    exam_ids: list[int]
    results: list[Any]
    exams: dict[int, Any]
    lecture_pass_scores: dict[int, float]
    attempt_counts: dict[int, int]
    not_submitted_exam_ids: set[int]


class SessionProgressCalculator:
    """
    차시(Session) 단위 진행 계산기

    ✅ 핵심 원칙(1 Session : N Exams):
    - SessionProgress는 특정 Exam 하나의 점수에 의존하지 않는다.
    - 반드시 Result 테이블(대표 스냅샷)들을 모아서 집계한다.
    - 집계 전략(MAX/AVG/LATEST)과 pass 기준 출처(POLICY/EXAM)는 ProgressPolicy가 단일 진실.
    """

    @staticmethod
    def _get_or_create_policy(session) -> ProgressPolicy:
        policy, _ = ProgressPolicy.objects.get_or_create(
            lecture=session.lecture,
            defaults={
                "video_required_rate": 90,
                "exam_start_session_order": 1,
                "exam_end_session_order": 9999,
                "exam_pass_score": 60.0,
                "exam_aggregate_strategy": ProgressPolicy.ExamAggregateStrategy.MAX,
                "exam_pass_source": ProgressPolicy.ExamPassSource.EXAM,
                "homework_start_session_order": 1,
                "homework_end_session_order": 9999,
                "homework_pass_type": ProgressPolicy.HomeworkPassType.TEACHER_APPROVAL,
            },
        )
        return policy

    @staticmethod
    def _pick_latest(results: List[Any]) -> Optional[Any]:
        if not results:
            return None
        return sorted(
            results,
            key=lambda r: (
                r.submitted_at is not None,
                r.submitted_at or timezone.datetime.min.replace(tzinfo=timezone.get_current_timezone()),
                r.id,
            ),
        )[-1]

    @staticmethod
    def _safe_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _regular_order_for_policy(session) -> int | None:
        if getattr(session, "session_type", None) == "SUPPLEMENT":
            return None
        return session.regular_order or session.order

    @classmethod
    def _aggregate_exam_results(
        cls,
        *,
        enrollment_id: int,
        session,
        policy: ProgressPolicy,
        read_sources: ExamAggregationReadSources | None = None,
    ) -> Tuple[bool, Optional[float], bool, Dict[str, Any]]:
        Result, ExamAttempt = get_result_attempt_models()
        Exam = get_exam_model()
        exam_ids = read_sources.exam_ids if read_sources is not None else get_target_exam_ids_for_session_enrollment(
            session=session,
            enrollment_id=enrollment_id,
        )

        if not exam_ids:
            meta = {
                "strategy": str(policy.exam_aggregate_strategy),
                "pass_source": str(policy.exam_pass_source),
                "exams": [],
                "note": "no_exams_in_session",
            }
            return False, None, True, meta

        results = read_sources.results if read_sources is not None else list(
            Result.objects.filter(
                target_type="exam",
                enrollment_id=int(enrollment_id),
                target_id__in=[int(x) for x in exam_ids],
            )
        )

        if not results:
            meta = {
                "strategy": str(policy.exam_aggregate_strategy),
                "pass_source": str(policy.exam_pass_source),
                "exams": [
                    {
                        "exam_id": int(eid),
                        "score": None,
                        "max_score": None,
                        "pass_score": None,
                        "passed": False,
                        "submitted_at": None,
                        "attempt_count": 0,
                    }
                    for eid in exam_ids
                ],
                "note": "no_results",
            }
            return False, None, False, meta

        exam_attempted = True

        exams = read_sources.exams if read_sources is not None else {
            e.id: e for e in Exam.objects.filter(id__in=[int(x) for x in exam_ids])
        }
        lecture_pass_scores = read_sources.lecture_pass_scores if read_sources is not None else exam_pass_score_overrides(
            exam_ids=[int(exam_id) for exam_id in exam_ids],
            lecture_id=int(session.lecture_id),
        )

        # ✅ Attempt count is authoritative from ExamAttempt
        attempt_counts = read_sources.attempt_counts if read_sources is not None else {
            int(row["exam_id"]): int(row["cnt"] or 0)
            for row in (
                ExamAttempt.objects.filter(
                    exam_id__in=[int(x) for x in exam_ids],
                    enrollment_id=int(enrollment_id),
                )
                .values("exam_id")
                .annotate(cnt=Count("id"))
            )
        }

        # 미응시(NOT_SUBMITTED) ExamAttempt 식별
        not_submitted_exam_ids = set(read_sources.not_submitted_exam_ids) if read_sources is not None else set()
        for ea in ([] if read_sources is not None else ExamAttempt.objects.filter(
            exam_id__in=[int(x) for x in exam_ids],
            enrollment_id=int(enrollment_id),
            is_representative=True,
        )):
            meta = ea.meta if isinstance(ea.meta, dict) else {}
            if meta.get("status") == "NOT_SUBMITTED":
                not_submitted_exam_ids.add(int(ea.exam_id))

        # ⚠️ exam_ids 전체 기준 per_exam_rows. Result 없는 시험도 명시적으로 row를 만들어
        # `passed=False, score=None, no_result=True`로 두어야 세션 완료 전수 검사에서 누락되지 않는다.
        # (exam_ids=[1,2,3] 중 Result가 2개만 있는 상황에서, 남은 1개 시험이 미응시인데도
        #  나머지 2개 합격만으로 세션 완료로 판정되던 잠재 버그 방지.)
        results_by_exam = {int(r.target_id): r for r in results}

        per_exam_rows: List[Dict[str, Any]] = []
        for eid in [int(x) for x in exam_ids]:
            ex = exams.get(eid)

            exam_pass_score = (
                lecture_pass_scores.get(
                    eid,
                    cls._safe_float(
                        getattr(ex, "pass_score", None),
                        default=0.0,
                    ),
                )
                if ex
                else 0.0
            )
            policy_pass_score = cls._safe_float(getattr(policy, "exam_pass_score", 0.0), default=0.0)
            pass_score = (
                policy_pass_score
                if policy.exam_pass_source == ProgressPolicy.ExamPassSource.POLICY
                else exam_pass_score
            )

            r = results_by_exam.get(eid)
            if r is None:
                per_exam_rows.append({
                    "exam_id": eid,
                    "score": None,
                    "max_score": cls._safe_float(getattr(ex, "max_score", None), 0.0) if ex else 0.0,
                    "pass_score": float(pass_score),
                    "passed": False,
                    "submitted_at": None,
                    "attempt_count": int(attempt_counts.get(eid, 0)),
                    "no_result": True,
                    "meta_status": None,
                })
                continue

            score = cls._safe_float(r.total_score, default=0.0)
            is_not_submitted = eid in not_submitted_exam_ids
            if is_not_submitted:
                passed_value = False
            elif pass_score > 0:
                passed_value = bool(score >= float(pass_score))
            else:
                passed_value = True  # pass_score=0 + 응시완료 → 기준 없음, 통과

            per_exam_rows.append({
                "exam_id": eid,
                "score": score,
                "max_score": cls._safe_float(r.max_score, default=0.0),
                "pass_score": float(pass_score),
                "passed": passed_value,
                "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                "attempt_count": int(attempt_counts.get(eid, 0)),
                "meta_status": "NOT_SUBMITTED" if is_not_submitted else None,
            })

        strategy = policy.exam_aggregate_strategy

        aggregate_score: Optional[float] = None
        selected_pass_score: float = cls._safe_float(policy.exam_pass_score, 0.0)

        # 집계는 score 있는 row만 대상 (no_result row는 성적 표시에서 제외)
        scored_rows = [x for x in per_exam_rows if x.get("score") is not None]

        if not scored_rows:
            aggregate_score = None
            selected_pass_score = cls._safe_float(policy.exam_pass_score, 0.0)
        elif strategy == ProgressPolicy.ExamAggregateStrategy.MAX:
            best = max(scored_rows, key=lambda x: cls._safe_float(x.get("score"), 0.0))
            aggregate_score = cls._safe_float(best.get("score"), 0.0)
            selected_pass_score = cls._safe_float(best.get("pass_score"), 0.0)

        elif strategy == ProgressPolicy.ExamAggregateStrategy.AVG:
            scores = [cls._safe_float(x.get("score"), 0.0) for x in scored_rows]
            aggregate_score = (sum(scores) / len(scores)) if scores else 0.0

            if policy.exam_pass_source == ProgressPolicy.ExamPassSource.EXAM:
                ps = [cls._safe_float(x.get("pass_score"), 0.0) for x in scored_rows]
                selected_pass_score = (sum(ps) / len(ps)) if ps else 0.0
            else:
                selected_pass_score = cls._safe_float(policy.exam_pass_score, 0.0)

        elif strategy == ProgressPolicy.ExamAggregateStrategy.LATEST:
            latest = cls._pick_latest(results)
            if latest is None:
                aggregate_score = 0.0
                selected_pass_score = cls._safe_float(policy.exam_pass_score, 0.0)
            else:
                row = next(
                    (x for x in per_exam_rows if int(x["exam_id"]) == int(latest.target_id)),
                    None,
                )
                aggregate_score = cls._safe_float(latest.total_score, 0.0)
                selected_pass_score = cls._safe_float(
                    (row.get("pass_score") if row else policy.exam_pass_score),
                    0.0,
                )

        else:
            best = max(scored_rows, key=lambda x: cls._safe_float(x.get("score"), 0.0))
            aggregate_score = cls._safe_float(best.get("score"), 0.0)
            selected_pass_score = cls._safe_float(best.get("pass_score"), 0.0)

        # ⚠️ 세션 완료 판정은 "exam_ids 전체 시험 모두 통과" 기준.
        # per_exam_rows가 exam_ids 전체를 포함하므로 Result 누락 시험도 자동으로 passed=False.
        # all_not_submitted는 "전부 미응시(NOT_SUBMITTED ExamAttempt) 또는 Result 없음"으로 확장.
        missing_any_result = any(x.get("no_result") for x in per_exam_rows)
        all_not_submitted = all(not x["passed"] for x in per_exam_rows) and all(
            x.get("no_result") or int(x["exam_id"]) in not_submitted_exam_ids
            for x in per_exam_rows
        )
        if all_not_submitted:
            exam_passed = False
        else:
            # 모든 개별 시험이 passed=True여야 세션 완료 (no_result면 자동 False)
            exam_passed = bool(per_exam_rows) and all(bool(x["passed"]) for x in per_exam_rows)

        # exam_attempted: 단 1건이라도 Result가 있으면 True (기존 의미 유지)
        # no_result가 섞인 경우에도 응시 시도 자체는 있었다는 의미로 True.
        # 단, 모든 exam_id에 Result가 없으면 아래쪽 `if not results` 분기에서 먼저 처리됨.
        _ = missing_any_result  # 명시적 표기, 현재는 meta에만 남김

        meta = {
            "strategy": str(strategy),
            "pass_source": str(policy.exam_pass_source),
            "aggregate_pass_score": float(selected_pass_score),
            "all_passed": exam_passed,  # 명시: 모든 개별 시험 통과 여부
            "missing_results": missing_any_result,  # 일부 exam에 Result 누락
            "exams": per_exam_rows,
        }

        return True, aggregate_score, exam_passed, meta

    @staticmethod
    def calculate(
        *,
        enrollment_id: int,
        session,
        attendance_type: str,
        video_progress_rate: int = 0,
        homework_submitted: bool = False,
    ) -> SessionProgress:
        policy = SessionProgressCalculator._get_or_create_policy(session)

        obj, _ = SessionProgress.objects.get_or_create(
            enrollment_id=enrollment_id,
            session=session,
        )

        obj.attendance_type = attendance_type
        obj.video_progress_rate = int(video_progress_rate or 0)

        if attendance_type == SessionProgress.AttendanceType.OFFLINE:
            obj.video_completed = True
        else:
            obj.video_completed = obj.video_progress_rate >= int(policy.video_required_rate)

        SessionProgressCalculator.set_exam_fields(obj=obj, session=session, policy=policy)

        regular_order = SessionProgressCalculator._regular_order_for_policy(session)
        in_hw_range = bool(
            regular_order is not None
            and policy.homework_start_session_order <= regular_order <= policy.homework_end_session_order
        )
        if in_hw_range:
            obj.homework_submitted = bool(homework_submitted)

            if policy.homework_pass_type == ProgressPolicy.HomeworkPassType.SUBMIT:
                obj.homework_passed = bool(homework_submitted)

            elif policy.homework_pass_type == ProgressPolicy.HomeworkPassType.SCORE:
                obj.homework_passed = homework_score_exists(
                    enrollment_id=enrollment_id,
                    session_id=session.id,
                    attempt_index=1,
                    passed=True,
                )

            elif policy.homework_pass_type == ProgressPolicy.HomeworkPassType.TEACHER_APPROVAL:
                obj.homework_passed = homework_score_exists(
                    enrollment_id=enrollment_id,
                    session_id=session.id,
                    attempt_index=1,
                    teacher_approved=True,
                )
        else:
            obj.homework_passed = True

        SessionProgressCalculator.set_completion_fields(obj)
        obj.calculated_at = timezone.now()
        obj.save()

        return obj

    @staticmethod
    def set_exam_fields(*, obj: SessionProgress, session, policy: ProgressPolicy) -> None:
        """Apply canonical exam policy without touching attendance/homework inputs."""
        regular_order = SessionProgressCalculator._regular_order_for_policy(session)
        in_exam_range = bool(
            regular_order is not None
            and policy.exam_start_session_order <= regular_order <= policy.exam_end_session_order
        )
        if in_exam_range:
            attempted, agg_score, passed, exam_meta = SessionProgressCalculator._aggregate_exam_results(
                enrollment_id=obj.enrollment_id,
                session=session,
                policy=policy,
            )
            obj.exam_attempted = bool(attempted)
            obj.exam_aggregate_score = agg_score
            obj.exam_passed = bool(passed)
            obj.exam_meta = exam_meta
        else:
            obj.exam_attempted = False
            obj.exam_aggregate_score = None
            obj.exam_passed = True
            obj.exam_meta = {
                "strategy": str(policy.exam_aggregate_strategy),
                "pass_source": str(policy.exam_pass_source),
                "exams": [],
                "note": "out_of_exam_range",
            }

    @staticmethod
    def set_completion_fields(obj: SessionProgress) -> None:
        obj.completed = bool(obj.video_completed and obj.exam_passed and obj.homework_passed)

        # ⚠️ completed_at은 "최초 완료 시점"으로 불변화.
        # 점수 수정/대표 attempt 교체로 completed가 False로 회귀해도
        # 지우지 않음(감사 이력 보존). 현재 완료 여부는 `completed` 플래그로만 판단.
        if obj.completed and not obj.completed_at:
            obj.completed_at = timezone.now()
