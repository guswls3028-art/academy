from django.test import SimpleTestCase

from apps.core.services.login_identifier import normalize_login_identifier


class LoginIdentifierNormalizationTests(SimpleTestCase):
    def test_korean_mobile_login_accepts_visual_separators(self):
        self.assertEqual(
            normalize_login_identifier(" 010-1234-5678 "),
            "01012345678",
        )

    def test_nfkc_normalizes_full_width_mobile_digits(self):
        self.assertEqual(
            normalize_login_identifier("０１０－１２３４－５６７８"),
            "01012345678",
        )

    def test_custom_identifier_keeps_meaningful_punctuation(self):
        self.assertEqual(normalize_login_identifier(" staff.name "), "staff.name")
