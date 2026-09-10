from io import StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.management.commands import check_dev_alerts as alerts
from apps.core.models import OpsAuditLog


@override_settings(DEV_ALERTS_WEBHOOK_URL="https://hooks.example.invalid/test")
class DevAlertsCommandTests(TestCase):
    def setUp(self):
        self.output = StringIO()
        self.errors = StringIO()
        self.evaluate = Mock(return_value=None)
        self.rules = [alerts.Rule("test_health", "Test health", self.evaluate)]
        self.rule_patch = patch.object(alerts, "RULES", self.rules)
        self.rule_patch.start()
        self.addCleanup(self.rule_patch.stop)
        self.post_patch = patch.object(alerts, "_post_slack", return_value=True)
        self.post = self.post_patch.start()
        self.addCleanup(self.post_patch.stop)

    def run_command(self, *args):
        call_command(
            "check_dev_alerts", *args, stdout=self.output, stderr=self.errors,
        )

    def assert_audit(self, result):
        audit = OpsAuditLog.objects.get(action="cron.check_dev_alerts")
        self.assertEqual(audit.result, result)
        return audit

    @override_settings(DEV_ALERTS_WEBHOOK_URL="")
    def test_missing_receiver_is_failure_even_when_no_rule_fires(self):
        with self.assertRaisesMessage(CommandError, "DEV_ALERTS_WEBHOOK_URL"):
            self.run_command("--silent")
        self.post.assert_not_called()
        self.assert_audit("failed")
        self.assertNotIn("All clear", self.output.getvalue())

    @override_settings(DEV_ALERTS_WEBHOOK_URL="")
    def test_missing_receiver_with_findings_is_not_delivery_success(self):
        self.evaluate.return_value = {"title": "Finding", "rows": [{"count": 1}]}
        with self.assertRaisesMessage(CommandError, "DEV_ALERTS_WEBHOOK_URL"):
            self.run_command()
        self.post.assert_not_called()
        self.assert_audit("failed")

    def test_failed_delivery_is_failure(self):
        self.evaluate.return_value = {"title": "Finding", "rows": [{"count": 1}]}
        self.post.return_value = False
        with self.assertRaisesMessage(CommandError, "Slack delivery failed"):
            self.run_command()
        self.post.assert_called_once()
        self.assert_audit("failed")

    def test_unknown_selection_does_not_report_all_clear(self):
        with self.assertRaisesMessage(CommandError, "Unknown alert rule selection"):
            self.run_command("--rule", "unknown")
        self.evaluate.assert_not_called()
        self.post.assert_not_called()
        self.assert_audit("failed")
        self.assertNotIn("All clear", self.output.getvalue())

    def test_any_rule_error_is_failure_without_sensitive_exception_text(self):
        sentinel = "synthetic-private-error-body"
        self.evaluate.side_effect = RuntimeError(sentinel)
        with self.assertLogs(alerts.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(CommandError, "test_health.*RuntimeError") as raised:
                self.run_command()
        audit = self.assert_audit("failed")
        self.assertNotIn(sentinel, str(raised.exception))
        self.assertNotIn(sentinel, audit.error)
        self.assertNotIn(sentinel, self.output.getvalue() + self.errors.getvalue())
        self.assertNotIn(sentinel, "\n".join(logs.output))
        self.assertNotIn("All clear", self.output.getvalue())
        self.post.assert_not_called()

    def test_rule_failure_does_not_suppress_other_valid_alerts(self):
        self.evaluate.side_effect = RuntimeError("private")
        healthy_rule = alerts.Rule(
            "other_health", "Other health",
            Mock(return_value={"title": "Finding", "rows": [{"count": 1}]}),
        )
        self.rules.append(healthy_rule)
        with self.assertRaises(CommandError):
            self.run_command()
        healthy_rule.evaluate.assert_called_once()
        self.post.assert_called_once()
        self.assert_audit("failed")

    @override_settings(DEV_ALERTS_WEBHOOK_URL="")
    def test_dry_run_succeeds_without_receiver_and_never_sends(self):
        self.evaluate.return_value = {"title": "Finding", "rows": [{"count": 1}]}
        self.run_command("--dry-run")
        self.post.assert_not_called()
        self.assert_audit("success")

    def test_dry_run_still_fails_when_rule_evaluation_fails(self):
        self.evaluate.side_effect = RuntimeError("private")
        with self.assertRaises(CommandError):
            self.run_command("--dry-run")
        self.post.assert_not_called()
        self.assert_audit("failed")

    def test_clear_with_config_does_not_send_unsolicited_probe(self):
        self.run_command()
        self.post.assert_not_called()
        self.assert_audit("success")
        self.assertIn("All clear", self.output.getvalue())

    def test_successful_delivery_is_recorded_as_success(self):
        self.evaluate.return_value = {"title": "Finding", "rows": [{"count": 1}]}
        self.run_command()
        self.post.assert_called_once()
        self.assert_audit("success")
        self.assertIn("Finding", self.output.getvalue())
        self.assertIn('"count": 1', self.output.getvalue())

    def test_silent_delivery_keeps_private_details_out_of_console(self):
        title = "synthetic-private-inquiry-title"
        row = "synthetic-private-inquiry-body"
        self.evaluate.return_value = {"title": title, "rows": [{"title": row}]}
        self.run_command("--silent")
        self.assertNotIn(title, self.output.getvalue() + self.errors.getvalue())
        self.assertNotIn(row, self.output.getvalue() + self.errors.getvalue())
        self.assertIn("[test_health] rows=1", self.output.getvalue())
        self.post.assert_called_once()
        receiver, payload = self.post.call_args.args
        self.assertEqual(receiver, "https://hooks.example.invalid/test")
        self.assertIn(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n• title={row}"}},
            payload["blocks"],
        )
        self.assert_audit("success")

    @override_settings(DEV_ALERTS_WEBHOOK_URL="")
    def test_silent_missing_receiver_does_not_dump_private_findings(self):
        sentinel = "synthetic-private-inquiry"
        self.evaluate.return_value = {"title": sentinel, "rows": [{"title": sentinel}]}
        with self.assertRaisesMessage(CommandError, "DEV_ALERTS_WEBHOOK_URL"):
            self.run_command("--silent")
        self.assertNotIn(sentinel, self.output.getvalue() + self.errors.getvalue())
        self.post.assert_not_called()
        self.assert_audit("failed")

    def test_silent_dry_run_does_not_dump_private_findings(self):
        sentinel = "synthetic-private-inquiry"
        self.evaluate.return_value = {"title": sentinel, "rows": [{"title": sentinel}]}
        self.run_command("--silent", "--dry-run")
        self.assertNotIn(sentinel, self.output.getvalue() + self.errors.getvalue())
        self.assertIn("[test_health] rows=1", self.output.getvalue())
        self.post.assert_not_called()
        self.assert_audit("success")

    def test_webhook_exception_does_not_log_secret_url_or_response(self):
        sentinel = "synthetic-private-webhook-token"
        self.post_patch.stop()
        with patch.object(alerts.urllib.request, "urlopen", side_effect=ValueError(sentinel)):
            with self.assertLogs(alerts.__name__, level="WARNING") as logs:
                self.assertFalse(alerts._post_slack("https://example.invalid/secret", {}))
        self.assertNotIn(sentinel, "\n".join(logs.output))

    def test_audit_write_failure_does_not_report_success(self):
        with patch.object(OpsAuditLog.objects, "create", side_effect=RuntimeError("private")):
            with self.assertRaisesMessage(CommandError, "invocation audit failed"):
                self.run_command()

    def test_local_receiver_failure_then_recovery_has_truthful_run_status(self):
        requests = []
        status = [503]

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(status[0])
                self.end_headers()

            def log_message(self, *args):
                pass

        self.post_patch.stop()
        self.evaluate.return_value = {"title": "Synthetic finding", "rows": [{"count": 1}]}
        with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.settings(DEV_ALERTS_WEBHOOK_URL=f"http://127.0.0.1:{server.server_port}"):
                    with self.assertRaisesMessage(CommandError, "Slack delivery failed"):
                        self.run_command()
                    status[0] = 200
                    self.run_command()
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.assertEqual(len(requests), 2)
        self.assertIn(b"Synthetic finding", requests[1])
        self.assertEqual(
            list(OpsAuditLog.objects.filter(action="cron.check_dev_alerts")
                 .order_by("id").values_list("result", flat=True)),
            ["failed", "success"],
        )


class DevAlertsEvaluatorImportTests(SimpleTestCase):
    def test_import_failures_cannot_be_reported_as_no_findings(self):
        original_import = builtins.__import__

        def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "apps.core.models":
                raise ImportError("synthetic unavailable model")
            return original_import(name, globals, locals, fromlist, level)

        for evaluate in (alerts.rule_unanswered_inbox, alerts.rule_stale_workers, alerts.rule_circuit_breaker_open):
            with self.subTest(rule=evaluate.__name__):
                with patch("builtins.__import__", side_effect=failing_import):
                    with self.assertRaises(ImportError):
                        evaluate()
import builtins
