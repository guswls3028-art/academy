from django.test import SimpleTestCase

from apps.domains.messaging.policy import (
    IMPLEMENTED_AUTO_TRIGGERS,
    get_trigger_implementation_status,
)


class ManualAttendanceTriggerPolicyTests(SimpleTestCase):
    def test_general_attendance_save_triggers_are_manual_only(self):
        for trigger in ("check_in_complete", "absent_occurred"):
            with self.subTest(trigger=trigger):
                self.assertNotIn(trigger, IMPLEMENTED_AUTO_TRIGGERS)
                self.assertEqual(
                    get_trigger_implementation_status(trigger),
                    "manual_only",
                )
