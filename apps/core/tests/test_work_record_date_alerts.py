from datetime import date, time, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
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
            },
        )
        rendered = str(result)
        self.assertNotIn(self.tenant.name, rendered)
        for staff in self.staffs:
            self.assertNotIn(staff.name, rendered)

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

    def test_events_outside_bounded_window_are_ignored(self):
        record = self._candidate(staff_index=0)
        second = self._candidate(staff_index=1)
        cutoff = timezone.now() - timedelta(days=36)
        OpsAuditLog.objects.filter(
            action="staff.work_record_created",
            payload__work_record_id__in=[record.id, second.id],
        ).update(created_at=cutoff)

        self.assertIsNone(alerts.rule_work_record_date_anomalies())
