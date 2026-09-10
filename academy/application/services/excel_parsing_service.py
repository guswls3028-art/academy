# PATH: src/application/services/excel_parsing_service.py
# 비즈니스 로직의 핵심: 엑셀 파싱 + 강의 수강 등록 원테이크 (헥사고날 Application Service)
# - 엑셀 파싱·등록 흐름은 이 서비스만 통과 (워커/API 공통)
# - "무적의 엑셀 파싱": 헤더 별칭 매칭, 이름·학부모전화 기준 행 파싱

from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from academy.application.ports.storage import IObjectStorage

logger = logging.getLogger(__name__)


class ExcelValidationError(ValueError):
    """
    학부모 전화 필수 검증 등 엑셀 파싱 검증 실패 시.
    errors: [{"row": int, "reason": str}, ...] — 전체 오류 목록(1행 단위).
    """

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


# 헤더 별칭 (학원별 양식 대응 — 양식 안 맞춰도 인식되도록 넓게)
# 완전 일치 + 부분 포함(contains) 둘 다 시도
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "이름", "성명", "학생명", "학생 이름", "이름(학생)", "성함", "학생성명", "수강생",
        "name", "student name",
    ),
    "parent_phone": (
        "학부모전화번호", "부모핸드폰", "부모 전화", "학부모 전화", "보호자 전화", "보호자전화",
        "학부모연락처", "부모 연락처", "보호자 연락처", "연락처(학부모)", "전화(학부모)",
        "휴대폰", "핸드폰", "연락처", "전화번호", "전화", "폰", "폰번호",
        "부모핸드", "학부모", "보호자",
        "parent phone", "parent mobile", "guardian phone", "guardian mobile",
        "emergency contact",
    ),
    "student_phone": (
        "학생전화번호", "학생핸드폰", "학생 전화", "학생연락처", "학생 연락처",
        "연락처(학생)", "전화(학생)", "학생폰", "학생 폰",
        "학생핸드", "학생전화",
        "student phone", "student mobile",
    ),
    "school": ("학교", "학교(학년)", "학교명", "출신학교", "school", "school / grade"),
    "grade": ("학년", "학년도", "grade"),
    "gender": ("성별", "남자", "여자", "남성", "여성", "남", "여", "녀"),
    "school_class": ("반", "학급"),
    "major": ("계열", "이과", "문과"),
    "memo": ("메모", "비고", "특이사항", "비고2"),
    "remark": ("구분", "체크", "비고", "비고2", "출석", "현장"),
}


def _normalize_header(label: str) -> str:
    """공백 제거, 전각→반각, 소문자."""
    s = (label or "").strip()
    s = re.sub(r"\s", "", s)
    s = "".join(chr(ord(c) - 0xFEE0) if "\uFF01" <= c <= "\uFF5E" else c for c in s)
    return s.lower()


def _match_header(cell: str, key: str) -> bool:
    """완전 일치 또는 짧은 별칭이 헤더 시작/포함 시 매칭 (전각 숫자 정규화)."""
    norm = _normalize_header(cell)
    if not norm:
        return False
    for alias in HEADER_ALIASES.get(key, ()):
        a = _normalize_header(alias)
        if not a:
            continue
        if norm == a:
            return True
        if len(a) >= 3 and a in norm:
            return True
        if len(a) >= 2 and norm.startswith(a) and len(norm) <= len(a) + 4:
            return True
    return False


def _find_header_row(rows: list[list[Any]]) -> int:
    """이름 + 전화 컬럼이 모두 있는 첫 행을 헤더로 반환."""
    for i, row in enumerate(rows):
        if not row:
            continue
        has_name = any(_match_header(str(c), "name") for c in row)
        has_phone = any(
            _match_header(str(c), "parent_phone") or _match_header(str(c), "student_phone")
            for c in row
        )
        if has_name and has_phone:
            return i
    return -1


def _find_header_row_fallback(rows: list[list[Any]]) -> int:
    """표준 헤더를 못 찾았을 때: 상위 10행 스캔, 이름/전화 컬럼 하나라도 있으면 헤더로 사용."""
    for i, row in enumerate(rows[:10]):
        if not row:
            continue
        col = _build_header_map(row)
        if col.get("name") is not None or col.get("parent_phone") is not None or col.get("student_phone") is not None:
            return i
    return -1


_PARENT_KEYWORDS = ("부모", "학부모", "보호자", "guardian", "parent", "emergency")
_STUDENT_KEYWORDS = ("학생", "student")


def _duplicate_ratio(values: list[str]) -> float:
    """전화 컬럼 내 중복 비율. 1 - (unique수/전체수). 부모 전화는 형제로 인해 중복될 가능성 높음."""
    non_empty = [v for v in values if v and _validate_parent_phone(v)]
    if len(non_empty) < 2:
        return 0.0
    unique_count = len(set(_to_raw_phone(v) for v in non_empty))
    return 1.0 - (unique_count / len(non_empty))


def _rule_guess_parent_score(header: str, values: list[str]) -> float:
    """
    Rule 기반 parent_phone 컬럼 점수. 0~1.
    - parent 키워드 포함 +0.6, student 키워드 -0.5
    - phone_ratio(010 포함 11자리 비율) * 0.7
    - 헤더에 부모/학생 키워드가 둘 다 없을 때: 중복도(duplicate_ratio) 가산 — 부모 전화는 형제로 인해 중복 가능성 높음
    """
    h = (header or "").replace(" ", "").lower()
    has_parent_kw = any(k in h for k in _PARENT_KEYWORDS)
    has_student_kw = any(k in h for k in _STUDENT_KEYWORDS)
    score = 0.0
    if has_parent_kw:
        score += 0.6
    if has_student_kw:
        score -= 0.5
    non_empty = [v for v in values if v]
    if non_empty:
        phone_ok = sum(1 for v in non_empty if _validate_parent_phone(v))
        score += (phone_ok / len(non_empty)) * 0.7
    if not has_parent_kw and not has_student_kw and non_empty:
        dup_ratio = _duplicate_ratio(values)
        score += dup_ratio * 0.3
    return max(0.0, min(1.0, score))


def _infer_missing_columns(
    col: dict[str, int], header_row: list[Any], rows: list[list[Any]], header_idx: int
) -> dict[str, int]:
    """
    필수 컬럼(name, parent_phone)이 없을 때, 샘플 데이터로 컬럼 추측.
    - 일반 명단은 010 패턴 2행 이상, 데이터가 한 행뿐이면 1행을 컬럼 후보로 본다.
    - 전화 후보가 하나이고 학생 번호 표식이 없으면 rule score >= 0.6에서 학부모 번호로 확정한다.
    - 전화 후보가 여러 개면 전체 후보를 AI에 동시 전달한다. null/conf<0.8이면 업로드를 실패한다.
    """
    out = dict(col)
    sample = rows[header_idx + 1 : header_idx + 21]  # 최대 20행 샘플
    if not sample:
        return out

    phone_col_candidates: list[tuple[int, int, float]] = []  # (col_idx, phone_hits, parent_score)
    name_col_candidates: list[tuple[int, int]] = []
    korean_name = re.compile(r"^[가-힣]{2,5}[A-Za-z0-9]*\*?$")
    populated_sample_rows = sum(
        1 for row in sample if any(str(value or "").strip() for value in row)
    )
    minimum_hits = 1 if populated_sample_rows <= 1 else 2

    max_col = max(len(r) for r in sample) if sample else 0
    for ci in range(max_col):
        phone_hits = 0
        name_hits = 0
        sample_vals: list[str] = []
        for row in sample:
            if ci >= len(row):
                continue
            v = str(row[ci] or "").strip()
            sample_vals.append(v)
            if _validate_parent_phone(v):
                phone_hits += 1
            if korean_name.match(v):
                name_hits += 1
        if phone_hits >= minimum_hits:
            header_label = str(header_row[ci] if ci < len(header_row) else "").strip()
            parent_score = _rule_guess_parent_score(header_label, sample_vals)
            phone_col_candidates.append((ci, phone_hits, parent_score))
        if name_hits >= minimum_hits:
            name_col_candidates.append((ci, name_hits))

    if out.get("parent_phone") is None and phone_col_candidates:
        phone_col_candidates.sort(key=lambda x: (-x[2], -x[1]))  # score desc, then hits
        best_idx, _, best_score = phone_col_candidates[0]

        best_header = str(
            header_row[best_idx] if best_idx < len(header_row) else ""
        ).strip().lower()
        best_is_student_labeled = any(
            keyword in best_header for keyword in _STUDENT_KEYWORDS
        )

        if (
            len(phone_col_candidates) == 1
            and best_score >= 0.6
            and not best_is_student_labeled
        ):
            out["parent_phone"] = best_idx
        elif len(phone_col_candidates) > 1 and best_score >= 0.6:
            ai_col, ai_conf = _ai_infer_parent_phone(
                header_row, sample, phone_col_candidates
            )
            if ai_col is not None and ai_conf >= 0.8:
                out["parent_phone"] = ai_col
            else:
                raise ExcelValidationError(
                    "학부모 전화번호 컬럼을 자동으로 식별할 수 없습니다. "
                    "헤더에 '학부모전화', '부모핸드폰', '보호자 전화' 등을 명확히 표기해 주세요."
                )
        else:
            raise ExcelValidationError(
                "학부모 전화번호 컬럼을 찾을 수 없습니다. "
                "헤더에 '학부모전화', '부모핸드폰', '보호자 전화' 등이 있어야 합니다."
            )
    if out.get("name") is None and name_col_candidates:
        name_col_candidates.sort(key=lambda x: -x[1])
        out["name"] = name_col_candidates[0][0]

    return out


def _phone_header_role(label: str) -> str | None:
    normalized = _normalize_header(label)
    if not (
        _match_header(label, "parent_phone")
        or _match_header(label, "student_phone")
    ):
        return None
    has_parent = any(
        _normalize_header(keyword) in normalized for keyword in _PARENT_KEYWORDS
    )
    has_student = any(
        _normalize_header(keyword) in normalized for keyword in _STUDENT_KEYWORDS
    )
    if has_parent and not has_student:
        return "parent_phone"
    if has_student and not has_parent:
        return "student_phone"
    return None


def _build_header_map(header_row: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    used_indices: set[int] = set()

    # 전화번호는 의미 표식(학생/보호자)을 먼저 본다. 일반 별칭 "연락처"가
    # "학생 연락처"를 학부모 번호로 선점하면 계정 그래프가 잘못 연결된다.
    for i, cell in enumerate(header_row):
        label = str(cell or "").strip()
        role = _phone_header_role(label)
        if role and role not in out:
            out[role] = i
            used_indices.add(i)

    # 의미 표식이 없는 기존 양식의 휴대폰/연락처는 학부모 번호로 유지한다.
    for key in ("parent_phone", "student_phone"):
        if key in out:
            continue
        for i, cell in enumerate(header_row):
            if i in used_indices:
                continue
            label = str(cell or "").strip()
            if label and _match_header(label, key):
                out[key] = i
                used_indices.add(i)
                break

    for i, cell in enumerate(header_row):
        label = str(cell or "").strip()
        if not label:
            continue
        for key in HEADER_ALIASES:
            if key in {"parent_phone", "student_phone"}:
                continue
            if key in out:
                continue
            if _match_header(label, key):
                out[key] = i
                break
    return out


def _worksheet_candidate_score(
    rows: list[list[Any]],
    *,
    is_active: bool,
) -> tuple[int, int, int, int, int, int] | None:
    header_idx = _find_header_row(rows)
    strict_header = header_idx >= 0
    if header_idx < 0:
        header_idx = _find_header_row_fallback(rows)
    if header_idx < 0:
        return None

    col = _build_header_map(rows[header_idx])
    sample_rows = rows[header_idx + 1 : header_idx + 51]
    phone_columns = {
        ci
        for row in sample_rows
        for ci, value in enumerate(row)
        if _validate_parent_phone(str(value or ""))
    }
    meaningful_rows = sum(
        1
        for row in sample_rows
        if any(str(value or "").strip() for value in row)
        and any(
            _validate_parent_phone(str(value or ""))
            for value in row
        )
    )
    return (
        int(strict_header),
        int(col.get("parent_phone") is not None),
        int(col.get("name") is not None),
        meaningful_rows,
        len(phone_columns),
        int(is_active),
    )


def _select_worksheet_rows(workbook) -> tuple[list[list[Any]], str]:
    candidates: list[tuple[tuple[int, ...], int, list[list[Any]], str]] = []
    active = workbook.active
    for position, worksheet in enumerate(workbook.worksheets):
        rows = [
            list(row) if row else []
            for row in worksheet.iter_rows(values_only=True)
        ]
        score = _worksheet_candidate_score(
            rows,
            is_active=worksheet is active,
        )
        if score is not None:
            candidates.append((score, position, rows, str(worksheet.title)))

    if candidates:
        score, _position, rows, title = max(
            candidates,
            key=lambda item: (item[0], -item[1]),
        )
        same_quality = [
            candidate
            for candidate in candidates
            if candidate[0][:-1] == score[:-1]
        ]
        if len(same_quality) > 1:
            active_matches = [
                candidate for candidate in same_quality if candidate[0][-1] == 1
            ]
            if len(active_matches) == 1:
                score, _position, rows, title = active_matches[0]
            else:
                titles = ", ".join(candidate[3] for candidate in same_quality)
                raise ExcelValidationError(
                    "명단으로 보이는 시트가 여러 개입니다. "
                    f"등록할 시트를 Excel에서 활성화한 뒤 다시 업로드해 주세요: {titles}"
                )
        logger.info(
            "excel_parsing: selected worksheet title=%s score=%s candidates=%d",
            title,
            score,
            len(candidates),
        )
        return rows, title

    if active is None:
        raise ValueError("No active sheet")
    return (
        [list(row) if row else [] for row in active.iter_rows(values_only=True)],
        str(active.title),
    )


def _cell_str(row: list[Any], col_index: int | None) -> str:
    if col_index is None or col_index >= len(row):
        return ""
    return str(row[col_index] or "").strip()


def _is_known_template_example_row(
    *,
    name: str,
    parent_phone_raw: str,
    student_phone_raw: str,
) -> bool:
    return (
        name == "홍길동"
        and parent_phone_raw == "01087654321"
        and student_phone_raw == "01012345678"
    ) or (
        name == "김영희"
        and parent_phone_raw == "01011112222"
        and not student_phone_raw
    )


def _to_raw_phone(v: str) -> str:
    return re.sub(r"\D", "", v)


def _mask_phone_for_ai(v: str) -> str:
    """
    전화번호 마스킹 (AI 전송 시 PII 보호). 포맷 다양성 유지.
    010-1234-5678 → 010-****-5678, 01012345678 → 010****5678, 010.1234.5678 → 010.****.5678
    """
    raw = _to_raw_phone(v)
    if len(raw) < 7 or not raw.startswith("010"):
        return "***"
    m = re.search(r"010(\D*)(\d{3,4})(\D*)(\d{4})(?:\D|$)", v)
    if m:
        return "010" + m.group(1) + "****" + m.group(3) + m.group(4)
    return raw[:3] + "****" + raw[-4:]


def _validate_parent_phone(raw: str) -> bool:
    """010 11자리 형식인지 검사. parent_phone 필수 검증용.
    enrollment 코드가 정확히 11자리를 요구하므로 10자리(구형 번호)는 거부."""
    p = _to_raw_phone(raw)
    if not p or not p.startswith("010"):
        return False
    return len(p) == 11


def _parse_school_grade(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value:
        return "", ""
    m = re.match(r"^(.+?)\(([０-９0-9]+)\)\s*$", value)
    if not m:
        return value, ""
    school = m.group(1).strip()
    grade = m.group(2)
    grade = "".join(
        chr(ord(c) - 0xFEE0) if "\uFF10" <= c <= "\uFF19" else c for c in grade
    )
    grade = re.sub(r"\D", "", grade)
    return school, grade


def _infer_school_type(school: str) -> str:
    if not school:
        return "HIGH"
    if re.search(r"초등학교|초등|초\b", school):
        return "ELEMENTARY"
    if re.search(r"중학교|중등|중\b", school):
        return "MIDDLE"
    return "HIGH"


def _row_looks_like_student(
    name: str,
    parent_phone_raw: str,
    student_phone_raw: str,
) -> bool:
    """
    학생 행 여부 스코어링. 소제목/날짜/빈 행 등 비학생 행 제외.
    - 이름이 날짜패턴(01월, 2/7~)이면 제외
    - 이름이 순수 숫자/너무 길면 제외
    - 최소 name 또는 유효 전화번호 1개 이상 필요
    """
    name = (name or "").strip()
    if not name and not parent_phone_raw and not student_phone_raw:
        return False
    if re.match(r"^\d{1,2}/\d{1,2}", name) or re.match(r"^\d{1,2}월\s*$", name):
        return False
    if name and re.match(r"^\d+$", name):
        return False
    has_phone = (
        (len(parent_phone_raw) == 11 and parent_phone_raw.startswith("010"))
        or (len(student_phone_raw) == 11 and student_phone_raw.startswith("010"))
        or (len(parent_phone_raw) == 8 and parent_phone_raw.isdigit())
        or (len(student_phone_raw) == 8 and student_phone_raw.isdigit())
    )
    if len(name) > 50:
        return False
    if len(name) > 20 and not has_phone:
        return False
    has_name = bool(re.match(r"^[가-힣]{2,5}[A-Za-z0-9]*\*?$", name))
    if not has_name and not has_phone:
        return False
    return True


def _extract_lecture_title(rows: list[list[Any]], header_idx: int) -> str:
    """헤더 행 위(0 ~ header_idx-1)에서 강의 제목처럼 보이는 셀 추출."""
    candidates: list[str] = []
    for r in range(min(header_idx, 5)):  # 최대 5행까지
        for cell in rows[r] if r < len(rows) else []:
            s = str(cell or "").strip()
            if len(s) >= 4 and len(s) <= 120 and not s.isdigit():
                candidates.append(s)
    return candidates[0] if candidates else ""


def _ai_infer_parent_phone(
    header_row: list[Any],
    sample_rows: list[list[Any]],
    phone_col_candidates: list[tuple[int, int, float]],
) -> tuple[int | None, float]:
    """
    AI 워커 2차 판정. conf 0.6~0.9 시 호출.
    Returns: (parent_phone_col_index | None, confidence)
    """
    try:
        from academy.adapters.ai.excel_schema_infer import infer_parent_phone_column
    except ImportError:
        return None, 0.0

    candidates = []
    for ci, _hits, score in phone_col_candidates:
        header_label = str(header_row[ci] if ci < len(header_row) else "").strip()
        samples = []
        for row in sample_rows:
            if ci < len(row):
                v = str(row[ci] or "").strip()
                if v:
                    samples.append(_mask_phone_for_ai(v))
        candidates.append({
            "col_index": ci,
            "header": header_label,
            "samples": samples[:5],
            "rule_score": score,
        })

    if not candidates:
        return None, 0.0
    try:
        col, conf = infer_parent_phone_column(candidates)
        valid_cols = {c["col_index"] for c in candidates}
        if col is not None and col in valid_cols:
            return col, conf
        return None, conf
    except Exception as e:
        logger.warning("AI parent_phone infer failed: %s", e)
        return None, 0.0


def _header_similarity(h1: str, h2: str) -> float:
    """
    두 헤더 간 유사도 0~1. Levenshtein 기반 SequenceMatcher 사용.
    학원별 매핑 재사용 시: 기존 저장 헤더와 새 업로드 헤더가 유사하면 캐시 사용.
    """
    if not h1 or not h2:
        return 0.0
    n1, n2 = _normalize_header(h1), _normalize_header(h2)
    return SequenceMatcher(None, n1, n2).ratio()


def _get_academy_parent_mapping(_academy_id: int, _headers: list[str]) -> int | None:
    """
    academy_id 기반 parent_phone 매핑 재사용 확장 포인트.
    구현 시: DB에서 캐시된 매핑 조회 → _header_similarity(cached_header, current_header) >= 0.8
    이면 기존 col_index 반환, 아니면 None 반환하여 재추론 유도.
    """
    return None


def parse_student_excel_file(
    local_path: str,
    *,
    validation_errors_out: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    로컬 엑셀 파일을 파싱하여 강의 수강 등록용 행 리스트와 강의 제목 반환.
    양식 안 맞춰도 인식: 헤더 별칭 넓게 지원, 이름+전화 없으면 첫 행 기준으로도 시도.
    Returns: (rows, lecture_title)

    ``validation_errors_out``가 주어지면 행 단위 오류를 그 목록에 모으고
    유효한 행은 계속 반환한다. 주어지지 않으면 기존 강의 등록 호환을 위해
    하나의 행 오류도 전체 파싱 오류로 처리한다.
    """
    import openpyxl

    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {local_path}")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows, _worksheet_title = _select_worksheet_rows(wb)
    finally:
        wb.close()

    if not rows:
        return [], ""

    header_idx = _find_header_row(rows)
    used_fallback = False
    if header_idx < 0:
        header_idx = _find_header_row_fallback(rows)
        used_fallback = header_idx >= 0
    if header_idx < 0:
        raise ValueError(
            "헤더 행을 찾을 수 없습니다. 첫 행에 '이름'(또는 성명/학생명), '연락처'(또는 학부모전화/전화번호) 등 컬럼명이 있어야 합니다."
        )
    if used_fallback:
        logger.info(
            "excel_parsing: 표준 헤더(이름+전화 동시) 없음 → 첫 행 기준으로 컬럼 매칭 사용 (row=%s)",
            header_idx,
        )

    header_row = rows[header_idx]
    col = _build_header_map(header_row)
    if col.get("name") is None or col.get("parent_phone") is None:
        col = _infer_missing_columns(col, header_row, rows, header_idx)
    name_col = col.get("name")
    parent_col = col.get("parent_phone")
    student_col = col.get("student_phone")

    if parent_col is None:
        raise ExcelValidationError(
            "학부모 전화번호 컬럼을 찾을 수 없습니다. "
            "헤더에 '학부모전화', '부모핸드폰', '보호자 전화' 등이 있어야 합니다."
        )
    school_col = col.get("school")
    grade_col = col.get("grade")
    remark_col = col.get("remark")
    mapped_indices = {index for index in col.values() if index is not None}
    extra_headers = [
        (index, str(value or "").strip())
        for index, value in enumerate(header_row)
        if index not in mapped_indices and str(value or "").strip()
    ]

    result: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []
    # 중요: parent_phone 검증은 반드시 _row_looks_like_student()로 필터링된 학생 행에만 적용.
    # 소제목/날짜 행 등이 parent_phone 검증 대상이 되면 잘못된 실패 발생.
    for r in range(header_idx + 1, len(rows)):
        row = rows[r]
        name = _cell_str(row, name_col)
        student_phone_raw = _to_raw_phone(_cell_str(row, student_col))
        parent_phone_raw = _to_raw_phone(_cell_str(row, parent_col))

        if (
            remark_col is not None
            and "예시" in _cell_str(row, remark_col)
        ) or _is_known_template_example_row(
            name=name,
            parent_phone_raw=parent_phone_raw,
            student_phone_raw=student_phone_raw,
        ):
            continue

        if not name and not student_phone_raw and not parent_phone_raw:
            continue

        if not _row_looks_like_student(name, parent_phone_raw, student_phone_raw):
            continue

        # 학생 행에 한해 parent_phone 검증 (순서 필수: 행 판별 → 검증)
        if not _validate_parent_phone(parent_phone_raw):
            display_val = _cell_str(row, parent_col) or parent_phone_raw or "(비어있음)"
            validation_errors.append({
                "row": r + 1,
                "name": name or "(이름 없음)",
                "value": display_val[:20] + ("..." if len(str(display_val)) > 20 else ""),
                "reason": "학부모 전화번호가 없거나 형식이 잘못되었습니다(010 포함 11자리).",
            })
            continue


        if len(student_phone_raw) == 8 and student_phone_raw.isdigit():
            student_phone = "010" + student_phone_raw
            uses_identifier = True
        elif student_phone_raw and len(student_phone_raw) == 11 and student_phone_raw.startswith("010"):
            student_phone = student_phone_raw
            uses_identifier = False
        else:
            student_phone = ""
            uses_identifier = True

        parent_final = parent_phone_raw
        if student_phone and student_phone == parent_final:
            student_phone = ""
            uses_identifier = True

        school_cell = _cell_str(row, school_col)
        grade_cell = _cell_str(row, grade_col)
        school_parsed, grade_parsed = _parse_school_grade(school_cell)
        school = school_parsed or school_cell
        grade = grade_parsed or grade_cell
        school_type = _infer_school_type(school)

        extra_columns: dict[str, str] = {}
        for index, header in extra_headers:
            value = _cell_str(row, index)
            if value and header not in extra_columns:
                extra_columns[header] = value

        result.append({
            "name": name,
            "parent_phone": parent_final,
            "phone": student_phone if student_phone else None,
            "studentPhone": student_phone if student_phone else None,
            "school": school,
            "grade": grade,
            "school_type": school_type,
            "schoolClass": _cell_str(row, col.get("school_class")),
            "major": _cell_str(row, col.get("major")),
            "memo": _cell_str(row, col.get("memo")),
            "gender": _cell_str(row, col.get("gender")).upper()[:1] or None,
            "uses_identifier": uses_identifier,
            "high_school_class": _cell_str(row, col.get("school_class")),
            "_extra_columns": extra_columns,
            "_excel_row": r + 1,
        })

    if validation_errors:
        if validation_errors_out is not None:
            validation_errors_out.extend(validation_errors)
        else:
            err_msg = (
                f"학부모 전화번호 검증 실패: {len(validation_errors)}건. "
                "010 포함 11자리 형식이어야 합니다."
            )
            raise ExcelValidationError(err_msg, errors=validation_errors)

    lecture_title = _extract_lecture_title(rows, header_idx)
    return result, lecture_title


class ExcelParsingService:
    """
    엑셀 파싱 + 등록 원테이크 (워커 전용).
    - lecture_id 있음: R2 다운로드 → 파싱 → 강의 수강 등록.
    - lecture_id 없음: R2 다운로드 → 파싱 → 학생만 일괄 생성.
    """

    def __init__(self, storage: IObjectStorage) -> None:
        self._storage = storage

    def run(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        on_progress: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        """
        payload:
          - file_key: str (R2 객체 키)
          - bucket: str (선택)
          - tenant_id: int (필수)
          - password_mode: str (학생 등록에서 fixed | random, 기본 fixed)
          - initial_password_secret: str (학생 등록 fixed 방식의 암호화 비밀번호)
          - lecture_id: int (선택) — 있으면 수강등록, 없으면 학생만 일괄 생성
          - session_id: int (선택, lecture_id 있을 때만)
          - student_match_mode: existing_only (수강등록 명시 표식)
        """
        import tempfile

        file_key = payload.get("file_key")
        if not file_key:
            raise ValueError("payload.file_key required")

        bucket = (
            payload.get("bucket")
            or os.environ.get("EXCEL_BUCKET_NAME")
            or "academy-excel"
        )
        tenant_id = payload.get("tenant_id")
        lecture_id = payload.get("lecture_id")
        session_id = payload.get("session_id")
        if not tenant_id:
            raise ValueError("payload.tenant_id required")

        password_policy = None
        if lecture_id is None:
            from apps.domains.ai.services.excel_job_secrets import (
                recover_excel_initial_password,
            )
            from apps.domains.students.services import build_student_import_password_policy

            password_mode = (payload.get("password_mode") or "fixed").strip()
            initial_password = recover_excel_initial_password(payload)
            password_policy = build_student_import_password_policy(
                password_mode=password_mode,
                initial_password=initial_password,
            )

        tmp_dir = Path(tempfile.gettempdir())
        local_path = tmp_dir / f"excel_job_{job_id}.xlsx"

        try:
            self._storage.download_to_path(bucket, file_key, str(local_path))
            parsing_errors: list[dict[str, Any]] = []
            rows, lecture_title = parse_student_excel_file(
                str(local_path),
                validation_errors_out=parsing_errors if lecture_id is None else None,
            )
            if not rows and not parsing_errors:
                raise ValueError("등록할 학생 데이터가 없습니다.")

            if lecture_id is not None:
                from apps.domains.enrollment.services import lecture_enroll_from_excel_rows
                from django.db import transaction
                from django.utils import timezone
                from academy.adapters.db.django.repositories_ai import DjangoAIJobRepository

                if on_progress:
                    on_progress("enrolling", 50)
                with transaction.atomic():
                    result = lecture_enroll_from_excel_rows(
                        tenant_id=int(tenant_id),
                        lecture_id=int(lecture_id),
                        students_data=rows,
                        session_id=int(session_id) if session_id is not None else None,
                        source_job_id=str(job_id),
                    )
                    if isinstance(result, dict) and lecture_title:
                        result["lecture_title"] = lecture_title
                    if not DjangoAIJobRepository().mark_done(
                        job_id,
                        timezone.now(),
                        result_payload=result,
                    ):
                        raise RuntimeError("excel_job_atomic_completion_failed")
                if on_progress:
                    on_progress("enrolling", 95)
                return result

            from apps.domains.students.services import import_students_from_rows
            from django.db import transaction
            from django.utils import timezone
            from academy.adapters.db.django.repositories_ai import DjangoAIJobRepository

            if password_policy is None:
                raise RuntimeError("student Excel password policy was not resolved")

            _last_pct: list[int] = [-1]  # mutable for closure

            def _row_progress(current: int, total: int) -> None:
                if on_progress and total > 0:
                    pct = min(95, 40 + int(55 * current / total))
                    if pct - _last_pct[0] >= 5 or current == total:
                        _last_pct[0] = pct
                        on_progress("creating", pct)

            with transaction.atomic():
                result = import_students_from_rows(
                    tenant_id=int(tenant_id),
                    students_data=rows,
                    initial_password=password_policy.fixed_password,
                    password_mode=password_policy.mode,
                    on_row_progress=_row_progress if on_progress else None,
                    source_job_id=str(job_id),
                )
                if parsing_errors:
                    result["failed"].extend(
                        {
                            "row": error["row"],
                            "name": error.get("name") or "(이름 없음)",
                            "error": error["reason"],
                            "reason_code": "invalid_row",
                            "conflict_student_id": None,
                        }
                        for error in parsing_errors
                    )
                    result["failed"].sort(key=lambda item: item["row"])
                    result["total"] += len(parsing_errors)
                if isinstance(result, dict) and lecture_title:
                    result["lecture_title"] = lecture_title
                if not DjangoAIJobRepository().mark_done(
                    job_id,
                    timezone.now(),
                    result_payload=result,
                ):
                    raise RuntimeError("excel_job_atomic_completion_failed")
            return result
        finally:
            # 더블 체크: 다운로드 성공/실패/부분 생성 여부와 관계없이 로컬 파일 삭제 시도
            cleaned = False
            if local_path.exists():
                try:
                    local_path.unlink()
                    cleaned = True
                except OSError as e:
                    logger.warning("Failed to remove tmp file %s: %s", local_path, e)
            if not cleaned and local_path.exists():
                logger.error("Local tmp file still exists after unlink attempt: %s", local_path)
