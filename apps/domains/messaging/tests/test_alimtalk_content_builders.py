from unittest import TestCase

from apps.domains.messaging.alimtalk_content_builders import (
    build_manual_replacements,
    build_unified_replacements,
    get_provider_template_contract,
    get_solapi_template_id,
    get_template_type,
    get_unified_for_manual_send,
    render_alimtalk_preview_text,
    TYPE_ATTENDANCE,
    TYPE_CLINIC_INFO,
    TYPE_NOTICE_PAYMENT,
    TYPE_NOTICE_WITHDRAWAL,
    TYPE_SCORE,
)


class TestManualSendEnvelopeRouting(TestCase):
    def test_each_explicit_event_uses_only_its_exact_provider_contract(self):
        cases = {
            "lesson_result": TYPE_SCORE,
            "attendance_notice": TYPE_ATTENDANCE,
            "clinic_reservation_notice": TYPE_CLINIC_INFO,
            "clinic_change_notice": "clinic_change",
        }
        for manual_event, expected_type in cases.items():
            with self.subTest(manual_event=manual_event):
                template_type, template_id = get_unified_for_manual_send(manual_event)
                contract = get_provider_template_contract(expected_type)
                self.assertEqual(template_type, expected_type)
                self.assertEqual(template_id, contract["template_id"])

    def test_category_name_and_variables_cannot_infer_an_envelope(self):
        for value in ("grades", "clinic", "payment", "수업 결과 기본형", ""):
            with self.subTest(value=value):
                self.assertEqual(get_unified_for_manual_send(value), (None, None))

    def test_score_provider_contract_matches_approved_snapshot(self):
        contract = get_provider_template_contract(TYPE_SCORE)
        self.assertEqual(contract["template_id"], "KA01TP260406105458211774JKJ3OU55")
        self.assertEqual(contract["template_version"], "Wy7Z91sBXK")
        self.assertEqual(contract["structure_fingerprint"], "54f3fb7aca49daaf")
        self.assertEqual(contract["content_fingerprint"], "a5605726f724dd9b")
        self.assertEqual(contract["header_fingerprint"], "dbaf4d19b3af2b21")
        self.assertEqual(
            contract["variables"],
            ("학원이름", "학생이름", "강의명", "차시명", "선생님메모", "사이트링크"),
        )


class TestCommunityTriggers(TestCase):
    """커뮤니티 답변 알림톡 트리거 — 매핑 부재 시 통합 알림톡 미사용 (옛 score 좀비 fallback 종료)."""

    def test_qna_answered_no_unified_mapping(self):
        # 카카오 검수 통과된 적합 양식이 없음 → 통합 알림톡 비활성
        self.assertIsNone(get_template_type("qna_answered"))
        self.assertIsNone(get_solapi_template_id("qna_answered"))

    def test_counsel_answered_no_unified_mapping(self):
        self.assertIsNone(get_template_type("counsel_answered"))
        self.assertIsNone(get_solapi_template_id("counsel_answered"))

    def test_unmapped_trigger_returns_empty_replacements(self):
        replacements = build_unified_replacements(
            trigger="qna_answered",
            content_body="선생님이 질문에 답변하셨습니다.",
            context={"강의명": "수학"},
            tenant_name="학원플러스",
            student_name="홍길동",
            site_url="https://hakwonplus.com",
        )
        self.assertEqual(replacements, [])


class TestSystemNoticeMappings(TestCase):
    """퇴원/결제 NONE 양식은 고정 본문 시스템 안내로 계속 라우팅한다."""

    def test_withdrawal_complete_uses_withdrawal_notice(self):
        self.assertEqual(
            get_template_type("withdrawal_complete"),
            TYPE_NOTICE_WITHDRAWAL,
        )
        self.assertTrue(bool(get_solapi_template_id("withdrawal_complete")))

    def test_payment_triggers_use_payment_notice(self):
        for trigger in ("payment_complete", "payment_due_days_before"):
            with self.subTest(trigger=trigger):
                self.assertEqual(get_template_type(trigger), TYPE_NOTICE_PAYMENT)
                self.assertFalse(bool(get_solapi_template_id(trigger)))

class TestExamAssignmentEnvelopeMappings(TestCase):
    """시험/과제 안내는 자동·수동 모두 출석 안내 ITEM_LIST 봉투를 재사용한다."""

    def test_exam_and_assignment_triggers_use_attendance_envelope(self):
        for trigger in (
            "exam_scheduled_days_before",
            "exam_start_minutes_before",
            "exam_not_taken",
            "assignment_registered",
            "assignment_due_hours_before",
            "assignment_not_submitted",
        ):
            with self.subTest(trigger=trigger):
                self.assertEqual(get_template_type(trigger), TYPE_ATTENDANCE)
                self.assertTrue(bool(get_solapi_template_id(trigger)))

    def test_retake_trigger_uses_clinic_info_envelope(self):
        self.assertEqual(get_template_type("retake_assigned"), TYPE_CLINIC_INFO)
        self.assertTrue(bool(get_solapi_template_id("retake_assigned")))


class TestRegisteredSolapiVariables(TestCase):
    def test_manual_replacements_use_registered_teacher_memo_only(self):
        replacements = build_manual_replacements(
            template_type=TYPE_CLINIC_INFO,
            content_body="내일 클리닉 안내입니다.",
            context={"장소": "301호", "날짜": "2026-05-26", "시간": "18:30"},
            tenant_name="림글리쉬",
            student_name="홍길동",
            site_url="https://limglish.hakwonplus.com",
        )
        reps = {item["key"]: item["value"] for item in replacements}
        self.assertEqual(reps["선생님메모"], "내일 클리닉 안내입니다.")
        self.assertNotIn("선생님메모1", reps)

    def test_preview_text_uses_the_exact_provider_replacements(self):
        replacements = build_manual_replacements(
            template_type=TYPE_SCORE,
            content_body="#{학생이름} 성적 안내입니다.",
            context={
                "강의명": "아주 긴 수학 심화 강의 이름은 카카오 제한에 맞춰야 합니다",
                "차시명": "3회차",
            },
            tenant_name="예담학원",
            student_name="김민준",
            site_url="https://yedam.example.com",
        )

        preview = render_alimtalk_preview_text(TYPE_SCORE, replacements)
        replacement_map = {item["key"]: item["value"] for item in replacements}

        self.assertIn("예담학원입니다.", preview)
        self.assertIn("김민준학생님.", preview)
        self.assertIn(replacement_map["강의명"], preview)
        self.assertNotIn("아주 긴 수학 심화 강의 이름은 카카오 제한에 맞춰야 합니다", preview)
        self.assertIn("김민준 성적 안내입니다.", preview)
        self.assertTrue(preview.endswith("https://yedam.example.com"))

    def test_score_replacements_match_registered_solapi_variables(self):
        replacements = build_manual_replacements(
            template_type=TYPE_SCORE,
            content_body="#{학생이름} 성적 안내입니다.\n#{시험1명}: #{시험1}/#{시험1만점}",
            context={
                "강의명": "수학A반",
                "차시명": "3회차",
                "날짜": "7월 8일",
                "시험1명": "단원평가",
                "시험1": "92",
                "시험1만점": "100",
                "시험총점": "92",
                "시험총만점": "100",
                "숙제완성도": "1/1 완료",
            },
            tenant_name="림글리쉬",
            student_name="홍길동",
            site_url="https://limglish.hakwonplus.com",
        )
        keys = [item["key"] for item in replacements]
        self.assertEqual(
            keys,
            ["학원이름", "학생이름", "강의명", "차시명", "선생님메모", "사이트링크"],
        )
        reps = {item["key"]: item["value"] for item in replacements}
        self.assertIn("단원평가: 92/100", reps["선생님메모"])
        self.assertNotIn("시험1명", reps)
        self.assertNotIn("시험1", reps)
        self.assertNotIn("시험1만점", reps)
        self.assertNotIn("숙제완성도", reps)
        self.assertNotIn("선생님메모1", reps)
