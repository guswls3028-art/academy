import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from scripts.codex.test_user_journey_outcomes import _journey
from scripts.codex.user_journey_receipts import (
    MACHINE_KIND,
    PRODUCER,
    REPOSITORY,
    ReceiptError,
    issue_receipt,
    journey_contract_sha256,
    validate_receipt,
)


HEAD = "a" * 40
FRONTEND_HEAD = "b" * 40
HASH = "c" * 64
DIGEST = f"sha256:{'d' * 64}"
NOW = dt.datetime(2026, 9, 6, 8, 30, tzinfo=dt.timezone.utc)
CI = {
    "event_name": "pull_request",
    "run_id": "12345",
    "run_attempt": "1",
    "workflow_ref": "guswls3028-art/academy-backend/.github/workflows/quality-gate.yml@refs/pull/7/merge",
}


def _machine_result(root: Path, journey: dict) -> dict:
    report = root / "test-results/backend.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="test_critical_academy_journey" name="test_positive" />'
        "</testsuite>",
        encoding="utf-8",
    )
    for name in ("desktop.png", "mobile.png"):
        (report.parent / name).write_bytes(name.encode("ascii"))
    workers = {
        name: {"repository_sha": HEAD, "digest": DIGEST}
        for name in ("messaging", "ai", "tools", "video")
    }
    projections = [
        {
            "projection_id": item["projection_id"],
            "audience": item["audience"],
            "outcome_sha256": HASH,
            "result": "passed",
        }
        for item in journey["receipt_policy"]["required_projections"]
    ]
    actions = [
        {
            **item,
            "mutation_delta": 1 if item["kind"] == "valid" else 0,
            "provider_attempt_delta": 0,
            "correlation_sha256": HASH,
            "result": "passed",
        }
        for item in journey["receipt_policy"]["required_cases"]
    ]
    return {
        "schema_version": 1,
        "kind": MACHINE_KIND,
        "producer": PRODUCER,
        "journey_id": journey["id"],
        "repository": REPOSITORY,
        "head_sha": HEAD,
        "contract_sha256": journey_contract_sha256(journey),
        "ci": {"provider": "github_actions", **CI},
        "artifacts": {
            "backend": {"repository_sha": HEAD, "api_digest": DIGEST},
            "frontend": {"repository_sha": FRONTEND_HEAD, "bundle_sha256": HASH},
            "workers": workers,
        },
        "environment": {
            "name": "persistent-development",
            "release_id": "release-20260906",
            "deployment_id": "deployment-12345",
            "api_instance_id": "i-0123456789abcdef0",
        },
        "actor": {"role": "teacher", "subject_sha256": HASH},
        "actions": actions,
        "persistence": {
            "assertion_id": "persisted-outcome",
            "write_count": 1,
            "outcome_sha256": HASH,
            "result": "passed",
        },
        "reload": {
            "assertion_id": "fresh-session-reload",
            "fresh_auth_session": True,
            "outcome_sha256": HASH,
            "result": "passed",
        },
        "projections": projections,
        "viewports": [
            {
                "id": "desktop-1366",
                "width": 1366,
                "browser": "chromium",
                "artifact_path": "test-results/desktop.png",
                "artifact_sha256": "",
                "result": "passed",
            },
            {
                "id": "mobile-390",
                "width": 390,
                "browser": "chromium",
                "artifact_path": "test-results/mobile.png",
                "artifact_sha256": "",
                "result": "passed",
            },
        ],
        "test_provenance": [
            {
                "source_path": "tests/test_critical_academy_journey.py",
                "runner": "pytest",
                "report_path": "test-results/backend.xml",
                "report_sha256": "",
                "result": "passed",
            }
        ],
        "notification_intent": {
            "selected_recipient_count": 2,
            "persisted_request_count": 2,
            "unselected_request_count": 0,
            "result": "passed",
        },
        "provider_dispatch": {
            "policy": "disabled_in_synthetic_qa",
            "mode_readback": "disabled",
            "alimtalk_attempt_count": 0,
            "sms_attempt_count": 0,
            "lms_attempt_count": 0,
            "provider_message_id_count": 0,
            "network_dispatch_count": 0,
            "result": "passed",
        },
        "cleanup": {
            "completed_at": "2026-09-06T08:20:00Z",
            "residue": {
                "tenants": 0,
                "users": 0,
                "rows": 0,
                "queue_items": 0,
                "objects": 0,
                "preview_tokens": 0,
                "provider_requests": 0,
            },
            "result": "passed",
        },
        "generated_at": "2026-09-06T08:00:00Z",
        "expires_at": "2026-09-06T12:00:00Z",
    }


class UserJourneyReceiptTests(unittest.TestCase):
    def test_exact_machine_result_issues_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journey = _journey()
            registry = {"schema_version": 1, "journeys": [journey]}
            receipt = issue_receipt(_machine_result(root, journey), registry, root, HEAD, CI, NOW)
            self.assertEqual(validate_receipt(receipt, registry, root, HEAD, CI, NOW), journey["id"])
            self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")

    def test_cross_sha_stale_provider_and_residue_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journey = _journey()
            registry = {"schema_version": 1, "journeys": [journey]}
            cases = {
                "exact repository head": (lambda item: item.update(head_sha="e" * 40), "repository/head SHA"),
                "worker cross-SHA": (
                    lambda item: item["artifacts"]["workers"]["messaging"].update(repository_sha="e" * 40),
                    "worker is cross-SHA",
                ),
                "expired": (lambda item: item.update(expires_at="2026-09-06T08:01:00Z"), "expired"),
                "provider": (
                    lambda item: item["provider_dispatch"].update(alimtalk_attempt_count=1),
                    "provider dispatch",
                ),
                "residue": (lambda item: item["cleanup"]["residue"].update(rows=1), "cleanup residue"),
                "placeholder": (lambda item: item["environment"].update(release_id="todo-release"), "identify"),
            }
            for name, (mutate, message) in cases.items():
                with self.subTest(name=name):
                    machine = _machine_result(root, journey)
                    mutate(machine)
                    with self.assertRaisesRegex(ReceiptError, message):
                        issue_receipt(machine, registry, root, HEAD, CI, NOW)

    def test_missing_projection_and_skipped_test_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journey = _journey()
            registry = {"schema_version": 1, "journeys": [journey]}
            machine = _machine_result(root, journey)
            machine["projections"].pop()
            with self.assertRaisesRegex(ReceiptError, "downstream projections"):
                issue_receipt(machine, registry, root, HEAD, CI, NOW)

    def test_no_notification_journey_requires_zero_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journey = _journey()
            journey["receipt_policy"]["notification_mode"] = "no_notification"
            registry = {"schema_version": 1, "journeys": [journey]}
            machine = _machine_result(root, journey)
            machine["notification_intent"].update(
                selected_recipient_count=0,
                persisted_request_count=0,
            )
            issue_receipt(machine, registry, root, HEAD, CI, NOW)
            machine = _machine_result(root, journey)
            with self.assertRaisesRegex(ReceiptError, "notification intent"):
                issue_receipt(machine, registry, root, HEAD, CI, NOW)

            machine = _machine_result(root, journey)
            (root / "test-results/backend.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="1">'
                '<testcase classname="test_critical_academy_journey" name="test_positive"><skipped /></testcase>'
                "</testsuite>",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ReceiptError, "non-skipped"):
                issue_receipt(machine, registry, root, HEAD, CI, NOW)

    def test_receipt_hash_and_result_artifacts_cannot_be_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            journey = _journey()
            registry = {"schema_version": 1, "journeys": [journey]}
            receipt = issue_receipt(_machine_result(root, journey), registry, root, HEAD, CI, NOW)
            tampered = copy.deepcopy(receipt)
            tampered["actor"]["role"] = "admin"
            with self.assertRaisesRegex(ReceiptError, "receipt_sha256"):
                validate_receipt(tampered, registry, root, HEAD, CI, NOW)

            receipt = issue_receipt(_machine_result(root, journey), registry, root, HEAD, CI, NOW)
            (root / "test-results/desktop.png").write_bytes(b"tampered")
            with self.assertRaisesRegex(ReceiptError, "artifact hash mismatch"):
                validate_receipt(receipt, registry, root, HEAD, CI, NOW)


if __name__ == "__main__":
    unittest.main()
