# PATH: academy/domain/tools/question_splitter.py
# Rule-based question splitting from PDF pages.
#
# Detects question number patterns in extracted text blocks and
# determines bounding regions for each question.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    # forward-ref only — runtime 에는 caller (segment_dispatcher / pipeline) 가
    # PaperTypeResult 인스턴스를 그대로 전달.
    from academy.domain.tools.paper_type import PaperTypeResult  # noqa: F401


# 시험지/워크북 문항 번호 현실 상한.
# 대형 문제집 Beta는 한 권에서 1,000번 문항까지 구조화한다.
# 1,000 초과는 연도/페이지/잡음 숫자일 가능성이 커 anchor로 쓰지 않는다.
_MAX_LEGIT_QUESTION_NUMBER = 1000
_IGNORABLE_PDF_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


def _normalize_pdf_match_text(text: str) -> str:
    """Replace non-printing PDF extraction artifacts before anchor matching."""
    return _IGNORABLE_PDF_CONTROL_RE.sub(" ", text or "")


@dataclass
class TextBlock:
    """A block of text with its bounding box on the page."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


def _looks_like_learning_concept_page(
    full_text: str,
    *,
    has_choices: bool,
    has_question_indicator: bool,
) -> bool:
    """개념/본문 설명 페이지를 문제 후보에서 제외한다.

    교재 내지에는 CHAPTER/개념/추가 설명/정의형 본문이 많고, 섹션 번호
    ``1)`` / ``2)`` 가 문항 anchor처럼 보인다. 강한 문항 신호가 있으면
    보존하고, 설명형 신호가 충분히 누적된 페이지만 비문항으로 본다.
    """
    if has_question_indicator:
        return False
    if re.search(r"(?:✑\s*Note|\bNote\b)", full_text, re.IGNORECASE) and len(full_text) > 200:
        return True
    if (
        "생명과학" in full_text
        and not re.search(r"(?:ASCENT|개념\s*완성|유형\s*(?:쌓기|정복))", full_text)
        and re.search(
            r"(?:호르몬|항상성|삼투압|혈당량|체온|시상\s*하부|내분비샘|"
            r"인슐린|글루카곤|타이록신|항이뇨\s*호르몬|ADH)",
            full_text,
            re.IGNORECASE,
        )
    ):
        return True
    choice_count = sum(full_text.count(p) for p in ("①", "②", "③", "④", "⑤"))
    if has_choices and choice_count >= 4:
        return False
    if "기출 분석" in full_text and "문제 유형" in full_text:
        return True
    if len(full_text) < 120:
        return False

    chapter_or_unit = bool(
        re.search(
            r"(?:\bCHAPTER\b|\bUNIT\b|\bLESSON\b|"
            r"대\s*단원|중\s*단원|소\s*단원|"
            r"Step\s*\d+\s*[.:]?\s*(?:개념|내신|수능)\s*완성)",
            full_text,
            re.IGNORECASE,
        )
    )
    unit_inner_title = bool(
        re.search(
            r"(?:^|\s)\d{1,3}\s*(?:\|\s*)?\d{1,2}\s*[.．]\s*[가-힣A-Za-z]",
            full_text,
        )
    )
    learning_markers = re.findall(
        r"(?:개념\s*(?:완성|정리|학습)?|핵심\s*(?:개념|정리)?|"
        r"추가\s*설명|학습\s*목표|용어\s*정리|탐구\s*(?:활동|자료)?|"
        r"기출\s*분석|문제\s*유형|✑\s*Note|\bNote\b|"
        r"\[\s*설명\s*\]|참고\s*(?:예시|!)?|"
        r"구성\s*요소|상호\s*작용|계\s*\(\s*system\s*\)|"
        r"과학\s*의\s*기초|자연\s*세계\s*규모|미시\s*세계|거시\s*세계|"
        r"시간\s*규모|공간\s*규모|국제\s*단위계|기본량|유도량|"
        r"물리량|측정\s*표준|아날로그\s*신호|디지털\s*신호|"
        r"센서|자료와\s*정보|우주론|스펙트럼|기본\s*입자|쿼크|"
        r"전하량|질량\s*에너지|우주\s*배경\s*복사|원자핵|"
        r"주기율|주기율표|원소|금속\s*원소|비금속\s*원소|"
        r"알칼리\s*금속|할로젠\s*원소|비활성\s*기체|"
        r"화학\s*결합|이온\s*결합|공유\s*결합|금속\s*결합|"
        r"양이온|음이온|자유\s*전자|전기적\s*인력|"
        r"전기\s*전도성|녹는점|끓는점|반응성\s*순서|"
        r"화학\s*반응식|분자|분자식|전자\s*배치|"
        r"전기적\s*성질|반도체|다이오드|트랜지스터|"
        r"도체|절연체|전기\s*저항|전류|양공|"
        r"성운|원시별|주계열성|핵융합|구성하는\s*원소|질량비|"
        r"지각|해양|대기|규산염|신경계|중추\s*신경계|말초\s*신경계|"
        r"대뇌|소뇌|사이뇌|뇌줄기|척수|반사|뉴런|"
        r"호르몬|항상성|삼투압|혈당량|체온|시상\s*하부|내분비샘|"
        r"인슐린|글루카곤|타이록신|항이뇨\s*호르몬|ADH|"
        r"Ex\s*\)|PROJECT|과학\s*개념)",
        full_text,
        re.IGNORECASE,
    )
    definition_markers = re.findall(
        r"(?:^|\s)[가-힣A-Za-z0-9() ]{1,24}\s*[:：]\s*",
        full_text,
    )
    list_markers = re.findall(
        r"(?:^|\s)(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\s*[.)．]?|\d+\s*\)|[-*ㆍ•]\s+)",
        full_text,
    )
    explanatory_markers = re.findall(
        r"(?:이다|있다|있음|된다|작용|구성|영역|포함|관련된|중심부)",
        full_text,
    )

    structural_count = len(definition_markers) + len(list_markers)
    explanation_count = structural_count + len(explanatory_markers)
    chemistry_note_markers = re.findall(
        r"(?:화학\s*결합|이온\s*결합|공유\s*결합|금속\s*결합|"
        r"금속\s*양이온|자유\s*전자|전기적\s*인력|"
        r"전기\s*전도성|녹는점|끓는점|반응성\s*순서|"
        r"주기\s*\d+\s*족|\d+\s*주기\s*\d+\s*족|"
        r"전자\s*배치|전기적\s*성질|반도체|다이오드|"
        r"트랜지스터|도체|절연체|전기\s*저항|전류|양공)",
        full_text,
        re.IGNORECASE,
    )

    if (
        re.search(r"(?:✑\s*Note|\bNote\b)", full_text, re.IGNORECASE)
        and (explanation_count >= 4 or len(learning_markers) >= 3)
    ):
        return True
    if (
        len(chemistry_note_markers) >= 3
        and (structural_count >= 2 or explanation_count >= 4)
    ):
        return True
    if len(chemistry_note_markers) >= 5 and len(full_text) >= 180:
        return True
    if chapter_or_unit and len(learning_markers) >= 2 and explanation_count >= 4:
        return True
    if unit_inner_title and len(learning_markers) >= 3 and explanation_count >= 2:
        return True
    if "추가 설명" in full_text and structural_count >= 3 and explanation_count >= 8:
        return True
    if len(learning_markers) >= 4 and structural_count >= 3:
        return True
    return False


def is_non_question_page(blocks: List[TextBlock]) -> bool:
    """
    비문항 페이지 감지 — 표지, 진도표, 안내문, 정답지, 해설지 등.

    휴리스틱:
    - 정답지/해설지 패턴 감지 → True
    - 문항 지시문이나 보기 번호가 있으면 → False (문항 페이지)
    - 디자인 표지(WORKBOOK/PROJECT 류) → True
    - 시험지 헤더(제 N 교시 / 탐구 영역 / 홀수형) → True
    - 학습자료 챕터 표지(객서심화 / 메인자료 / 복습과제 등) → True
    - 비문항 키워드가 여러 개 있으면 → True
    """
    full_text = " ".join(b.text for b in blocks).strip()
    if not full_text:
        return True

    # 정답지 감지 (최우선): "⑴ × ⑵ O" "⑴ ② ⑵ ④" 같은 패턴 반복
    answer_pattern = re.findall(r"[⑴⑵⑶⑷⑸⑹⑺⑻⑼]\s*[×OX①②③④⑤]", full_text)
    if len(answer_pattern) >= 5:
        return True

    # ── 정답표 페이지 감지 ──
    # 운영 케이스 (Tenant 2 doc#143 p55/p65): "1. ④ 2. ④ 3. ① 4. ③ ..." 60+ 반복.
    # is_non_question_page 미차단 시 60 false anchor가 한 페이지에 폭증.
    # 패턴: "N. ①②③④⑤" 또는 "N. ④" 가 5개 이상 + 본문 지시문 없음.
    answer_table = re.findall(r"\b\d{1,3}\.\s*[①②③④⑤]", full_text)
    if len(answer_table) >= 5:
        question_indicators_early = [
            "옳은 것", "구하시오", "표시하시오", "고르시오", "서술하시오",
            "풀이 과정", "이에 대한 설명", "다음 중", "보기에서",
            "채우시오", "선택하시오", "써 넣으시오", "써넣으시오",
            "빈칸에 알맞은",
        ]
        if not any(kw in full_text for kw in question_indicators_early):
            return True

    # ── 서답형 답안지 표 감지 ──
    # 운영 케이스 (Tenant 2 doc#197): "문항 번호 / 유형 / 배점 / 정답" 표에
    # "서답형1", "(1) ..." 같은 답안 해설이 반복된다. 실제 문제지가 아니라
    # 채점용 답안지이므로 서답형 번호를 문항 anchor로 쓰면 안 된다.
    answer_sheet_header = all(
        token in full_text for token in ("문항", "번호", "유형", "배점", "정답")
    )
    written_answer_rows = re.findall(r"서\s*답\s*형\s*\d{1,3}", full_text)
    if answer_sheet_header and len(written_answer_rows) >= 2:
        return True

    # ── workbook 정답표 grid 감지 ──
    # 운영 케이스 (T2 commercial workbook answer reference pages):
    # "Part 03 ... Step 2. 내신완성 01 / ① / 02 / ⑤ ..." 또는
    # "01 해설 참조 02 해설 참조 ..." 형태. 보기 기호가 많아도 실제 문항이
    # 아니라 answer-key table 이므로 문항 지시문 판정보다 먼저 차단한다.
    answer_grid_choice_cells = re.findall(
        r"(?:^|\s)\d{1,2}\s*/\s*[①②③④⑤](?:\s*,\s*[①②③④⑤])*",
        full_text,
    )
    answer_grid_choice_cells.extend(
        re.findall(
            r"(?:^|\s)\d{1,2}\s+[①②③④⑤](?:\s*,\s*[①②③④⑤])*",
            full_text,
        )
    )
    answer_grid_ref_cells = re.findall(
        r"(?:^|\s)\d{1,2}\s*(?:/|\s)\s*해설\s*참조",
        full_text,
    )
    has_workbook_answer_grid_context = bool(
        re.search(r"\bPart\s*\d+\b|Step\s*\d+\s*[.:]?\s*(?:개념|내신|수능)\s*완성", full_text)
    )
    if has_workbook_answer_grid_context and (
        len(answer_grid_choice_cells) >= 8
        or len(answer_grid_ref_cells) >= 5
        or len(answer_grid_choice_cells) + len(answer_grid_ref_cells) >= 12
    ):
        return True
    if (
        len(answer_grid_choice_cells) >= 12
        and re.search(r"(?:PROJECT|고난도\s*(?:수능|대치동)|1등급\s*(?:만들기|다지기))", full_text)
    ):
        return True

    # ── workbook appendix TEST/정답 페이지 ──
    # T2 화학 workbook 후반부의 "이온과 이온화 TEST", "화학반응식 TEST"는
    # 20~40개 암기 항목/정답을 나열하는 부록이며 학원장 수동 GT에서 문제 crop
    # 대상으로 보지 않았다. 일반 시험지의 TEST 표제까지 과차단하지 않도록
    # 해당 좁은 부록 제목 + 많은 번호 항목일 때만 제외한다.
    appendix_numbered_items = re.findall(r"(?:^|\s)\d{1,2}\s*\.", full_text)
    if (
        len(appendix_numbered_items) >= 10
        and re.search(r"(?:이온과\s*이온화|화학\s*반응식)\s*TEST(?:\s*정답)?", full_text)
    ):
        return True

    # 해설지 감지: "번호. ⑴ ...이다." 소문항 패턴
    sub_q_pattern = re.findall(r"\d+\.\s*[⑴⑵⑶⑷⑸⑹⑺⑻⑼]", full_text)
    if len(sub_q_pattern) >= 2:
        question_indicators_early = [
            "옳은 것", "구하시오", "표시하시오", "고르시오", "서술하시오",
            "풀이 과정", "이에 대한 설명", "다음 중", "보기에서",
            "채우시오", "선택하시오", "써 넣으시오", "써넣으시오",
            "빈칸에 알맞은",
        ]
        if not any(kw in full_text for kw in question_indicators_early):
            return True

    # ── 해설지 페이지 감지 ──
    # 운영 케이스:
    #   1. Tenant 2 doc#120 p112-116: "10. 정답 ④ 문제 해설 ..."
    #   2. Step 3. 수능완성 1. 정답 ③ 문제 해설 ..."
    #   3. T1 doc 772 (지권의 변화 메인자료) p72 정답해설지: "30. [정답] 영덕"
    #      / "31. [정답] (가)는 ..." — N. 와 정답 사이 "[" 또는 "(" prefix 등장.
    # 패턴: "N. 정답" / "N. [정답]" / "N. (정답)" / "N. 문제 해설" 5+ 반복.
    explanation_answer = re.findall(
        r"\b\d{1,3}\s*\.\s*[\[\(\s]*(?:정\s*답|문제\s*해설)",
        full_text,
    )
    if len(explanation_answer) >= 3:
        return True

    # ── 단독 "정답 ①②③④⑤" 패턴 (N. 접두어 없음) ──
    # 운영 케이스 (모의고사 해설지): OCR이 N. 접두어를 흘리거나 layout이 깨져
    # "정답 ③", "정답 ⑤" 만 반복되는 페이지. 일반 본문에는 "정답"이 3+ 등장하지 않음.
    standalone_answer = re.findall(r"정\s*답\s*[①②③④⑤]", full_text)
    if len(standalone_answer) >= 3:
        return True

    # ── 해설지 OX 마커 패턴 — T1 doc 772 p56 케이스 ──
    # 운영 케이스 (지권의 변화 메인자료 p56 정답+문제 해설 페이지):
    #   "7. 그림은 ... | 1. A-C 지역... (O) | 2. 규모는 ... (X) | 3. ... (X) | ..."
    # 워크북 본문 페이지에는 "(O/X)" 가 표시 영역 placeholder 로만 등장 (학원장이
    # 채우기 전). 해설지에는 "(O)" / "(X)" / "(✓)" 단독 마커가 정답으로 채워져 있어
    # 본문 페이지보다 단독 OX 마커 빈도가 훨씬 높다.
    # 임계: 단독 OX 마커 (괄호 안 단일 O / X / ✓) 가 6+ 등장 + "정답" / "해설" 단어 동반.
    standalone_ox = re.findall(r"[\(（][\s]*[OX✓×][\s]*[\)）]", full_text)
    has_answer_word = "정답" in full_text or "해설" in full_text
    if len(standalone_ox) >= 6 and has_answer_word:
        return True

    # ── 학습자료 본문 항목번호 (zb\d+ ID) 페이지 감지 ──
    # 운영 케이스 (Tenant 2 doc#143 객서심화): "5. zb5) 다음 글을 읽고",
    # "11. zb11) 다음은", "17. zb17) 그림 (가)는" — 학습 항목 ID로 본문에 다수 등장.
    # zb패턴이 있고 + 학습 항목 번호로 anchor가 5+ 잡히는 페이지는 학습자료 본문.
    zb_markers = re.findall(r"\bzb\s*\d{1,3}\s*\)", full_text)
    zb_real_question_signal = bool(
        re.search(
            r"(?:<\s*보\s*기|고른\s*것|옳은\s*것|옳지\s*않은\s*것|"
            r"이에\s*대한\s*설명|①|②|③|④|⑤)",
            full_text,
        )
    )
    if len(zb_markers) >= 3 and not zb_real_question_signal:
        return True

    # 문항 페이지 강력 지표: 보기 번호 패턴
    choice_patterns = ["①", "②", "③", "④", "⑤", "ㄱ.", "ㄴ.", "ㄷ."]
    has_choices = any(p in full_text for p in choice_patterns)

    question_indicators = [
        "옳은 것", "구하시오", "표시하시오", "고르시오", "서술하시오",
        "풀이 과정", "이에 대한 설명", "다음 중", "보기에서",
        "물음에 답", "답하시오", "설명하시오", "쓰시오", "나열하시오",
        "적으시오", "적으십시오",
        "옳지 않은 것", "서술형", "단답형", "약술형", "무엇인가",
        "채우시오", "선택하시오", "써 넣으시오", "써넣으시오",
        "빈칸에 알맞은",
    ]
    has_question_indicator = any(kw in full_text for kw in question_indicators)

    if _looks_like_learning_concept_page(
        full_text,
        has_choices=has_choices,
        has_question_indicator=has_question_indicator,
    ):
        return True

    if has_choices or has_question_indicator:
        return False

    # ── 디자인 표지 페이지 감지 (학습자료/문제집 표지) ──
    # 운영 케이스 (Tenant 2): "Runner's High with God Min", "신과 함께 PROJECT WORKBOOK",
    # "1학기 중간고사 대비 문항편", "객·서 최종대비", "복습과제", "메인자료" 등.
    # 텍스트 양은 적고(< 300자), 디자인 키워드가 있으면 표지로 판정.
    design_cover_markers = re.findall(
        r"(?:WORKBOOK|PROJECT|Runner['’]?s\s*High|GOD\s*MIN|"
        r"\bTEST\s*[-–—]|TEST\s+\d|"
        r"Routine\s*복습\s*Test|Welcome\s*Test|"
        r"신과\s*함께|객\s*[·‧・·]\s*서\s*최종\s*대비|"
        r"객서\s*심화|객\s*·\s*서|"
        r"최종\s*대비|중간고사\s*대비|기말고사\s*대비|"
        r"내신\s*대비|기출\s*통과|"
        r"문항편|해설편|정답편|"
        r"복습\s*과제|메인\s*자료|개념\s*완성)",
        full_text,
    )
    if len(design_cover_markers) >= 1 and len(full_text) < 300:
        return True
    # 디자인 키워드가 2+ 동시에 있으면 텍스트 길이와 무관 (대형 표지 디자인)
    if len(design_cover_markers) >= 2 and len(full_text) < 800:
        return True

    # ── Lorem ipsum placeholder 표지 감지 ──
    # 운영 케이스 (Tenant 2 18 doc): "RUNNER'S HIGH WITH GOD MIN" + 라틴 lorem ipsum
    # placeholder 텍스트로 채워진 디자인 표지.
    # adipiscing/dolore/laoreet/aliquam/ipsum 같은 라틴어 단어가 한 페이지에 다수 등장.
    lorem_markers = re.findall(
        r"(?:adipiscing|dolore|laoreet|aliquam|nibh|tincidunt|euismod|"
        r"volutpat|nonummy|ipsum|consectetur|magna)",
        full_text,
        re.IGNORECASE,
    )
    if len(lorem_markers) >= 3:
        return True

    # ── 시험지 헤더 페이지 감지 (수능/모의고사 표제) ──
    # "제 4교시 / 신민T 신념 모의고사 / 통합과학 N제 / 탐구 영역 / 홀수형" 같은 표제 페이지.
    exam_header_markers = re.findall(
        r"(?:제\s*\d+\s*교시|탐구\s*영역|홀수형|짝수형|"
        r"수능\s*모의고사|N제|모의고사\s*\d+회차?)",
        full_text,
    )
    # 표제 페이지는 본문 대비 텍스트가 아주 적음 (디자인+여백)
    if len(exam_header_markers) >= 2 and len(full_text) < 400:
        return True
    # 헤더 키워드 3+ 동시 등장 + 본문 지시문 없음 → 표제 페이지 (길이 무관)
    if len(exam_header_markers) >= 3 and not has_question_indicator and not has_choices:
        return True

    # 표지 페이지 감지: 시험지 메타정보는 있고 문항 지표는 없음.
    # "학년도 1학기 기말고사 과목명 성명" 류 조합이 표지의 특징.
    cover_markers = re.findall(
        r"(?:\d+학년도|\d+학기|기말\s?고사|중간\s?고사|과목\s?명|"
        r"문제지|답안지|답란\s?지|수험\s?번호|응시\s?번호|"
        r"성\s?명|학\s?번|반\s?번호)",
        full_text,
    )
    # 표지는 보통 본문 대비 매우 짧음. 500자 기준은 빈 박스+헤더 정도.
    if len(cover_markers) >= 2 and len(full_text) < 500:
        return True

    # ── 텍스트 매우 적은 페이지 (디자인/이미지 위주) ──
    # 문항 지시문도 없고 보기도 없는데 텍스트가 100자 미만이면 표지/디자인 페이지.
    # "1.", "2." 같은 번호만 있어서 anchor가 잡히는 표지 디자인 페이지 차단.
    if len(full_text) < 100:
        return True

    # 목차/차례 페이지 감지 — 키워드 + 페이지 번호 점선 패턴.
    # ".... 5", "··· 12" 같은 점선 가이드 또는 "목차/차례/Contents" 헤더.
    toc_keyword = bool(
        re.search(r"(?:^|\s)(?:목\s?차|차\s?례|Contents?|Table\s+of\s+Contents|INDEX)(?:\s|$)", full_text)
    )
    dot_leader_count = len(re.findall(r"[.·…]{3,}\s*\d{1,3}\b", full_text))
    if toc_keyword or dot_leader_count >= 4:
        # 문항 지표가 없을 때만 — 본문 페이지가 아니라는 확신
        if not has_choices and not has_question_indicator:
            return True

    # 설명조 종결어미 빈도 기반 해설지 감지
    explanation_markers = re.findall(
        r"(?:이므로|때문이다|따라서|그러므로|해설|나타난다|관측된다|생성된다)",
        full_text,
    )
    if len(explanation_markers) >= 3 and not has_question_indicator:
        return True

    # 비문항 지표: 진도표, 강의방침, 안내 등
    non_question_indicators = [
        "진도", "운영 방침", "재시험", "클리닉", "홈페이지",
        "대단원", "중단원", "세부 내용", "난이도",
        "주차", "복습과제", "워크북", "시험일자", "일정", "등원",
    ]
    non_q_count = sum(1 for kw in non_question_indicators if kw in full_text)
    if non_q_count >= 3:
        return True

    return False


@dataclass
class QuestionRegion:
    """A detected question region on a page."""
    number: int
    bbox: Tuple[float, float, float, float]  # (x0, y0, x1, y1)
    page_index: int
    body_bbox: Optional[Tuple[float, float, float, float]] = None
    context_bbox: Optional[Tuple[float, float, float, float]] = None
    display_bbox: Optional[Tuple[float, float, float, float]] = None
    audit_bbox: Optional[Tuple[float, float, float, float]] = None
    semantic_flags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.body_bbox is None:
            self.body_bbox = self.bbox
        if self.display_bbox is None:
            self.display_bbox = self.bbox
        if self.audit_bbox is None:
            self.audit_bbox = self.bbox

    def set_display_bbox(self, bbox: Tuple[float, float, float, float]) -> None:
        """Update the product-facing crop while preserving audit/body boxes."""
        self.bbox = bbox
        self.display_bbox = bbox


# 선택형(객관식) 문항 번호 패턴. "1.", "1) ", "(1) ", "[1] ", "문제 1.", "문 1)".
# 운영 케이스 (Tenant 2 모의고사): "12)그림은..." 처럼 닫는 ")" 뒤에 공백 없이
# 한글/영문/괄호가 바로 오는 PDF가 흔함. 공백 강제 시 anchor 다수 미검출.
# → 다음 글자가 "공백 또는 한글/영문/숫자/구두점"이면 anchor로 인정.
# (단, ")" 직후가 또 ")" 이거나 "."이면 보기 ①②③④⑤ 또는 본문 일부일 수 있어 거부.)
_QUESTION_PATTERN = re.compile(
    r"^\s*(?:"
    # PyMuPDF may duplicate overlaid/outlined problem numbers: "98 98. ..."
    # Treat that as one anchor, not as body text.
    r"(\d{1,3})\s+\1\s*[.)](?=\s|[가-힣A-Za-z(<【\[\"'“‘])"
    r"|"
    r"(\d{1,3})\s*[.)](?=\s|[가-힣A-Za-z(<【\[\"'“‘])"  # "1." / "1) " / "12)그림"
    r"|"
    # OCR이 "1."을 "1 /"처럼 읽는 학생 촬영 시험지 보정. "1/2" 같은 비율은
    # 숫자 직후라 매치하지 않는다.
    r"(\d{1,3})\s*/(?=\s|[가-힣A-Za-z(<【\[\"'“‘])"
    r"|"
    r"\((\d{1,3})\)\s"              # "(1) "
    r"|"
    r"\[(\d{1,3})\]\s"              # "[1] "
    r"|"
    r"(?:문제|문)\s*(\d{1,3})\s*[.)]"  # "문제1." or "문 1)"
    r")"
)

_SOURCE_PREFIXED_QUESTION_PATTERN = re.compile(
    r"^\s*[\[【]\s*[^\]】]{2,90}\s*[\]】]\s*(?:/|\s)*(.+)$",
    re.DOTALL,
)
_SOURCE_PREFIX_ONLY_PATTERN = re.compile(
    r"^\s*[\[【]\s*[^\]】]{2,90}\s*[\]】]\s*$"
)
_QUESTION_TYPE_PREFIX_ONLY_PATTERN = re.compile(
    r"^\s*[\[【(（]?\s*(?:객관식|선택형|서술형|논술형|단답형|약술형|주관식)\s*[\]】)）]?\s*$"
)
_SECTION_TITLE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:Step\s*\d+\s*[.:]?\s*)?"
    r"(?:개념\s*완성|내신\s*완성|수능\s*완성|실전\s*문제|대표\s*문제)\s*$",
    re.IGNORECASE,
)

# 서술형/논술형/단답형 섹션은 1부터 번호를 리셋하므로 선택형 번호와 충돌함.
# 섹션별 number-space offset을 부여해 크로스-페이지 중복 제거 시 legit 문항이
# 잘못 드롭되지 않도록 한다.
# 예: `[서술형 1]` → 101, `[서술형 2]` → 102, `[논술형 1]` → 201.
# OCR은 "술"을 "답"으로 자주 오인식하므로 variant도 허용.
_SECTION_OFFSETS = {
    "서술": 100, "서답": 100,
    "논술": 200, "논답": 200,
    "단답": 300, "단술": 300,
    "약술": 400, "약답": 400,
}
_SECTION_PATTERN = re.compile(
    r"^\s*\[?\s*"
    r"(서\s*술|서\s*답|논\s*술|논\s*답|단\s*답|단\s*술|약\s*술|약\s*답)"
    # 형 필수 — "서술 1가지 방법..." 같은 본문 false positive 차단.
    # OCR이 형을 흘리는 경우는 드물고, 본문에서 "서술/논술/단답" 단어가
    # 숫자 앞에 등장하는 케이스가 더 흔함. 형 없는 패턴은 anchor 후보에서 제외.
    r"\s*형\s*[\]】)）]?\s*[\[【(（]?\s*(\d{1,3})"
)

_SHARED_QUESTION_RANGE_PATTERN = re.compile(
    r"^\s*[\[【(]\s*"
    r"(\d{1,3})\s*(?:[,，~\-–]|및)\s*(\d{1,3})"
    r"\s*[\]】)]?"
)
_INLINE_MAIN_QUESTION_PATTERN = re.compile(
    r"^\s*(\d{1,3})\s*[.)](?=\s|[가-힣A-Za-z(<【\[\"'“‘])"
)
_SHORT_WORKBOOK_PROMPT_PATTERN = re.compile(
    r"(?:빈\s*칸|써\s*넣으시오|써넣으시오|쓰시오|적으시오|구하시오|"
    r"서술하시오|설명하시오|답하시오|의미한다|나타낸다|\(\s*\))"
)
_WRITTEN_RESPONSE_PROMPT_PATTERN = re.compile(
    r"(?:써\s*넣으시오|써넣으시오|쓰시오|적으시오|구하시오|"
    r"서술하시오|설명하시오|답하시오)"
)
_REASONING_RESPONSE_PROMPT_PATTERN = re.compile(
    r"(?:(?:까닭|이유|원인).{0,40}(?:쓰시오|서술하시오|설명하시오|답하시오)|"
    r"(?:쓰시오|서술하시오|설명하시오|답하시오).{0,40}(?:까닭|이유|원인))"
)
_FILL_IN_WORKSHEET_PROMPT_PATTERN = re.compile(
    r"(?:빈\s*칸.{0,20}(?:알맞은|적절한).{0,20}(?:말|용어|기호).{0,20}"
    r"(?:써\s*넣으시오|써넣으시오|쓰시오|적으시오)|"
    r"(?:써\s*넣으시오|써넣으시오|쓰시오|적으시오).{0,20}빈\s*칸)"
)
_DENSE_OWNER_STEM_PATTERN = re.compile(
    r"(?:다음|그림|표|자료|내용|설명|물음|빈\s*칸|"
    r"써\s*넣으시오|써넣으시오|쓰시오|적으시오|답하시오|"
    r"서술하시오|설명하시오|옳은|옳지\s*않은|고르)"
)
_PLAIN_NUMBERED_ROW_PATTERN = re.compile(r"^\s*\d{1,2}\s*[.)]\s+")
_OX_MARKING_PATTERN = re.compile(r"(?:[Oo]\s*/\s*[Xx]|○|☓|옳은.{0,40}옳지)")
_VISUAL_CONTEXT_PROMPT_PATTERN = re.compile(
    r"(?:그림|그래프|자료|모형|사진|도표|(?<![가-힣])표(?=[\s는를의와과에,.]))"
)
_LARGE_VISUAL_CONTEXT_PROMPT_PATTERN = re.compile(r"(?:그림|그래프|자료|모형|사진|도표)")
_PRIOR_CONTEXT_REFERENCE_PATTERN = re.compile(
    r"(?:(?<![가-힣])위\s*(?:의\s*)?(?:실험|그림|자료|물체|문제|내용|보기|표)|"
    r"이를\s*토대로|해당\s*(?:자료|그림|실험))"
)


# Marginal 큰 번호 패턴 — 워크북/메인자료 페이지 좌측 marginal column 에 standalone
# "3." / "4." 형식으로 박힌 큰 번호. 학원장 mental model 의 "한 문제 단위" anchor.
# 본문 sub-item anchor ("1. 다음 글은...") 와 구분하기 위해 매우 짧은 standalone block 만 인정.
_MARGINAL_NUMBER_PATTERN = re.compile(
    r"^\s*(\d{1,3})(?:\s+\1)?\s*\.?\s*$"
)


def _extract_marginal_question_number(text: str) -> Optional[int]:
    """짧은 standalone 'N.' / 'N' block (또는 multi-line block 첫 줄) 에서 큰 문제 번호 추출.

    워크북 marginal column 의 main question anchor 용. 본문 sub-item anchor 와 구분.

    Production text extraction (`page.get_text("blocks")`) 은 마진 "3." 와 본문 첫 줄
    "그림은 화산..." 을 한 block 으로 묶어 반환한다 (multi-line block). local fixture
    diag 의 `get_text("dict")` 와 다르다. 따라서 block.text 의 **첫 줄** 만 분리해
    marginal 패턴 검사.

    조건:
      - 첫 줄 length ≤ 5 char (e.g. "3.", "12.", "3")
      - 첫 줄 regex: 숫자 + 선택적 점 + 공백 외 다른 문자 없음
    """
    stripped = _normalize_pdf_match_text(text).strip()
    if not stripped:
        return None
    # multi-line block 첫 줄 추출 (production blocks 모드 대응).
    first_line = stripped.split("\n", 1)[0].strip()
    if not first_line or len(first_line) > 5:
        return None
    m = _MARGINAL_NUMBER_PATTERN.match(first_line)
    if not m:
        return None
    try:
        num = int(m.group(1))
        if 1 <= num <= _MAX_LEGIT_QUESTION_NUMBER:
            return num
    except ValueError:
        pass
    return None


def _looks_like_footer_folio(
    block: TextBlock,
    *,
    page_width: float,
    page_height: float,
) -> bool:
    if page_width <= 0 or page_height <= 0:
        return False
    if block.y0 < page_height * 0.86:
        return False
    stripped = re.sub(r"\s+", "", (block.text or "").strip())
    if not stripped or len(stripped) > 12:
        return False
    return bool(re.fullmatch(r"[-–—]?\d{1,3}(?:[/／]\d{1,3})?[-–—]?", stripped))


def _looks_like_short_workbook_prompt(text: str) -> bool:
    return bool(_SHORT_WORKBOOK_PROMPT_PATTERN.search(text or ""))


def _looks_like_written_response_prompt(text: str) -> bool:
    return bool(_WRITTEN_RESPONSE_PROMPT_PATTERN.search(text or ""))


def _looks_like_reasoning_response_prompt(text: str) -> bool:
    return bool(_REASONING_RESPONSE_PROMPT_PATTERN.search(text or ""))


def _mentions_visual_context(text: str) -> bool:
    return bool(_VISUAL_CONTEXT_PROMPT_PATTERN.search(text or ""))


def _mentions_large_visual_context(text: str) -> bool:
    return bool(_LARGE_VISUAL_CONTEXT_PROMPT_PATTERN.search(text or ""))


def _references_prior_context(text: str) -> bool:
    return bool(_PRIOR_CONTEXT_REFERENCE_PATTERN.search(text or ""))


def _has_bilateral_marginal_anchors(
    blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
) -> bool:
    """Pixel-only dual pages with real left/right marginal numbers are true dual."""
    if page_width <= 0 or page_height <= 0:
        return False
    mid_x = page_width * 0.5
    local_margin = page_width * 0.15
    left_count = 0
    right_count = 0
    for block in blocks:
        if _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        ):
            continue
        if _extract_marginal_question_number(block.text) is None:
            continue
        center_x = (block.x0 + block.x1) / 2
        if block.x0 < local_margin:
            left_count += 1
        elif center_x >= mid_x and block.x0 < (mid_x + local_margin):
            right_count += 1
    return left_count >= 1 and right_count >= 1


def _extract_top_level_question_number(text: str) -> Optional[int]:
    stripped = (text or "").strip()
    if re.match(r"^\s*\(\d{1,3}\)", stripped):
        return None
    return _extract_question_number(stripped)


def _looks_like_dense_owner_question_stem(text: str) -> bool:
    return bool(_DENSE_OWNER_STEM_PATTERN.search(text or ""))


def _looks_like_plain_numbered_subrow(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or re.match(r"^\s*\(\d{1,3}\)", stripped):
        return False
    if _SOURCE_PREFIXED_QUESTION_PATTERN.match(stripped):
        return False
    if not _PLAIN_NUMBERED_ROW_PATTERN.match(stripped):
        return False
    number = _extract_question_number(stripped)
    return number is not None and number <= 20


def _looks_like_low_numbered_subrow_run(numbers: list[int]) -> bool:
    present = {number for number in numbers if 1 <= number <= 20}
    return len(numbers) >= 5 and {1, 2, 3, 4, 5}.issubset(present)


def _looks_like_ox_marking_cluster(owner_text: str, row_texts: list[str]) -> bool:
    owner_has_marking_instruction = bool(_OX_MARKING_PATTERN.search(owner_text or ""))
    marked_rows = sum(1 for text in row_texts if _OX_MARKING_PATTERN.search(text or ""))
    return owner_has_marking_instruction and marked_rows >= 4


def _has_dense_numbered_subrows_under_main_stem(
    blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
) -> bool:
    """Detect one physical question that owns many numbered fill-in rows.

    Some workbook pages have a large main question stem like ``1. 다음 ...`` or
    ``33. ...`` followed by rows ``1.``, ``2.``, ... inside the same problem.
    The dense-row fast path must not split those rows into product questions.
    """
    content_blocks = [
        block for block in blocks
        if not _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        )
    ]
    if not content_blocks:
        return False

    anchors: list[tuple[int, TextBlock]] = []
    for block in sorted(content_blocks, key=lambda item: (item.y0, item.x0)):
        number = _extract_top_level_question_number(block.text or "")
        if number is not None:
            anchors.append((number, block))
    if len(anchors) < 6:
        return False

    full_text = "\n".join(block.text or "" for block in content_blocks)
    if not _looks_like_dense_owner_question_stem(full_text):
        return False

    plain_rows = [
        (number, block)
        for number, block in anchors
        if _looks_like_plain_numbered_subrow(block.text)
    ]
    if len(plain_rows) < 5:
        return False

    numbers = [number for number, _ in anchors]
    has_high_main = any(number >= 10 for number in numbers)
    has_duplicate_leading_main = any(
        number <= 5 and numbers.count(number) >= 2
        for number in numbers[:4]
    )
    if not (has_high_main or has_duplicate_leading_main):
        return False

    stem_blocks = [
        (number, block)
        for number, block in anchors
        if _looks_like_dense_owner_question_stem(block.text)
    ]
    if not stem_blocks:
        return has_high_main

    for number, stem in stem_blocks:
        below_rows = [
            row_number
            for row_number, row in plain_rows
            if row is not stem and row.y0 > stem.y0
        ]
        if len(below_rows) >= 5 and (number >= 10 or number in below_rows):
            return True
    return False


def _has_bilateral_top_level_question_anchors(
    blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
) -> bool:
    """Pixel-only dual pages with left/right body anchors are still dual."""
    if page_width <= 0 or page_height <= 0:
        return False
    mid_x = page_width * 0.5
    gutter_tolerance = page_width * 0.035
    left_count = 0
    right_count = 0
    for block in blocks:
        if _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        ):
            continue
        if _extract_top_level_question_number(block.text or "") is None:
            continue
        if block.x0 >= mid_x - gutter_tolerance:
            right_count += 1
        else:
            left_count += 1
    return left_count >= 1 and right_count >= 1


def _looks_like_dense_fill_in_row_page(
    blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
) -> bool:
    """Dense fill-in worksheets should be cropped by numbered row."""
    content_blocks = [
        block for block in blocks
        if not _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        )
    ]
    if not content_blocks:
        return False
    full_text = "\n".join(block.text or "" for block in content_blocks)
    if not _FILL_IN_WORKSHEET_PROMPT_PATTERN.search(full_text):
        return False

    numbers: list[int] = []
    for block in content_blocks:
        number = _extract_top_level_question_number(block.text or "")
        if number is not None:
            numbers.append(number)
    if len(numbers) < 8:
        return False
    return max(numbers) >= 8


def _build_dense_fill_in_row_regions(
    blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
    page_index: int,
) -> List[QuestionRegion]:
    content_blocks = [
        block for block in blocks
        if not _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        )
    ]
    if not content_blocks:
        return []

    anchors: list[tuple[int, TextBlock]] = []
    for block in sorted(content_blocks, key=lambda item: (item.y0, item.x0)):
        number = _extract_top_level_question_number(block.text or "")
        if number is None:
            continue
        if _FILL_IN_WORKSHEET_PROMPT_PATTERN.search(block.text or ""):
            continue
        anchors.append((number, block))
    if len(anchors) < 2:
        return []

    pad_x = max(page_width * 0.012, 2.0)
    pad_top = max(page_height * 0.003, 2.0)
    pad_bottom = max(page_height * 0.012, 6.0)
    x0 = max(0.0, min(block.x0 for block in content_blocks) - pad_x)
    x1 = min(page_width, max(block.x1 for block in content_blocks) + pad_x)

    footer_top = page_height * 0.94
    regions: List[QuestionRegion] = []
    for idx, (number, block) in enumerate(anchors):
        y0 = max(0.0, block.y0 - pad_top)
        if idx + 1 < len(anchors):
            next_y0 = anchors[idx + 1][1].y0
            y1 = max(y0 + page_height * 0.025, next_y0 - pad_top)
        else:
            lower_blocks = [b for b in content_blocks if b.y0 >= block.y0 - 1.0]
            content_bottom = max((b.y1 for b in lower_blocks), default=block.y1)
            y1 = min(page_height, max(content_bottom + pad_bottom, y0 + page_height * 0.035))
        y1 = min(y1, footer_top)
        if y1 <= y0:
            continue
        regions.append(
            QuestionRegion(
                number=number,
                bbox=(x0, y0, x1, y1),
                page_index=page_index,
                semantic_flags=("short_workbook_prompt",),
            )
        )
    return regions


def _expand_inline_anchor_blocks(text_blocks: List[TextBlock]) -> List[TextBlock]:
    """PyMuPDF가 한 block에 합친 후속 문항 anchor를 가상 block으로 보강한다.

    예: 선택지 줄 뒤에 같은 block으로 ``4.\n다음은...`` 이 붙으면 기존 anchor
    추출은 block 시작의 3번만 본다. 줄 시작의 plain ``N.`` main anchor만 보강하고,
    ``(1)`` 같은 소문항은 보강하지 않는다.
    """
    expanded: List[TextBlock] = []
    for block in text_blocks:
        expanded.append(block)
        lines = [line.strip() for line in block.text.splitlines()]
        if len(lines) < 2:
            continue
        line_height = (block.y1 - block.y0) / max(len(lines), 1)
        for line_idx, line in enumerate(lines[1:], start=1):
            m = _INLINE_MAIN_QUESTION_PATTERN.match(line)
            if not m:
                m = _MARGINAL_NUMBER_PATTERN.match(line)
                if m and not re.search(r"[.)]", line):
                    continue
            if not m:
                continue
            try:
                num = int(m.group(1))
            except ValueError:
                continue
            if not (1 <= num <= _MAX_LEGIT_QUESTION_NUMBER):
                continue
            virtual_text = "\n".join(lines[line_idx:]).strip()
            if not virtual_text:
                continue
            tail_probe = " ".join(lines[line_idx:line_idx + 3])
            if not (
                re.search(
                    r"(?:다음|그림|표|자료|이에|대한|설명|물음|쓰시오|고르)",
                    tail_probe,
                )
                or re.search(r"[가-힣]{3,}", tail_probe)
            ):
                continue
            y0 = block.y0 + line_height * line_idx
            expanded.append(
                TextBlock(
                    text=virtual_text,
                    x0=block.x0,
                    y0=y0,
                    x1=block.x1,
                    y1=block.y1,
                )
            )
    return expanded


def _looks_like_source_prefix_only(text: str) -> bool:
    stripped = (text or "").strip()
    if not _SOURCE_PREFIX_ONLY_PATTERN.match(stripped):
        return False
    return bool(re.search(r"(?:년|학평|평가원|수능|기출|문제|번)", stripped))


def _looks_like_question_type_prefix_only(text: str) -> bool:
    return bool(_QUESTION_TYPE_PREFIX_ONLY_PATTERN.match((text or "").strip()))


def _looks_like_section_title_prefix_only(text: str) -> bool:
    return bool(_SECTION_TITLE_PREFIX_PATTERN.match((text or "").strip()))


def _section_offset_from_text(text: str) -> int | None:
    sec_m = _SECTION_PATTERN.match((text or "").strip())
    if not sec_m:
        return None
    section_key = re.sub(r"\s+", "", sec_m.group(1))[:2]  # "서술" 등
    return _SECTION_OFFSETS.get(section_key)


def _extract_question_number(text: str) -> Optional[int]:
    """Extract question number from text block content.

    선택형 1~60 그대로. 서술형 N → 100+N. 논술형 N → 200+N. 단답형 N → 300+N.
    번호 공간을 분리해 서술형 리셋 번호가 선택형과 충돌하지 않게 한다.
    """
    text = _normalize_pdf_match_text(text).strip()
    if not text:
        return None

    # 0. 공통 자료 묶음: "[9, 10] 그림은 ..." → 첫 문항 번호(9)를 anchor로 사용.
    shared_m = _SHARED_QUESTION_RANGE_PATTERN.match(text)
    if shared_m:
        try:
            start = int(shared_m.group(1))
            end = int(shared_m.group(2))
            if (
                1 <= start <= _MAX_LEGIT_QUESTION_NUMBER
                and 1 <= end <= _MAX_LEGIT_QUESTION_NUMBER
                and start < end
                and end - start <= 10
            ):
                return start
        except ValueError:
            pass

    # 1. 서술형/논술형/단답형/약술형 섹션 패턴 먼저 검사
    sec_m = _SECTION_PATTERN.match(text)
    if sec_m:
        offset = _section_offset_from_text(text) or 0
        try:
            sub_num = int(sec_m.group(2))
            if 1 <= sub_num <= _MAX_LEGIT_QUESTION_NUMBER:
                return offset + sub_num
        except ValueError:
            pass

    # 2. 선택형 번호 패턴
    m = _QUESTION_PATTERN.match(text)
    if not m:
        source_prefixed = _SOURCE_PREFIXED_QUESTION_PATTERN.match(text)
        if source_prefixed:
            m = _QUESTION_PATTERN.match(source_prefixed.group(1).strip())
    if not m:
        return None

    for g in m.groups():
        if g is not None:
            try:
                num = int(g)
                if 1 <= num <= _MAX_LEGIT_QUESTION_NUMBER:
                    return num
            except ValueError:
                continue
    return None


def _detect_column_layout(
    blocks: List[TextBlock],
    page_width: float,
) -> bool:
    """Detect if the page has a dual-column layout.

    Two heuristics (OR):
      1. >20% of text blocks have x0 > page_width * 0.45 (right column populated)
      2. anchor "N." pattern blocks distributed in both halves (≥2 each side)

    조건 (2) 추가 근거: 운영 doc#177/329/292/291 (T2 시험지) — Vision OCR이
    페이지 좌/우 column을 한 line으로 묶거나 우측 column block 수가 적어 (1)
    임계값을 넘기지 못하는 경우, dual-column 미인식 → split_questions 단계에서
    next_block.y0 < start_block.y0 인 cross-column anchor를 single-column으로
    잘못 처리 → strip(10px) bbox 결함. anchor 분포 기반 판정으로 보완.
    """
    if not blocks or page_width <= 0:
        return False

    mid_x = page_width * 0.45
    right_count = sum(1 for b in blocks if b.x0 > mid_x)
    if right_count > len(blocks) * 0.2:
        return True

    # Fallback: anchor "N."이 좌/우 column 모두에 배치되어 있으면 dual-column.
    # Some workbook pages have 2 left questions and only 1 right question; requiring
    # 2+ anchors on each side misclassifies them as single-column and merges columns.
    left_anchors = sum(
        1 for b in blocks
        if b.x0 <= mid_x and _extract_question_number(b.text or "") is not None
    )
    right_anchors = sum(
        1 for b in blocks
        if b.x0 > mid_x and _extract_question_number(b.text or "") is not None
    )
    if left_anchors >= 2 and right_anchors >= 2:
        return True
    return left_anchors >= 2 and right_anchors >= 1 and (left_anchors + right_anchors) >= 3


def _detect_quad_layout(
    blocks: List[TextBlock],
    page_width: float,
    page_height: float,
) -> bool:
    """4분할(2x2) 레이아웃 감지 — 한 페이지에 4문항이 grid로 배치된 시험지.

    운영 케이스 (Tenant 2 doc#148 4분할 기출 시험지): 가로 띠로 잘려 multi-question
    bleed 발생. 4분면 각각 독립 anchor 검색이 필요하다.

    Heuristic:
      - 4 quadrant 모두에 텍스트 블록이 일정량 이상 분포
      - 페이지 가운데 가로띠 + 세로띠에 텍스트가 거의 없음 (gutter)
    """
    if not blocks or page_width <= 0 or page_height <= 0 or len(blocks) < 12:
        return False

    mid_x = page_width / 2
    mid_y = page_height / 2
    gutter_x = page_width * 0.05
    gutter_y = page_height * 0.05

    tl = sum(1 for b in blocks if b.x1 < mid_x and b.y1 < mid_y)
    tr = sum(1 for b in blocks if b.x0 > mid_x and b.y1 < mid_y)
    bl = sum(1 for b in blocks if b.x1 < mid_x and b.y0 > mid_y)
    br = sum(1 for b in blocks if b.x0 > mid_x and b.y0 > mid_y)

    # 4분면 모두 충분한 텍스트
    if min(tl, tr, bl, br) < 3:
        return False

    # 가운데 가로띠/세로띠에 텍스트 거의 없음 (gutter 검증)
    in_h_gutter = sum(
        1 for b in blocks if mid_y - gutter_y < b.y0 < mid_y + gutter_y
    )
    in_v_gutter = sum(
        1 for b in blocks if mid_x - gutter_x < b.x0 < mid_x + gutter_x
    )
    threshold = max(2, len(blocks) * 0.06)
    return in_h_gutter <= threshold and in_v_gutter <= threshold


def _paper_type_value(paper_type: Optional["PaperTypeResult"]) -> str:
    if paper_type is None:
        return ""
    raw = getattr(paper_type, "paper_type", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _is_continuous_scan_type(paper_type: Optional["PaperTypeResult"]) -> bool:
    """학교 시험지/학생 촬영 페이지처럼 문항 번호가 연속 증가하는 스캔 계열."""
    return _paper_type_value(paper_type) in {
        "scan_single",
        "scan_dual",
        "quadrant",
        "student_answer_photo",
    }


def _filter_continuous_anchor_sequence(
    candidates: List[Tuple[int, int, bool]],
) -> List[Tuple[int, int, bool]]:
    """OCR 잡음 anchor를 분리 전에 제거한다.

    Google Vision은 보기 번호 ⑤/⑦, 주기율표 칸 번호, 손글씨 숫자를 ``5.``/``7.``
    같은 본문 anchor처럼 반환할 때가 있다. 시험지 스캔은 레이아웃 순서상 문항
    번호가 거의 항상 연속 증가하므로, 가장 긴 증가 부분수열을 고르되 번호 gap이
    작은 경로를 우선한다. 워크북/메인자료의 비연속 큰 번호에는 적용하지 않는다.
    """
    if len(candidates) < 3:
        return candidates

    # DP state: (length, -gap_penalty, -last_number, path_indices)
    best_states: List[Tuple[int, int, int, List[int]]] = []
    for i, (num, _, _) in enumerate(candidates):
        state = (1, 0, -num, [i])
        for j in range(i):
            prev_num = candidates[j][0]
            if prev_num >= num:
                continue
            prev_len, prev_gap_score, _, prev_path = best_states[j]
            gap_penalty = max(0, num - prev_num - 1)
            cand_state = (
                prev_len + 1,
                prev_gap_score - gap_penalty,
                -num,
                [*prev_path, i],
            )
            if cand_state[:3] > state[:3]:
                state = cand_state
        best_states.append(state)

    best = max(best_states, key=lambda s: s[:3])
    # 필터가 실제 정보를 잃지 않게 2개 이하만 남는 극단 케이스는 원본 보존.
    if best[0] < 3:
        return candidates
    keep = set(best[3])
    return [c for idx, c in enumerate(candidates) if idx in keep]


def _tighten_region_to_content(
    *,
    blocks: List[TextBlock],
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    page_width: float,
    page_height: float,
    margin: float,
    allow_short_content: bool = False,
    min_height_ratio: float | None = None,
    allow_visual_context_min: bool = False,
) -> Tuple[float, float, float, float]:
    """Shrink an anchor-derived region to the actual text content inside it."""
    if not blocks or page_width <= 0 or page_height <= 0:
        return x0, y0, x1, y1

    footer_y = page_height * 0.92
    region_blocks: List[TextBlock] = []
    for block in blocks:
        if _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        ):
            continue
        if block.y0 >= footer_y:
            continue
        if block.y1 < y0 or block.y0 > y1:
            continue
        overlap_x = max(0.0, min(x1, block.x1) - max(x0, block.x0))
        block_w = max(1.0, block.x1 - block.x0)
        if overlap_x / block_w < 0.35:
            continue
        region_blocks.append(block)

    if not region_blocks:
        return x0, y0, x1, y1

    region_text = " ".join(b.text or "" for b in region_blocks)
    inferred_min_height_ratio = min_height_ratio
    if inferred_min_height_ratio is None:
        if allow_visual_context_min and _mentions_large_visual_context(region_text):
            inferred_min_height_ratio = 0.22

    content_x0 = min(b.x0 for b in region_blocks)
    content_x1 = max(b.x1 for b in region_blocks)
    content_y1 = max(b.y1 for b in region_blocks)

    pad_x = max(page_width * 0.012, margin * 2)
    pad_y_bottom = max(
        page_height * (0.020 if allow_short_content else 0.035),
        margin * 3,
    )
    min_w = page_width * 0.12
    min_h = page_height * 0.06
    wide_single_line_short = (
        allow_short_content
        and (content_x1 - content_x0) >= page_width * 0.78
        and len(region_blocks) <= 3
        and not _mentions_large_visual_context(region_text)
    )
    if allow_short_content:
        ratio = 0.045 if wide_single_line_short else 0.075
        min_h = max(page_height * ratio, margin * 3, 12.0)
    target_min_h = min_h
    if inferred_min_height_ratio is not None:
        target_min_h = max(target_min_h, page_height * inferred_min_height_ratio)

    tightened_x0 = max(x0, content_x0 - pad_x)
    tightened_x1 = min(x1, content_x1 + pad_x)
    if (
        allow_short_content
        and not _mentions_large_visual_context(region_text)
        and x0 >= page_width * 0.45
        and (x1 - x0) <= page_width * 0.62
        and (content_x1 - content_x0) <= (x1 - x0) * 0.72
    ):
        tightened_x1 = max(tightened_x1, x1)
    tightened_y1 = min(y1, max(content_y1 + pad_y_bottom, y0 + target_min_h))

    if tightened_x1 - tightened_x0 < min_w:
        return x0, y0, x1, y1
    if tightened_y1 - y0 < min_h - 1e-6:
        if allow_short_content:
            ratio = 0.045 if wide_single_line_short else 0.075
            short_min_h = max(page_height * ratio, margin * 3, 12.0)
            tightened_y1 = min(y1, max(tightened_y1, y0 + short_min_h))
            return tightened_x0, y0, tightened_x1, tightened_y1
        return x0, y0, x1, y1
    return tightened_x0, y0, tightened_x1, tightened_y1


def _extract_shared_question_range(text: str) -> Optional[Tuple[int, int]]:
    m = _SHARED_QUESTION_RANGE_PATTERN.match((text or "").strip())
    if not m:
        return None
    try:
        start = int(m.group(1))
        end = int(m.group(2))
    except ValueError:
        return None
    if (
        1 <= start <= _MAX_LEGIT_QUESTION_NUMBER
        and 1 <= end <= _MAX_LEGIT_QUESTION_NUMBER
        and start < end
        and end - start <= 10
    ):
        return (start, end)
    return None


def _shared_range_uses_answer_instruction(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return bool(
        re.search(
            r"(?:물음|문제)에답|(?:다음|아래)물음",
            compact,
        )
    )


def _expand_singleton_cross_column_shared_range(
    regions: List[QuestionRegion],
    text_blocks: List[TextBlock],
    *,
    shared_block: TextBlock,
    start: int,
    page_width: float,
    page_height: float,
    mid_x: float,
    margin: float,
) -> bool:
    """Join a shared range with its same-number question in the other column."""
    region = next((item for item in regions if item.number == start), None)
    if region is None:
        return False

    def _in_left(block: TextBlock) -> bool:
        return ((block.x0 + block.x1) / 2) < mid_x

    shared_in_left = _in_left(shared_block)
    body_anchors = [
        block
        for block in text_blocks
        if block is not shared_block
        and _in_left(block) != shared_in_left
        and _extract_question_number(block.text) == start
        and block.y0 < page_height * 0.92
    ]
    if not body_anchors:
        return False
    body_anchor = min(body_anchors, key=lambda block: (block.y0, block.x0))

    def _content_until_next_anchor(
        *,
        in_left: bool,
        start_y: float,
        ignored_block: TextBlock,
    ) -> List[TextBlock]:
        next_anchor_y = page_height * 0.92
        for block in text_blocks:
            if block is ignored_block or _in_left(block) != in_left:
                continue
            if block.y0 <= start_y + 1.0:
                continue
            if _extract_question_number(block.text) is not None:
                next_anchor_y = min(next_anchor_y, block.y0 - margin)

        return [
            block
            for block in text_blocks
            if _in_left(block) == in_left
            and block.y1 >= start_y - margin
            and block.y0 <= next_anchor_y
            and not _looks_like_footer_folio(
                block,
                page_width=page_width,
                page_height=page_height,
            )
        ]

    shared_content = _content_until_next_anchor(
        in_left=shared_in_left,
        start_y=shared_block.y0,
        ignored_block=shared_block,
    )
    body_content = _content_until_next_anchor(
        in_left=not shared_in_left,
        start_y=body_anchor.y0,
        ignored_block=body_anchor,
    )
    if not shared_content or not body_content:
        return False

    pad_x = max(page_width * 0.012, margin * 2)
    pad_bottom = max(page_height * 0.035, margin * 3)
    combined_content = [*shared_content, *body_content]
    combined_bbox = (
        max(0.0, min(block.x0 for block in combined_content) - pad_x),
        max(0.0, min(shared_block.y0, body_anchor.y0) - margin),
        min(page_width, max(block.x1 for block in combined_content) + pad_x),
        min(page_height, max(block.y1 for block in combined_content) + pad_bottom),
    )
    body_bbox = (
        max(0.0, min(block.x0 for block in body_content) - pad_x),
        max(0.0, body_anchor.y0 - margin),
        min(page_width, max(block.x1 for block in body_content) + pad_x),
        min(page_height, max(block.y1 for block in body_content) + pad_bottom),
    )

    region.body_bbox = body_bbox
    region.context_bbox = combined_bbox
    region.set_display_bbox(combined_bbox)
    region.audit_bbox = combined_bbox
    region.semantic_flags = tuple(sorted({
        *region.semantic_flags,
        "shared_context",
        "shared_context_cross_column",
        "shared_context_first",
    }))
    return True


def _expand_shared_range_regions(
    regions: List[QuestionRegion],
    text_blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
    mid_x: float,
    margin: float,
) -> None:
    """[9,10] 같은 공통 자료 묶음의 display/body/context 역할을 분리한다."""
    if not regions:
        return

    for block in text_blocks:
        shared_range = _extract_shared_question_range(block.text)
        if not shared_range:
            continue
        start, end = shared_range
        group = [r for r in regions if start <= r.number <= end]
        if len(group) < 2:
            _expand_singleton_cross_column_shared_range(
                regions,
                text_blocks,
                shared_block=block,
                start=start,
                page_width=page_width,
                page_height=page_height,
                mid_x=mid_x,
                margin=margin,
            )
            continue

        block_in_left = ((block.x0 + block.x1) / 2) < mid_x

        def _same_column(region: QuestionRegion) -> bool:
            rx0, _, rx1, _ = region.bbox
            center = (rx0 + rx1) / 2
            return (center < mid_x) == block_in_left

        group = [r for r in group if _same_column(r)]
        if len(group) < 2:
            continue

        y0 = max(0.0, block.y0 - margin)
        first_group_number = min(r.number for r in group)
        group_body_top = min((r.body_bbox or r.bbox)[1] for r in group)
        shared_text_parts = [block.text or ""]
        for other in text_blocks:
            if other is block:
                continue
            other_center = (other.x0 + other.x1) / 2
            if (other_center < mid_x) != block_in_left:
                continue
            if not (block.y0 < other.y0 < group_body_top):
                continue
            shared_text_parts.append(other.text or "")
        shared_instruction_text = " ".join(shared_text_parts)
        group_bottom = min(
            page_height,
            max(y0 + 10, max(r.bbox[3] for r in group)),
        )
        duplicate_full_group = not _shared_range_uses_answer_instruction(shared_instruction_text)

        for r in group:
            rx0, _, rx1, original_y1 = r.bbox
            body_bbox = r.body_bbox or r.bbox
            if duplicate_full_group:
                display_bbox = (rx0, y0, rx1, group_bottom)
            else:
                display_bbox = (rx0, y0, rx1, min(page_height, max(y0 + 10, original_y1)))
            context_bbox = (rx0, y0, rx1, group_bottom)
            r.body_bbox = body_bbox
            r.context_bbox = context_bbox
            shared_position_flag = (
                "shared_context_first"
                if r.number == first_group_number
                else "shared_context_later"
            )
            r.semantic_flags = tuple(sorted({
                *r.semantic_flags,
                "shared_context",
                shared_position_flag,
            }))
            r.set_display_bbox(display_bbox)
            if duplicate_full_group or r.number == first_group_number:
                r.audit_bbox = display_bbox


def _collapse_cross_column_shared_instruction_regions(
    regions: List[QuestionRegion],
    text_blocks: List[TextBlock],
    *,
    page_width: float,
    page_height: float,
    mid_x: float,
    margin: float,
) -> List[QuestionRegion]:
    """Collapse workbook pages where one shared prompt owns opposite-column answers.

    Some tenant-2 workbooks place a full experiment/data passage in the left
    column as ``[1~3] ... 물음에 답하시오`` and the short answer prompts 1/2/3
    in the right column. Cropping the right prompts independently loses the
    actual problem; duplicating three full-page crops also overstates the
    product unit. Treat this layout as one physical question group.
    """
    if len(regions) < 2 or page_width <= 0 or page_height <= 0:
        return regions

    collapsed_ids: set[int] = set()
    collapsed: list[QuestionRegion] = []

    def _block_in_left(block: TextBlock) -> bool:
        return ((block.x0 + block.x1) / 2) < mid_x

    def _region_in_left(region: QuestionRegion) -> bool:
        rx0, _, rx1, _ = region.body_bbox or region.bbox
        return ((rx0 + rx1) / 2) < mid_x

    def _same_column_block(block: TextBlock, in_left: bool) -> bool:
        return _block_in_left(block) == in_left

    for block in text_blocks:
        shared_range = _extract_shared_question_range(block.text)
        if not shared_range:
            continue
        start, end = shared_range
        group = [r for r in regions if start <= r.number <= end]
        if len(group) < 2:
            continue

        shared_in_left = _block_in_left(block)
        opposite_group = [r for r in group if _region_in_left(r) != shared_in_left]
        if len(opposite_group) < 2:
            continue

        group_numbers = {r.number for r in opposite_group}
        if not group_numbers.issubset(set(range(start, end + 1))):
            continue
        if not all("written_response" in set(r.semantic_flags) for r in opposite_group):
            continue

        shared_text_parts = [block.text or ""]
        answer_top = min((r.body_bbox or r.bbox)[1] for r in opposite_group)
        answer_bottom = max((r.body_bbox or r.bbox)[3] for r in opposite_group)
        for other in text_blocks:
            if other is block:
                continue
            if not _same_column_block(other, shared_in_left):
                continue
            if block.y0 <= other.y0 <= max(answer_bottom, page_height * 0.88):
                shared_text_parts.append(other.text or "")
        if not _shared_range_uses_answer_instruction(" ".join(shared_text_parts)):
            continue

        content_blocks = [
            other for other in text_blocks
            if not _looks_like_footer_folio(
                other,
                page_width=page_width,
                page_height=page_height,
            )
            and other.y0 >= block.y0 - margin
            and other.y0 <= max(answer_bottom, page_height * 0.88)
            and (
                _same_column_block(other, shared_in_left)
                or (
                    not _same_column_block(other, shared_in_left)
                    and answer_top - page_height * 0.04 <= other.y0 <= answer_bottom + page_height * 0.04
                )
            )
        ]
        if not content_blocks:
            continue

        pad_x = max(page_width * 0.012, margin * 2)
        pad_bottom = max(page_height * 0.035, margin * 3)
        x0 = max(0.0, min(b.x0 for b in content_blocks) - pad_x)
        y0 = max(0.0, min(block.y0, min(b.y0 for b in content_blocks)) - margin)
        x1 = min(page_width, max(b.x1 for b in content_blocks) + pad_x)
        y1 = min(page_height, max(b.y1 for b in content_blocks) + pad_bottom)

        collapsed_ids.update(id(r) for r in group)
        collapsed.append(
            QuestionRegion(
                number=start,
                bbox=(x0, y0, x1, y1),
                page_index=opposite_group[0].page_index,
                body_bbox=(x0, y0, x1, y1),
                context_bbox=(x0, y0, x1, y1),
                display_bbox=(x0, y0, x1, y1),
                audit_bbox=(x0, y0, x1, y1),
                semantic_flags=("shared_context", "shared_group", "written_response"),
            )
        )

    if not collapsed:
        return regions
    return [r for r in regions if id(r) not in collapsed_ids] + collapsed


def count_marginal_anchor_candidates(
    text_blocks: List[TextBlock],
    page_width: float,
    page_height: float = 0.0,
) -> int:
    """페이지에서 marginal column 큰 번호 anchor 후보 개수.

    `_pdf_to_images` 의 phase 1 (doc-level workbook 감지) 에서 사용.
    페이지마다 short standalone "N." block (x0 < 15% page width, text ≤ 5 char)
    개수를 센다. 모든 페이지에 marginal block 이 분포하면 워크북 doc.
    """
    if not text_blocks or page_width <= 0:
        return 0
    threshold_x = page_width * 0.15
    footer_y = page_height * 0.92 if page_height > 0 else None
    count = 0
    for b in text_blocks:
        if _looks_like_footer_folio(b, page_width=page_width, page_height=page_height):
            continue
        if footer_y is not None and b.y0 >= footer_y:
            continue
        if b.x0 >= threshold_x:
            continue
        if _extract_marginal_question_number(b.text) is not None:
            count += 1
    return count


def split_questions(
    text_blocks: List[TextBlock],
    page_width: float,
    page_height: float,
    page_index: int = 0,
    paper_type: Optional["PaperTypeResult"] = None,
    prefer_marginal: bool = False,
) -> List[QuestionRegion]:
    """Split a page into question regions based on detected question numbers.

    Args:
        text_blocks: Text blocks extracted from the page with positions.
        page_width: Page width in points.
        page_height: Page height in points.
        page_index: Index of the page in the PDF.
        paper_type: Optional explicit layout decision (paper_type.PaperTypeResult).
                    If provided, the dual/quad detection heuristics are bypassed
                    and the explicit layout is used. None → fall back to the
                    in-module heuristics for backward compatibility.
        prefer_marginal: 워크북/메인자료 doc-level 신호. True 면 페이지에 marginal
                    column 큰 번호 standalone "N." block 이 있으면 그것만 anchor 로 사용
                    (본문 sub-item anchor 들 reject). 학원장 mental model "한 문제 =
                    Q3 (그림+설명+sub-items 통째)" 정합. False (시험지) 면 marginal
                    1개 이상이면 marginal 만 사용 (보수적 임계), 0~1 개면 body 사용.

    Returns:
        List of QuestionRegion sorted by question number.
    """
    if not text_blocks:
        return []

    text_blocks = _expand_inline_anchor_blocks(text_blocks)

    # paper_type이 명시되면 휴리스틱 우회. NON_QUESTION이면 빈 리스트 반환.
    if paper_type is not None:
        if paper_type.is_non_question:
            return []
        debug = getattr(paper_type, "debug", {}) or {}
        pixel_only_dual = (
            bool(getattr(paper_type, "has_embedded_text", False))
            and bool(getattr(paper_type, "is_dual_column", False))
            and debug.get("is_dual_text") is False
            and bool(debug.get("is_dual_pixel"))
        )
        bilateral_marginal_dual = pixel_only_dual and _has_bilateral_marginal_anchors(
            text_blocks,
            page_width=page_width,
            page_height=page_height,
        )
        bilateral_body_dual = (
            pixel_only_dual
            and not bilateral_marginal_dual
            and _has_bilateral_top_level_question_anchors(
                text_blocks,
                page_width=page_width,
                page_height=page_height,
            )
        )
        is_quad_layout = paper_type.is_quadrant
        is_dual_column = (
            paper_type.is_dual_column
            and not is_quad_layout
            and (not pixel_only_dual or bilateral_marginal_dual or bilateral_body_dual)
        )
    else:
        # 4분할 레이아웃 우선 검사 — 가로 + 세로 gutter가 모두 있으면 quad.
        # 4분할이면 dual column 분기와 다른 좌표 구속 필요.
        is_quad_layout = _detect_quad_layout(text_blocks, page_width, page_height)
        is_dual_column = (not is_quad_layout) and _detect_column_layout(text_blocks, page_width)

    if (
        paper_type is not None
        and bool(getattr(paper_type, "has_embedded_text", False))
    ):
        from academy.domain.tools.clean_pdf_question_splitter_v2 import (
            split_clean_pdf_questions_v2,
        )

        clean_pdf_result = split_clean_pdf_questions_v2(
            text_blocks,
            page_width=page_width,
            page_height=page_height,
            is_dual_hint=is_dual_column,
            is_quadrant_hint=is_quad_layout,
            prefer_marginal=prefer_marginal,
        )
        if clean_pdf_result.handled:
            return [
                QuestionRegion(
                    number=region.number,
                    bbox=region.bbox,
                    page_index=page_index,
                    semantic_flags=region.semantic_flags,
                )
                for region in clean_pdf_result.regions
            ]

    if _looks_like_dense_fill_in_row_page(
        text_blocks,
        page_width=page_width,
        page_height=page_height,
    ) and not _has_dense_numbered_subrows_under_main_stem(
        text_blocks,
        page_width=page_width,
        page_height=page_height,
    ):
        dense_row_regions = _build_dense_fill_in_row_regions(
            text_blocks,
            page_width=page_width,
            page_height=page_height,
            page_index=page_index,
        )
        if dense_row_regions:
            return dense_row_regions

    mid_x = page_width * 0.5
    mid_y = page_height * 0.5

    # layout strategy 결정 — 정렬·x/y 경계·후처리 클램프를 strategy에 위임.
    from academy.domain.tools.region_splitters import get_strategy_by_layout_flags

    strategy = get_strategy_by_layout_flags(
        is_quad=is_quad_layout, is_dual=is_dual_column,
    )

    sorted_blocks = strategy.sort_blocks(text_blocks, mid_x, mid_y)

    # Find question start positions — marginal/body candidates 구분 수집.
    # marginal = 페이지 좌측 marginal column 의 짧은 standalone "N." block.
    #           워크북/메인자료 의 "큰 번호" main question anchor.
    # body = "1. 다음 글은..." 처럼 anchor 뒤 문장이 이어지는 일반 시험지 anchor.
    marginal_threshold_x = page_width * 0.15

    def _is_marginal_position(block: TextBlock) -> bool:
        """문항 큰 번호가 자기 column의 왼쪽 margin에 있는지 판정한다.

        기존 판정은 page-left margin만 marginal로 보아 2단 워크북의 우측 column
        큰 번호(Q3/Q4 등)를 전부 body anchor로 밀어냈다. marginal-only 모드에서는
        그 결과 우측 column 전체가 누락되므로, dual/quad layout에서는 우측 column의
        local-left margin도 같은 신호로 인정한다. PyMuPDF block x0가 gutter를
        몇 pt 침범하는 경우가 있어 center가 우측 column이면 x0 살짝 좌측 진입을
        허용한다.
        """
        if page_height > 0 and block.y0 >= page_height * 0.92:
            return False
        if block.x0 < marginal_threshold_x:
            return True
        if (
            (is_dual_column or is_quad_layout)
            and ((block.x0 + block.x1) / 2) >= mid_x
            and block.x0 < (mid_x + marginal_threshold_x)
        ):
            return True
        return False

    def _same_coarse_cell(left: TextBlock, right: TextBlock) -> bool:
        if not (is_dual_column or is_quad_layout):
            return True
        same_cell = (
            ((left.x0 + left.x1) / 2 < mid_x)
            == ((right.x0 + right.x1) / 2 < mid_x)
        )
        if is_quad_layout:
            same_cell = same_cell and (
                ((left.y0 + left.y1) / 2 < mid_y)
                == ((right.y0 + right.y1) / 2 < mid_y)
            )
        return same_cell

    def _nearby_section_offset_for_marginal(block_idx: int) -> int | None:
        """PyMuPDF가 "서답형 6."을 "6." + "서답형..."로 쪼갠 경우 보정."""
        block = sorted_blocks[block_idx]
        line_tolerance = max(8.0, page_height * 0.025)
        for other_idx, other in enumerate(sorted_blocks):
            if other_idx == block_idx:
                continue
            offset = _section_offset_from_text(other.text)
            if offset is None:
                continue
            if not _same_coarse_cell(block, other):
                continue
            overlaps_y = block.y0 <= other.y1 + 1.0 and other.y0 <= block.y1 + 1.0
            same_line = abs(block.y0 - other.y0) <= line_tolerance
            if overlaps_y or same_line:
                return offset
        return None

    def _marginal_candidate_has_question_body(block_idx: int) -> bool:
        """Reject standalone chart/page numbers that look like marginal anchors.

        Real workbook marginal anchors are either extracted with the stem in the
        same block ("10.\n다음은...") or have stem text immediately beside/below
        the large number. Axis tick labels such as "40" / "80" do not.
        """
        block = sorted_blocks[block_idx]
        text = block.text or ""
        tail = "\n".join(text.strip().splitlines()[1:]).strip()
        if re.search(r"[가-힣A-Za-z]{3,}", tail):
            return True
        if re.search(r"(?:[.)]|[~∼])", text) and re.search(r"[가-힣A-Za-z]{3,}", text):
            return True

        line_tolerance = max(10.0, page_height * 0.045)
        for other_idx, other in enumerate(sorted_blocks):
            if other_idx == block_idx:
                continue
            if not _same_coarse_cell(block, other):
                continue
            if other.y0 < block.y0 - 2.0:
                continue
            if other.y0 - block.y0 > line_tolerance:
                continue
            if other.x1 < block.x0 - page_width * 0.03:
                continue
            if re.search(r"[가-힣A-Za-z]{4,}", other.text or ""):
                return True
        return False

    candidates: List[Tuple[int, int, bool]] = []  # (qnum, block_idx, is_marginal)
    for idx, block in enumerate(sorted_blocks):
        if _looks_like_footer_folio(
            block,
            page_width=page_width,
            page_height=page_height,
        ):
            continue
        # marginal candidate 우선 검사 (짧은 standalone block).
        marginal_num = _extract_marginal_question_number(block.text)
        if (
            marginal_num is not None
            and _is_marginal_position(block)
        ):
            if (
                not re.search(r"[.)]", block.text or "")
                and not _marginal_candidate_has_question_body(idx)
            ):
                continue
            section_offset = _nearby_section_offset_for_marginal(idx)
            if section_offset is not None:
                marginal_num += section_offset
            candidates.append((marginal_num, idx, True))
            continue
        # body anchor (current regex).
        body_num = _extract_question_number(block.text)
        if body_num is not None:
            candidates.append((body_num, idx, False))

    if not candidates:
        return []

    def _looks_like_exam_header_page_number(block: TextBlock) -> bool:
        stripped = (block.text or "").strip()
        first_line = stripped.split("\n", 1)[0].strip()
        if not re.fullmatch(r"\d{1,3}\.?", first_line):
            return False
        if page_height <= 0 or block.y1 > page_height * 0.13:
            return False
        top_text = " ".join(
            b.text or "" for b in sorted_blocks
            if b.y1 <= page_height * 0.24
        )
        header_text = f"{top_text} {stripped}"
        return bool(
            re.search(
                r"(?:문제지|제\s*\d+\s*교시|수험\s*번호|성\s*명|문항\s*번호|총\s*면수|학년|고사일|중간\s*고사|배점)",
                header_text,
            )
        )

    if len(candidates) > 1:
        candidates = [
            c for c in candidates
            if not (c[2] and _looks_like_exam_header_page_number(sorted_blocks[c[1]]))
        ]
        if not candidates:
            return []

    def _same_layout_cell(left_idx: int, right_idx: int) -> bool:
        left = sorted_blocks[left_idx]
        right = sorted_blocks[right_idx]
        if not (is_dual_column or is_quad_layout):
            return True
        same_cell = (
            ((left.x0 + left.x1) / 2 < mid_x)
            == ((right.x0 + right.x1) / 2 < mid_x)
        )
        if is_quad_layout:
            same_cell = same_cell and (
                ((left.y0 + left.y1) / 2 < mid_y)
                == ((right.y0 + right.y1) / 2 < mid_y)
            )
        return same_cell

    def _is_parenthesized_body_subitem(candidate: Tuple[int, int, bool]) -> bool:
        qnum, idx, is_marginal = candidate
        if is_marginal:
            return False
        block = sorted_blocks[idx]
        if not re.match(r"^\s*\(\d{1,3}\)", (block.text or "").strip()):
            return False
        for other_num, other_idx, other_is_marginal in candidates:
            if other_idx == idx:
                continue
            other_block = sorted_blocks[other_idx]
            if other_block.y0 >= block.y0:
                continue
            if not _same_layout_cell(other_idx, idx):
                continue
            if re.match(r"^\s*\(\d{1,3}\)", (other_block.text or "").strip()):
                continue
            return True
        return False

    if len(candidates) > 1:
        candidates = [
            c for c in candidates
            if not _is_parenthesized_body_subitem(c)
        ]

    def _candidate_has_dense_subrows(candidate: Tuple[int, int, bool]) -> bool:
        qnum, idx, _ = candidate
        block = sorted_blocks[idx]
        if not _looks_like_dense_owner_question_stem(block.text):
            return False
        lower_rows: list[int] = []
        lower_row_texts: list[str] = []
        for other_num, other_idx, other_is_marginal in candidates:
            if other_idx == idx or other_is_marginal:
                continue
            other_block = sorted_blocks[other_idx]
            if other_block.y0 <= block.y0:
                continue
            if not _same_layout_cell(idx, other_idx):
                continue
            if not _looks_like_plain_numbered_subrow(other_block.text):
                continue
            lower_rows.append(other_num)
            lower_row_texts.append(other_block.text)
        if len(lower_rows) < 5:
            return False
        if qnum >= 10:
            return True
        if qnum in lower_rows:
            return True
        if not _looks_like_low_numbered_subrow_run(lower_rows):
            return False
        return _mentions_large_visual_context(block.text) or _looks_like_ox_marking_cluster(
            block.text,
            lower_row_texts,
        )

    scan_continuous = _is_continuous_scan_type(paper_type)

    dense_owner_indices = {
        idx
        for candidate in candidates
        for _, idx, _ in [candidate]
        if _candidate_has_dense_subrows(candidate)
    }
    if dense_owner_indices:
        filtered_candidates = []
        for candidate in candidates:
            qnum, idx, is_marginal = candidate
            block = sorted_blocks[idx]
            drop_as_dense_subrow = False
            if not is_marginal and _looks_like_plain_numbered_subrow(block.text):
                if scan_continuous and qnum >= 10:
                    filtered_candidates.append(candidate)
                    continue
                for owner_idx in dense_owner_indices:
                    owner_block = sorted_blocks[owner_idx]
                    if (
                        idx != owner_idx
                        and block.y0 > owner_block.y0
                        and _same_layout_cell(owner_idx, idx)
                    ):
                        drop_as_dense_subrow = True
                        break
            if not drop_as_dense_subrow:
                filtered_candidates.append(candidate)
        candidates = filtered_candidates

    main_anchor_candidates = [
        (qnum, idx)
        for qnum, idx, is_marginal in candidates
        if not is_marginal
        and (
            _SOURCE_PREFIXED_QUESTION_PATTERN.match(sorted_blocks[idx].text.strip())
            or _section_offset_from_text(sorted_blocks[idx].text) is not None
            or (
                qnum >= 10
                and not re.match(r"^\s*\(\d{1,3}\)", sorted_blocks[idx].text.strip())
            )
        )
    ]
    if main_anchor_candidates:
        filtered_candidates: List[Tuple[int, int, bool]] = []
        for candidate in candidates:
            qnum, idx, is_marginal = candidate
            block = sorted_blocks[idx]
            is_parenthesized_subitem = bool(
                re.match(r"^\s*\(\d{1,3}\)", block.text.strip())
            )
            drop_as_subitem = False
            is_plain_low_subitem = bool(
                not is_marginal
                and qnum <= 4
                and re.match(r"^\s*\d{1,2}\s*[.)]", block.text.strip())
            )
            for main_num, main_idx in main_anchor_candidates:
                main_block = sorted_blocks[main_idx]
                if not (
                    qnum < main_num
                    and block.y0 > main_block.y0
                    and _same_layout_cell(main_idx, idx)
                ):
                    continue
                if is_parenthesized_subitem or (
                    is_plain_low_subitem and main_num >= 10
                ):
                    drop_as_subitem = True
                    break
            if not drop_as_subitem:
                filtered_candidates.append(candidate)
        candidates = filtered_candidates

    # Marginal preference 분기:
    # - prefer_marginal=True (워크북 doc-level 신호): marginal candidate 1+ 면 마진만.
    #   페이지에 main question 1 개 (Q1) 만 있는 케이스도 학원장 mental model 의
    #   "한 문제" 단위 = marginal anchor.
    # - prefer_marginal=False (시험지): marginal 2+ 인 경우만 마진 preference (보수적
    #   임계). 시험지에 standalone "N." block 우연히 1개 있는 경우 본문 anchor 도 사용.
    marginal_count = sum(1 for _, _, m in candidates if m)
    body_count = len(candidates) - marginal_count
    if prefer_marginal and marginal_count >= 1:
        marginal_columns: set[tuple[int, int]] = set()

        def _candidate_column(block_idx: int) -> tuple[int, int]:
            block = sorted_blocks[block_idx]
            center_x = (block.x0 + block.x1) / 2
            center_y = (block.y0 + block.y1) / 2
            x_col = 1 if (is_dual_column or is_quad_layout) and center_x >= mid_x else 0
            y_col = 1 if is_quad_layout and center_y >= mid_y else 0
            return x_col, y_col

        for _, idx, is_marginal in candidates:
            if is_marginal:
                marginal_columns.add(_candidate_column(idx))
        marginal_numbers = {
            qnum for qnum, _, is_marginal in candidates if is_marginal
        }
        shared_material_columns: List[tuple[tuple[int, int], float]] = []
        for idx, block in enumerate(sorted_blocks):
            shared_range = _extract_shared_question_range(block.text)
            if not shared_range:
                continue
            start, end = shared_range
            if marginal_numbers & set(range(start, end + 1)):
                shared_material_columns.append((_candidate_column(idx), block.y0))

        def _is_source_prefixed_body_anchor(candidate: Tuple[int, int, bool]) -> bool:
            if candidate[2]:
                return False
            block = sorted_blocks[candidate[1]]
            if _extract_shared_question_range(block.text):
                return False
            return bool(_SOURCE_PREFIXED_QUESTION_PATTERN.match(block.text.strip()))

        def _is_shared_material_body_anchor(candidate: Tuple[int, int, bool]) -> bool:
            if candidate[2] or not shared_material_columns:
                return False
            block = sorted_blocks[candidate[1]]
            block_col = _candidate_column(candidate[1])
            return any(
                block_col == shared_col and block.y0 >= shared_y - 2.0
                for shared_col, shared_y in shared_material_columns
            )

        def _keep_prefer_marginal_candidate(
            candidate: Tuple[int, int, bool],
        ) -> bool:
            if candidate[2]:
                return True
            if _is_source_prefixed_body_anchor(candidate):
                return True
            if candidate[0] in marginal_numbers:
                return False
            if _is_shared_material_body_anchor(candidate):
                return False
            return (
                (is_dual_column or is_quad_layout)
                and _candidate_column(candidate[1]) not in marginal_columns
            )

        candidates = [c for c in candidates if _keep_prefer_marginal_candidate(c)]
    elif (
        not scan_continuous
        and marginal_count >= 2
        and body_count == 0
    ):
        candidates = [c for c in candidates if c[2]]

    if scan_continuous and not prefer_marginal and not is_quad_layout:
        candidates = _filter_continuous_anchor_sequence(candidates)

    if is_dual_column and candidates:
        anchor_blocks = [sorted_blocks[idx] for _, idx, _ in candidates]
        right_anchor_count = sum(
            1 for block in anchor_blocks
            if (
                ((block.x0 + block.x1) / 2) >= mid_x
                and block.x0 >= (mid_x - marginal_threshold_x)
            )
        )
        top_anchor_y = min(b.y0 for b in anchor_blocks)
        wide_layout_margin = 2.0
        content_reaches_right = any(
            block.x1 >= page_width * 0.75
            and block.y0 >= top_anchor_y - wide_layout_margin
            and block.y0 <= page_height * 0.90
            for block in text_blocks
        )
        if right_anchor_count == 0 and content_reaches_right:
            is_dual_column = False
            strategy = get_strategy_by_layout_flags(
                is_quad=is_quad_layout, is_dual=False,
            )

    question_starts: List[Tuple[int, int]] = [(qn, ix) for qn, ix, _ in candidates]

    # 페이지 내 중복 번호 제거. 본문 내 "그림 4는..." 같은 표현이 regex와
    # 매치되는 경우, layout 순서상 먼저 나온 것을 실제 문항 앵커로 간주.
    seen: dict[int, int] = {}
    deduped: List[Tuple[int, int]] = []
    for qnum, idx in question_starts:
        if qnum in seen:
            continue
        seen[qnum] = idx
        deduped.append((qnum, idx))
    question_starts = deduped

    # Build regions: each question spans from its start to the next question start
    regions: List[QuestionRegion] = []
    margin = 2.0  # small margin in points

    def _prefix_idx_for_start(start_idx: int) -> Optional[int]:
        if start_idx <= 0:
            return None
        start_block = sorted_blocks[start_idx]
        for prev_idx in range(start_idx - 1, max(-1, start_idx - 8), -1):
            prev_block = sorted_blocks[prev_idx]
            if not _same_layout_cell(prev_idx, start_idx):
                continue
            if not (0 <= start_block.y0 - prev_block.y1 <= page_height * 0.08):
                continue
            if (
                _looks_like_source_prefix_only(prev_block.text)
                or _looks_like_question_type_prefix_only(prev_block.text)
                or _looks_like_section_title_prefix_only(prev_block.text)
            ):
                return prev_idx
        return None

    def _allow_short_region_until_next_anchor(
        start_idx: int,
        next_start_idx: int | None,
        *,
        y0: float,
        y1: float,
    ) -> bool:
        if next_start_idx is None:
            return False
        if len(question_starts) < 5:
            return False
        if not _same_layout_cell(start_idx, next_start_idx):
            return False
        if y1 <= y0 + max(10.0, page_height * 0.012):
            return False
        return _looks_like_short_workbook_prompt(sorted_blocks[start_idx].text)

    for i, (qnum, start_idx) in enumerate(question_starts):
        start_block = sorted_blocks[start_idx]
        next_start_idx = question_starts[i + 1][1] if i + 1 < len(question_starts) else None
        next_block = sorted_blocks[next_start_idx] if next_start_idx is not None else None

        # Defensive: region_blocks 비면 skip (sort 결과 inconsistency 방어)
        next_prefix_idx = (
            _prefix_idx_for_start(next_start_idx)
            if next_start_idx is not None
            else None
        )
        end_idx = next_prefix_idx if next_prefix_idx is not None else (
            next_start_idx if next_start_idx is not None else len(sorted_blocks)
        )
        if not sorted_blocks[start_idx:end_idx]:
            continue

        # Strategy 호출: x range / y end 계산
        x0, x1 = strategy.compute_x_range(start_block, page_width, mid_x, margin)
        y0 = max(0, start_block.y0 - margin)
        prefix_idx = _prefix_idx_for_start(start_idx)
        if prefix_idx is not None:
            y0 = max(0.0, sorted_blocks[prefix_idx].y0 - margin)
        y1 = strategy.compute_y_end(
            start_block, next_block,
            page_width, page_height, mid_x, mid_y, margin,
        )
        if next_prefix_idx is not None:
            y1 = min(y1, sorted_blocks[next_prefix_idx].y0 - margin)

        y1 = min(page_height, max(y1, y0 + 10))

        # Strip 절대 차단: height < 페이지의 5% (시험지 problem의 합리적 최소 크기)는
        # OCR false anchor 또는 cross-column anchor sort 오류의 흔적. strategy의
        # compute_y_end fallback이 miss한 경우의 마지막 보루.
        # T2 운영 reanalyze (2026-04-30): 11건 strip 잔존 (h=10~100). 임계 100px도
        # doc#177 q1 (정확히 100px, h=y1-y0이 page_height의 0.9%)에서 통과되어
        # 5% 비율 임계로 변경. 11200 * 0.05 = 560px 최소.
        allow_short_until_next = _allow_short_region_until_next_anchor(
            start_idx,
            next_start_idx,
            y0=y0,
            y1=y1,
        )
        text_start_idx = prefix_idx if prefix_idx is not None else start_idx
        region_text = " ".join(
            block.text or ""
            for block in sorted_blocks[text_start_idx:end_idx]
        )
        start_block_text = start_block.text or ""
        allow_short_content = allow_short_until_next or (
            next_start_idx is None
            and not (is_dual_column or is_quad_layout)
            and _looks_like_short_workbook_prompt(start_block_text)
            and not _mentions_visual_context(region_text)
        )
        response_min_height_ratio = None
        is_written_response = _looks_like_written_response_prompt(region_text)
        mentions_large_visual = _mentions_large_visual_context(region_text)
        if (
            (is_written_response or _looks_like_short_workbook_prompt(region_text))
            and not mentions_large_visual
        ):
            allow_short_content = True
            next_gap = (
                sorted_blocks[next_start_idx].y0 - start_block.y0
                if next_start_idx is not None
                else page_height
            )
            if (
                not (is_dual_column or is_quad_layout)
                and next_gap >= page_height * 0.10
            ):
                y0 = max(0.0, y0 - page_height * 0.012)
        if is_written_response and mentions_large_visual:
            response_min_height_ratio = 0.22
        if (
            y1 - y0 < page_height * 0.05
            and not allow_short_until_next
        ):
            y1 = page_height

        # Strategy post-clamp: quad는 quadrant 경계 / dual은 column 경계 추가 구속.
        x0, x1 = strategy.post_clamp_x(start_block, x0, x1, page_width, mid_x, margin)
        y0, y1 = strategy.post_clamp_y(start_block, y0, y1, page_height, mid_y, margin)
        tighten_to_content = (
            paper_type is None
            or bool(getattr(paper_type, "has_embedded_text", True))
        )
        if tighten_to_content:
            content_blocks = (
                text_blocks
                if getattr(strategy, "name", "") == "single"
                else sorted_blocks[start_idx:end_idx]
            )
            x0, y0, x1, y1 = _tighten_region_to_content(
                blocks=content_blocks,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                page_width=page_width,
                page_height=page_height,
                margin=margin,
                allow_short_content=allow_short_content,
                min_height_ratio=response_min_height_ratio,
                allow_visual_context_min=mentions_large_visual,
            )

        semantic_flags = set()
        if _mentions_large_visual_context(region_text):
            semantic_flags.add("visual_context")
        if _looks_like_written_response_prompt(region_text):
            semantic_flags.add("written_response")
        if _looks_like_reasoning_response_prompt(region_text):
            semantic_flags.add("reasoning_response")
        if _looks_like_short_workbook_prompt(region_text):
            semantic_flags.add("short_workbook_prompt")
        if _references_prior_context(region_text):
            semantic_flags.add("references_prior_context")

        regions.append(
            QuestionRegion(
                number=qnum,
                bbox=(x0, y0, x1, y1),
                page_index=page_index,
                semantic_flags=tuple(sorted(semantic_flags)),
            )
        )

    _expand_shared_range_regions(
        regions,
        sorted_blocks,
        page_width=page_width,
        page_height=page_height,
        mid_x=mid_x,
        margin=margin,
    )
    regions = _collapse_cross_column_shared_instruction_regions(
        regions,
        sorted_blocks,
        page_width=page_width,
        page_height=page_height,
        mid_x=mid_x,
        margin=margin,
    )

    # Sort by question number, fallback to layout order
    regions.sort(key=lambda r: r.number)

    return regions


def _detect_per_page_restart(
    regions_per_page: List[List["QuestionRegion"]],
) -> bool:
    """페이지별/섹션별 anchor 번호 리셋 패턴 감지 (global dedup 끄기 여부).

    Why: 시험지(continuous numbering)는 cross-page dedup 이 false anchor("그림 4는") 를 잘 거른다.
    하지만 페이지/섹션별 anchor 번호가 리셋되는 doc 들 (워크북·메인자료·여러 섹션 모아둔 자료) 에서는
    같은 dedup 이 후속 페이지 anchor 를 전부 drop 시켜 catastrophic under-cut 을 일으킨다
    (T2 운영 1 doc 단위 81% drop 실측, 24% clean_pdf_dual 학원장 manual_create 의 본질).

    감지 대상:
      A. 페이지별 리셋 워크북 (지권의 변화 메인자료) — 같은 1, 2, 3 시퀀스가 N 페이지에 반복.
      B. 섹션별 리셋 / 멀티-섹션 자료 (빅뱅 복습과제) — 큰 번호 범위가 두 번 이상 등장.
      C. 일반 시험지 false anchor (개포고 시험지 마지막 2 페이지에 "1.","2.","3." OCR 오탐) — 회피.

    Heuristic (3-신호 OR):
      1. anchor 가 있는 페이지가 5+ 개여야 함 (너무 작은 doc 은 시험지 protect).
      2. **신호 A (페이지 리셋)**: anchor 1, 2, 3 중 하나라도 포함된 페이지가
         (a) 절대 ≥ 5 페이지 AND (b) anchor 보유 페이지의 40% 이상.
      3. **신호 B (섹션 리셋)**: 2 페이지 이상에 걸쳐 등장하는 anchor 가
         (a) 절대 ≥ 5 개 AND (b) 전체 unique anchor 의 30% 이상.

    A 또는 B 하나라도 True 면 per-page-restart 로 간주 (global dedup off).
    개포고 시험지: pages_with_low=3 (5 미만) AND repeated=3 (5 미만) → A,B 모두 False → continuous 유지.
    """
    pages_with_anchors = sum(1 for p in regions_per_page if p)
    if pages_with_anchors < 5:
        return False

    # 신호 A — low anchor 가 많은 페이지에 분산 (페이지 리셋)
    pages_with_low = 0
    pages_per_number: dict[int, int] = {}
    for page_regions in regions_per_page:
        nums = {r.number for r in page_regions}
        if nums & {1, 2, 3}:
            pages_with_low += 1
        for n in nums:
            pages_per_number[n] = pages_per_number.get(n, 0) + 1

    threshold_low = max(5, int(pages_with_anchors * 0.4))
    signal_a = pages_with_low >= threshold_low

    # 신호 B — multi-page anchor 비율이 높음 (섹션 리셋)
    repeated = sum(1 for cnt in pages_per_number.values() if cnt >= 2)
    unique = len(pages_per_number)
    signal_b = (
        repeated >= 5
        and unique > 0
        and (repeated / unique) >= 0.3
    )

    return signal_a or signal_b


def _page_can_continue_late_restart(
    nums: set[int],
    *,
    active_max: int,
) -> bool:
    """후반부 섹션 리셋이 다음 페이지로 자연스럽게 이어지는지 판단."""
    if not nums:
        return False
    page_min = min(nums)
    page_max = max(nums)
    allowed_jump = max(active_max + 20, 30)
    if page_max > allowed_jump:
        return False
    has_expected_next = any(
        n in nums for n in (active_max + 1, active_max + 2, active_max + 3)
    )
    if page_min <= 3:
        return active_max < 8 or has_expected_next
    return page_min <= active_max + 3 or has_expected_next


def _has_late_restart_continuation(
    regions_per_page: List[List["QuestionRegion"]],
    start_index: int,
    current_nums: set[int],
) -> bool:
    """late section restart 후보가 단발 오탐이 아니라 다음 페이지로 이어지는지 확인."""
    active_max = max(current_nums)
    for page_regions in regions_per_page[start_index + 1:start_index + 3]:
        next_nums = {r.number for r in page_regions}
        if not next_nums:
            continue
        return _page_can_continue_late_restart(next_nums, active_max=active_max)
    return False


def _detect_late_section_restart_pages(
    regions_per_page: List[List["QuestionRegion"]],
) -> dict[int, int]:
    """문서 후반부에서 번호가 1부터 다시 시작하는 섹션 page를 찾는다.

    Continuous numbering 자료라도 뒤쪽에 "[언남고 기출] / 1." 같은 별도 섹션이
    붙으면 기존 global dedup 이 1,2,3... 을 이전 문항 중복으로 오인해 전부 버린다.
    다만 시험지 마지막 본문에 보이는 "1, 2, 3" 류 false anchor 는 계속 제거해야 하므로,
    충분히 큰 선행 번호 진행 이후 + 낮은 번호 시작 + 다음 페이지 연속성 조건을 모두 요구한다.
    """
    restart_pages: dict[int, int] = {}
    prior_max = 0
    active_section: int | None = None
    active_max = 0
    section_id = 0

    for page_idx, page_regions in enumerate(regions_per_page):
        nums = {r.number for r in page_regions}

        if active_section is not None:
            if _page_can_continue_late_restart(nums, active_max=active_max):
                restart_pages[page_idx] = active_section
                active_max = max(active_max, max(nums))
                continue
            active_section = None
            active_max = 0

        if nums:
            page_min = min(nums)
            page_max = max(nums)
            has_long_prior_section = prior_max >= 20
            has_completed_short_section = (
                prior_max >= 8
                and page_max <= min(prior_max, 12)
            )
            is_restart_start = (
                (has_long_prior_section or has_completed_short_section)
                and page_min <= 3
                and page_max <= 30
                and _has_late_restart_continuation(
                    regions_per_page,
                    page_idx,
                    nums,
                )
            )
            if is_restart_start:
                section_id += 1
                active_section = section_id
                active_max = page_max
                restart_pages[page_idx] = section_id
                continue
            prior_max = max(prior_max, page_max)

    return restart_pages


def _drop_outliers_in_seen(
    seen_numbers: set[int],
) -> set[int]:
    """sorted-unique number-space 별 sequence outlier 식별.

    100 단위 number-space 분리 (선택형 <100 / 서술형 100~ / 논술형 200~ / 단답 300~).
    median gap 대비 5x + abs >= 5 인 gap 이후 tail 이 짧으면 outlier 로 표시.
    예: [3, 4, 5, 6, 7, 46] → 46 드롭.
    단, [1, 2, 3, 4, 11, 13, 19, 20] 같은 공식 발췌형 sparse
    sequence는 tail anchor가 3개 이상이므로 보존한다.
    """
    outlier_nums: set[int] = set()
    by_space: dict[int, List[int]] = {}
    for n in sorted(seen_numbers):
        space = n // 100
        by_space.setdefault(space, []).append(n)

    for space_nums in by_space.values():
        if len(space_nums) < 4:
            continue
        gaps = [space_nums[i + 1] - space_nums[i] for i in range(len(space_nums) - 1)]
        sorted_gaps = sorted(gaps)
        median_gap = sorted_gaps[len(sorted_gaps) // 2]
        for i, gap in enumerate(gaps):
            if gap >= 5 and gap >= median_gap * 5:
                tail = space_nums[i + 1:]
                if len(tail) >= 3:
                    break
                outlier_nums.update(tail)
                break

    low_space = by_space.get(0, [])
    if len(low_space) >= 3:
        for space, space_nums in by_space.items():
            if space < 3 or len(space_nums) > 2:
                continue
            outlier_nums.update(space_nums)
    return outlier_nums


def _page_outliers_with_dense_leading_continuation_kept(
    page_regions: List["QuestionRegion"],
) -> set[int]:
    page_nums = {r.number for r in page_regions}
    page_outliers = _drop_outliers_in_seen(page_nums)
    if not page_outliers:
        return page_outliers
    has_dense_rows = any(
        "short_workbook_prompt" in set(r.semantic_flags or ())
        for r in page_regions
    )
    if not has_dense_rows:
        return page_outliers

    low_tops = [r.bbox[1] for r in page_regions if r.number <= 3]
    if not low_tops:
        return page_outliers
    first_low_top = min(low_tops)
    leading_continuations = {
        r.number
        for r in page_regions
        if r.number in page_outliers
        and r.number >= 10
        and r.bbox[1] < first_low_top - 1.0
    }
    return page_outliers - leading_continuations


def validate_anchors_across_pages(
    regions_per_page: List[List[QuestionRegion]],
    *,
    force_per_page_restart: bool = False,
) -> List[List[QuestionRegion]]:
    """
    여러 페이지의 anchor를 모아 문서 전역 검증.

    세 모드:
      A. **Continuous numbering (시험지)** — 기본 모드.
         1. 크로스-페이지 중복 → 처음 등장 page 만 유지. 본문 내 "그림 4는" 같은 오탐 제거.
         2. Sequence outlier (median gap × 5 + abs ≥ 5) 드롭.
      B. **Per-page-restart (워크북/메인자료)** — `_detect_per_page_restart` 가 True
         이거나 호출자가 `force_per_page_restart=True` 를 준 때.
         페이지마다 anchor 1, 2, 3... 가 리셋되는 doc 이라 global dedup 이 후속 페이지를 전부
         drop 시키는 결함을 방지. 페이지간 dedup 을 끄고 page-local outlier 만 적용.
      C. **Late section restart** — continuous 자료 뒤쪽에 별도 기출/복습 섹션이 붙어 번호가
         다시 1부터 시작하는 경우. 선행 섹션과는 dedup 하지 않고, 새 섹션 안에서는 dedup 한다.

    입력 형식: [per_page_regions]
    반환: 필터링된 [per_page_regions] (페이지 구조 유지, 내부 regions만 필터)
    """
    if not regions_per_page:
        return regions_per_page

    # ── Per-page-restart 감지 — workbook/메인자료 페이지 리셋 패턴 ──
    is_per_page_restart = force_per_page_restart or _detect_per_page_restart(regions_per_page)

    if is_per_page_restart:
        # 페이지간 dedup OFF — 각 페이지 anchor 그대로 유지.
        # outlier 는 페이지 안에서만 적용 (페이지 내 false anchor 잔존 방지).
        filtered: List[List[QuestionRegion]] = []
        for page_regions in regions_per_page:
            page_outliers = _page_outliers_with_dense_leading_continuation_kept(page_regions)
            filtered.append([r for r in page_regions if r.number not in page_outliers])
        return filtered

    # ── Continuous mode — 기존 시험지 dedup + late section restart 보정 ──
    restart_pages = _detect_late_section_restart_pages(regions_per_page)
    seen_numbers: set[int] = set()
    restart_seen: dict[int, set[int]] = {}
    region_section: dict[tuple[int, int, tuple[float, float, float, float]], int] = {}
    filtered = []
    for page_idx, page_regions in enumerate(regions_per_page):
        kept: List[QuestionRegion] = []
        restart_section = restart_pages.get(page_idx)
        for r in page_regions:
            if restart_section is not None:
                section_seen = restart_seen.setdefault(restart_section, set())
                if r.number in section_seen:
                    continue
                section_seen.add(r.number)
                region_section[(r.page_index, r.number, r.bbox)] = restart_section
            else:
                if r.number in seen_numbers:
                    continue
                seen_numbers.add(r.number)
            kept.append(r)
        filtered.append(kept)

    outlier_nums = _drop_outliers_in_seen(seen_numbers)
    restart_outliers = {
        section: _drop_outliers_in_seen(nums)
        for section, nums in restart_seen.items()
    }
    if outlier_nums:
        filtered = [
            [
                r for r in page_regions
                if not (
                    r.number in outlier_nums
                    and (r.page_index, r.number, r.bbox) not in region_section
                )
            ]
            for page_regions in filtered
        ]
    if restart_outliers:
        filtered = [
            [
                r for r in page_regions
                if r.number not in restart_outliers.get(
                    region_section.get((r.page_index, r.number, r.bbox), -1),
                    set(),
                )
            ]
            for page_regions in filtered
        ]

    return filtered
