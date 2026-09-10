from datetime import date, time, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.management.commands import check_dev_alerts as alerts
from apps.core.models import OpsAuditLog, Tenant
from apps.domains.staffs.models import Staff, WorkRecord, WorkType


class WorkRecordDateAlertTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            code="work-date-alert",
            name="Work Date Alert",
            is_active=True,
        )
        self.work_type = WorkType.objects.create(
            tenant=self.tenant,
            name="기본 근무",
            base_hourly_wage=15_000,
            is_active=True,
        )
        self.staffs = [
            Staff.objects.create(
                tenant=self.tenant,
                name=f"직원 {index}",
                phone="",
            )
            for index in range(3)
        ]

    def _candidate(self, *, staff_index, selected_date="2026-08-01"):
        staff = self.staffs[staff_index]
        record_date = date.fromisoformat(selected_date)
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=record_date,
            start_time=time(14, 0),
            end_time=time(18, 0),
        )
        OpsAuditLog.objects.create(
            action="staff.work_record_created",
            target_tenant=self.tenant,
            summary=f"work_record_id={record.id}",
            payload={
                "source": "payroll_manager_manual",
                "work_record_id": record.id,
                "staff_id": staff.id,
                "date": selected_date,
                "created_local_date": "2026-09-10",
            },
        )
        return record

    def _updated_candidate(
        self,
        *,
        staff_index,
        old_date="2026-08-09",
        selected_date="2026-08-01",
        old_staff_index=None,
    ):
        staff = self.staffs[staff_index]
        old_staff = self.staffs[
            staff_index if old_staff_index is None else old_staff_index
        ]
        record = WorkRecord.objects.create(
            tenant=self.tenant,
            staff=staff,
            work_type=self.work_type,
            date=date.fromisoformat(selected_date),
            start_time=time(14, 0),
            end_time=time(18, 0),
        )
        fields = ["date"]
        if old_staff.id != staff.id:
            fields.append("staff")
        OpsAuditLog.objects.create(
            action="staff.work_record_updated",
            target_tenant=self.tenant,
            summary=f"work_record_id={record.id}",
            payload={
                "source": "payroll_manager_manual",
                "work_record_id": record.id,
                "fields": sorted(fields),
                "old": {
                    "date": old_date,
                    "staff": str(old_staff.id),
                },
                "new": {
                    "date": selected_date,
                    "staff": str(staff.id),
                },
            },
        )
        return record

    def test_two_distinct_staff_with_same_stale_month_first_date_trigger_review(self):
        first = self._candidate(staff_index=0)
        second = self._candidate(staff_index=1)

        result = alerts.rule_work_record_date_anomalies()

        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(
            result["rows"][0],
            {
                "tenant_id": self.tenant.id,
                "selected_date": "2026-08-01",
                "distinct_staff": 2,
                "record_count": 2,
                "work_record_ids": [first.id, second.id],
                "evidence": [
                    {
                        "action": "created",
                        "work_record_id": first.id,
                        "old_date": None,
                        "new_date": "2026-08-01",
                        "old_staff_id": None,
                        "new_staff_id": self.staffs[0].id,
                    },
                    {
                        "action": "created",
                        "work_record_id": second.id,
                        "old_date": None,
                        "new_date": "2026-08-01",
                        "old_staff_id": None,
                        "new_staff_id": self.staffs[1].id,
                    },
                ],
            },
        )
        rendered = str(result)
        self.assertNotIn(self.tenant.name, rendered)
        for staff in self.staffs:
            self.assertNotIn(staff.name, rendered)

    def test_updates_to_same_month_first_date_trigger_with_exact_old_new_evidence(self):
        first = self._updated_candidate(staff_index=0, old_date="2026-08-09")
        second = self._updated_candidate(
            staff_index=1,
            old_date="2026-08-08",
            old_staff_index=2,
        )

        result = alerts.rule_work_record_date_anomalies()

        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 2)
        row = result["rows"][0]
        self.assertEqual(row["work_record_ids"], [first.id, second.id])
        self.assertEqual(
            row["evidence"],
            [
                {
                    "action": "updated",
                    "work_record_id": first.id,
                    "old_date": "2026-08-09",
                    "new_date": "2026-08-01",
                    "old_staff_id": self.staffs[0].id,
                    "new_staff_id": self.staffs[0].id,
                },
                {
                    "action": "updated",
                    "work_record_id": second.id,
                    "old_date": "2026-08-08",
                    "new_date": "2026-08-01",
                    "old_staff_id": self.staffs[2].id,
                    "new_staff_id": self.staffs[1].id,
                },
            ],
        )

    def test_staff_only_update_into_existing_month_first_concentration_is_detected(self):
        first = self._candidate(staff_index=0)
        second = self._updated_candidate(
            staff_index=1,
            old_date="2026-08-01",
            selected_date="2026-08-01",
            old_staff_index=0,
        )

        result = alerts.rule_work_record_date_anomalies()

        self.assertIsNotNone(result)
        self.assertEqual(result["rows"][0]["work_record_ids"], [first.id, second.id])
        self.assertEqual(result["rows"][0]["distinct_staff"], 2)

    def test_single_legitimate_backfill_is_not_treated_as_anomaly(self):
        self._candidate(staff_index=0)

        self.assertIsNone(alerts.rule_work_record_date_anomalies())

    def test_same_staff_repeated_entry_does_not_meet_distinct_staff_threshold(self):
        self._candidate(staff_index=0)
        self._candidate(staff_index=0)

        self.assertIsNone(alerts.rule_work_record_date_anomalies())

    def test_candidates_from_different_tenants_are_not_combined(self):
        self._candidate(staff_index=0)
        other_tenant = Tenant.objects.create(
            code="other-work-date-alert",
            name="Other Work Date Alert",
            is_active=True,
        )
        other_work_type = WorkType.objects.create(
            tenant=other_tenant,
            name="기본 근무",
            base_hourly_wage=15_000,
            is_active=True,
        )
        other_staff = Staff.objects.create(
            tenant=other_tenant,
            name="다른 테넌트 직원",
            phone="",
        )
        other_record = WorkRecord.objects.create(
            tenant=other_tenant,
            staff=other_staff,
            work_type=other_work_type,
            date=date(2026, 8, 1),
            start_time=time(14, 0),
            end_time=time(18, 0),
        )
        OpsAuditLog.objects.create(
            action="staff.work_record_created",
            target_tenant=other_tenant,
            payload={
                "source": "payroll_manager_manual",
                "work_record_id": other_record.id,
                "staff_id": other_staff.id,
                "date": "2026-08-01",
                "created_local_date": "2026-09-10",
            },
        )

        self.assertIsNone(alerts.rule_work_record_date_anomalies())

    def test_non_first_day_and_same_day_creation_are_not_candidates(self):
        self._candidate(staff_index=0, selected_date="2026-08-02")
        same_day = self._candidate(staff_index=1)
        audit = OpsAuditLog.objects.get(
            action="staff.work_record_created",
            payload__work_record_id=same_day.id,
        )
        audit.payload["created_local_date"] = "2026-08-01"
        audit.save(update_fields=["payload"])

        self.assertIsNone(alerts.rule_work_record_date_anomalies())

    def test_deleted_or_date_corrected_records_do_not_keep_alert_open(self):
        first = self._candidate(staff_index=0)
        second = self._candidate(staff_index=1)
        first.delete()
        second.date = date(2026, 8, 2)
        second.save(update_fields=["date"])

        self.assertIsNone(alerts.rule_work_record_date_anomalies())

    def test_successful_slack_delivery_deduplicates_exact_fingerprint(self):
        self._candidate(staff_index=0)
        self._candidate(staff_index=1)
        result = alerts.rule_work_record_date_anomalies()

        alerts._record_work_record_date_slack_delivery(result)

        self.assertIsNone(alerts.rule_work_record_date_anomalies())
        receipt = OpsAuditLog.objects.get(
            action=alerts.WORK_RECORD_DATE_SLACK_DELIVERY_ACTION,
        )
        self.assertEqual(receipt.payload["fingerprints"], result["fingerprints"])
        self.assertEqual(receipt.payload["displayed_group_count"], 1)

    def test_receipt_only_consumes_the_five_groups_displayed_in_slack(self):
        selected_dates = [
            "2026-01-01",
            "2026-02-01",
            "2026-03-01",
            "2026-04-01",
            "2026-05-01",
            "2026-06-01",
        ]
        for selected_date in selected_dates:
            self._candidate(staff_index=0, selected_date=selected_date)
            self._candidate(staff_index=1, selected_date=selected_date)
        result = alerts.rule_work_record_date_anomalies()

        alerts._record_work_record_date_slack_delivery(result)

        receipt = OpsAuditLog.objects.get(
            action=alerts.WORK_RECORD_DATE_SLACK_DELIVERY_ACTION,
        )
        self.assertEqual(receipt.payload["displayed_group_count"], 5)
        self.assertEqual(
            receipt.payload["fingerprints"],
            result["fingerprints"][: alerts.SLACK_RULE_ROW_LIMIT],
        )
        remaining = alerts.rule_work_record_date_anomalies()
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["total"], 2)
        self.assertEqual(
            [row["selected_date"] for row in remaining["rows"]],
            ["2026-06-01"],
        )

    def test_custom_window_days_also_bounds_delivery_receipt_retention(self):
        self._candidate(staff_index=0)
        self._candidate(staff_index=1)
        result = alerts.rule_work_record_date_anomalies(window_days=5)
        alerts._record_work_record_date_slack_delivery(result)
        OpsAuditLog.objects.filter(
            action=alerts.WORK_RECORD_DATE_SLACK_DELIVERY_ACTION,
        ).update(created_at=timezone.now() - timedelta(days=10))

        repeated = alerts.rule_work_record_date_anomalies(window_days=5)

        self.assertIsNotNone(repeated)
        self.assertEqual(repeated["fingerprints"], result["fingerprints"])

    @override_settings(DEV_ALERTS_WEBHOOK_URL="https://hooks.example.invalid/test")
    def test_command_records_dedupe_only_after_slack_accepts(self):
        self._candidate(staff_index=0)
        self._candidate(staff_index=1)
        output = StringIO()

        with patch.object(alerts, "_post_slack", return_value=True):
            call_command(
                "check_dev_alerts",
                "--rule",
                "work_record_date_anomalies",
                stdout=output,
            )

        self.assertTrue(
            OpsAuditLog.objects.filter(
                action=alerts.WORK_RECORD_DATE_SLACK_DELIVERY_ACTION,
            ).exists()
        )
        self.assertIsNone(alerts.rule_work_record_date_anomalies())

    @override_settings(
        DEV_ALERTS_WEBHOOK_URL="",
        DEV_ALERTS_WEBHOOK_REQUIRED=False,
    )
    def test_command_fails_closed_when_work_record_alert_has_no_recipient(self):
        self._candidate(staff_index=0)
        self._candidate(staff_index=1)

        with self.assertRaisesMessage(
            CommandError,
            "work_record_date_anomalies",
        ):
            call_command(
                "check_dev_alerts",
                "--rule",
                "work_record_date_anomalies",
                stdout=StringIO(),
            )

        self.assertFalse(
            OpsAuditLog.objects.filter(
                action=alerts.WORK_RECORD_DATE_SLACK_DELIVERY_ACTION,
            ).exists()
        )
        self.assertEqual(
            OpsAuditLog.objects.get(action="cron.check_dev_alerts").result,
            "failed",
        )

    def test_events_outside_bounded_window_are_ignored(self):
        record = self._candidate(staff_index=0)
        second = self._candidate(staff_index=1)
        cutoff = timezone.now() - timedelta(days=36)
        OpsAuditLog.objects.filter(
            action="staff.work_record_created",
            payload__work_record_id__in=[record.id, second.id],
        ).update(created_at=cutoff)

        self.assertIsNone(alerts.rule_work_record_date_anomalies())
