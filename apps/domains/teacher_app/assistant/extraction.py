from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError


_PHONE_RE = re.compile(r"(?<!\d)(01[016789])[-.\s]?(\d{3,4})[-.\s]?(\d{4})(?!\d)")
_SESSION_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:회차|차시)")
_KOREAN_NAME_RE = re.compile(r"[가-힣]{2,10}")
_NON_NAME_WORDS = {
    "그룹채팅",
    "메시지",
    "입력",
    "답장",
    "월요일",
    "화요일",
    "수요일",
    "목요일",
    "금요일",
    "토요일",
    "일요일",
}


@dataclass(frozen=True)
class ExtractedTeacherOpsRow:
    row_id: str
    name: str
    student_phone: str
    parent_phone: str
    lecture_hint: str
    school: str
    school_type: str
    grade: str
    session_order: int | None
    register_student: bool
    enroll_lecture: bool
    open_video: bool
    send_account_notice: bool
    correct_enrollment: bool
    warnings: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "row_id": self.row_id,
            "name": self.name,
            "student_phone": self.student_phone,
            "parent_phone": self.parent_phone,
            "lecture_hint": self.lecture_hint,
            "school": self.school,
            "school_type": self.school_type,
            "grade": self.grade,
            "session_order": self.session_order,
            "register_student": self.register_student,
            "enroll_lecture": self.enroll_lecture,
            "open_video": self.open_video,
            "send_account_notice": self.send_account_notice,
            "correct_enrollment": self.correct_enrollment,
            "warnings": list(self.warnings),
        }


def _phone_digits(match: re.Match[str]) -> str:
    return "".join(match.groups())


def _clean_line(value: str) -> str:
    return " ".join(str(value or "").replace("\u200b", " ").split()).strip()


def _name_and_hint(lines: list[str], first_phone_line: int) -> tuple[str, str]:
    candidate_lines = lines[:first_phone_line]
    for line in reversed(candidate_lines):
        if "/" not in line:
            continue
        left, right = line.split("/", maxsplit=1)
        names = _KOREAN_NAME_RE.findall(left)
        if names:
            return names[-1], _clean_line(right).strip(" ,.:;()[]")[:100]

    for line in reversed(candidate_lines[-4:]):
        for candidate in reversed(_KOREAN_NAME_RE.findall(line)):
            if candidate not in _NON_NAME_WORDS and not candidate.endswith("요일"):
                return candidate, ""
    return "", ""


def _school_fields(lecture_hint: str) -> tuple[str, str, str]:
    hint = _clean_line(lecture_hint)
    grade_match = re.search(r"([1-6])\s*(?:학년)?(?:반)?$", hint)
    grade = grade_match.group(1) if grade_match else ""
    school = re.sub(r"[1-6]\s*(?:학년)?(?:반)?$", "", hint).strip()

    if not school:
        return "", "HIGH", grade
    if school.endswith("초") or "초등" in school:
        return school, "ELEMENTARY", grade
    if school.endswith("중") or "중학교" in school:
        return school, "MIDDLE", grade
    if school.endswith("고") or "고등" in school or "부고" in school:
        return school, "HIGH", grade
    # 숙명1처럼 강의 힌트만 보이는 경우 학교 필드로 단정하지 않는다.
    return "", "HIGH", grade


def parse_teacher_ops_text(*, ocr_text: str, message: str) -> ExtractedTeacherOpsRow:
    lines = [_clean_line(line) for line in str(ocr_text or "").splitlines()]
    lines = [line for line in lines if line]
    phone_lines: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        for match in _PHONE_RE.finditer(line):
            phone_lines.append((index, _phone_digits(match), line))

    if not phone_lines:
        raise ValidationError({"image": "사진에서 010 전화번호를 찾지 못했습니다. 더 선명하게 잘라서 올려 주세요."})

    name, lecture_hint = _name_and_hint(lines, phone_lines[0][0])
    parent_phone = ""
    student_phone = ""
    unlabelled: list[str] = []
    for _, phone, line in phone_lines:
        if any(label in line for label in ("학부모", "부모", "보호자", "(모", "모)", " 모")):
            parent_phone = parent_phone or phone
        elif "학생" in line:
            student_phone = student_phone or phone
        else:
            unlabelled.append(phone)

    warnings: list[str] = []
    for phone in unlabelled:
        if not parent_phone:
            parent_phone = phone
        elif not student_phone and phone != parent_phone:
            student_phone = phone
    if unlabelled:
        warnings.append("전화번호 역할 표기가 흐려 순서를 추정했습니다. 학생·학부모 번호를 확인해 주세요.")

    combined = f"{message}\n{ocr_text}"
    session_match = _SESSION_RE.search(combined)
    session_order = int(session_match.group(1)) if session_match else None
    register_student = any(word in combined for word in ("학생등록", "학생 등록", "신규", "등록", "입반"))
    open_video = "영상" in combined and any(word in combined for word in ("권한", "열어", "신청", "시청", "영상"))
    enroll_lecture = (
        any(word in combined for word in ("입반", "수강", "강의 등록"))
        or (register_student and bool(lecture_hint))
        or open_video
    )
    send_account_notice = any(
        word in combined for word in ("초기 안내", "계정 안내", "아이디", "비밀번호", "로그인 안내")
    )
    # 삭제·교정 의도는 사진 문구가 아니라 로그인한 교사의 현재 요청에서만 읽는다.
    correct_enrollment = any(word in str(message or "") for word in ("잘못", "교정", "옮겨", "수강 취소", "등록 취소"))
    school, school_type, grade = _school_fields(lecture_hint)

    if not name:
        warnings.append("학생 이름을 확실히 읽지 못했습니다.")
    if not student_phone:
        warnings.append("학생 전화번호를 읽지 못했습니다. 신규 등록이면 로그인 아이디를 자동 발급합니다.")
    if not parent_phone:
        warnings.append("학부모 전화번호를 읽지 못했습니다.")
    if not any((register_student, enroll_lecture, open_video)):
        warnings.append("요청에서 등록 또는 영상 권한 작업을 찾지 못했습니다.")

    return ExtractedTeacherOpsRow(
        row_id=str(uuid.uuid4()),
        name=name,
        student_phone=student_phone,
        parent_phone=parent_phone,
        lecture_hint=lecture_hint,
        school=school,
        school_type=school_type,
        grade=grade,
        session_order=session_order,
        register_student=register_student,
        enroll_lecture=enroll_lecture,
        open_video=open_video,
        send_account_notice=send_account_notice,
        correct_enrollment=correct_enrollment,
        warnings=tuple(warnings),
    )


def inherit_previous_intent(
    *, row: ExtractedTeacherOpsRow, message: str, previous_row: dict | None
) -> ExtractedTeacherOpsRow:
    """Copy only the prior operation intent for phrases such as '이 친구도'."""
    compact = re.sub(r"\s+", "", str(message or ""))
    if previous_row is None or not any(word in compact for word in ("이친구도", "얘도", "이학생도", "같이")):
        return row
    return ExtractedTeacherOpsRow(
        row_id=row.row_id,
        name=row.name,
        student_phone=row.student_phone,
        parent_phone=row.parent_phone,
        lecture_hint=row.lecture_hint or str(previous_row.get("lecture_hint") or ""),
        school=row.school,
        school_type=row.school_type,
        grade=row.grade,
        session_order=row.session_order or previous_row.get("session_order"),
        register_student=row.register_student or bool(previous_row.get("register_student")),
        enroll_lecture=row.enroll_lecture or bool(previous_row.get("enroll_lecture")),
        open_video=row.open_video or bool(previous_row.get("open_video")),
        send_account_notice=row.send_account_notice or bool(previous_row.get("send_account_notice")),
        correct_enrollment=row.correct_enrollment or bool(previous_row.get("correct_enrollment")),
        warnings=row.warnings,
    )


def ocr_teacher_ops_image(image_bytes: bytes) -> str:
    if not image_bytes:
        raise ValidationError({"image": "사진이 비어 있습니다."})
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.verify()
        with Image.open(io.BytesIO(image_bytes)) as source:
            width, height = source.size
            if width < 240 or height < 240:
                raise ValidationError({"image": "사진이 너무 작습니다. 학생 정보 부분이 보이도록 다시 올려 주세요."})
            if width * height > 24_000_000:
                raise ValidationError({"image": "사진 해상도가 너무 큽니다. 2,400만 화소 이하로 줄여 주세요."})
            image = ImageOps.exif_transpose(source).convert("RGB")
            if max(image.size) > 3000:
                image.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
            grayscale = ImageOps.autocontrast(ImageOps.grayscale(image))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValidationError({"image": "손상되었거나 지원하지 않는 사진입니다."}) from exc

    try:
        import pytesseract

        return str(
            pytesseract.image_to_string(
                grayscale,
                lang="kor+eng",
                config="--psm 6",
                timeout=20,
            )
            or ""
        ).strip()
    except (pytesseract.TesseractError, RuntimeError) as exc:
        raise ValidationError({"image": "사진을 읽지 못했습니다. 잠시 후 다시 시도해 주세요."}) from exc
