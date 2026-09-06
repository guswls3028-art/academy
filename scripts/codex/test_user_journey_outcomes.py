import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.codex.check_user_journey_outcomes import (
    RegistryError,
    _changed_paths,
    impacted_journey_ids,
    parse_pr_evidence,
    validate_registry_transition,
    validate_receipt_bootstrap_boundary,
    validate_invocation,
    validate_pr_evidence,
    validate_registry,
)


def _journey() -> dict:
    return {
        "id": "score-result-messaging",
        "availability": "beta",
        "seal_status": "gap",
        "known_gaps": ["Same-artifact replay receipt is not implemented."],
        "actors": ["owner", "admin", "teacher"],
        "projection_audiences": ["owner", "admin", "teacher", "staff", "student", "parent"],
        "role_outcomes": {
            "owner": "Owner can request and inspect the delivery.",
            "admin": "Admin can request and inspect the delivery.",
            "teacher": "Teacher can request and inspect the delivery.",
            "staff": "Staff direct send is denied before dispatch.",
            "student": "Student sees the published result projection.",
            "parent": "Parent sees the selected student result projection."
        },
        "path_patterns": ["apps/domains/results/**", "apps/domains/messaging/**"],
        "must_match": ["apps/domains/results/services/report.py"],
        "must_not_match": ["docs/domain/exam-grading.md"],
        "start_state": "A published result and explicit recipient choices exist.",
        "valid_action": "An authorized staff member sends the result letter.",
        "invalid_case": "A missing approved provider identity blocks before dispatch.",
        "persisted_outcome": "One immutable request and recipient-specific logs persist.",
        "reload_assertion": "Reload shows the same recipient and delivery state.",
        "downstream_projections": ["student result", "guardian result", "delivery log"],
        "notification_intent": "Only explicitly selected recipients are requested.",
        "provider_dispatch_policy": "disabled_in_synthetic_qa",
        "receipt_policy": {
            "ttl_seconds": 21600,
            "notification_mode": "explicit_recipients",
            "required_cases": [
                {
                    "case_id": "score-send-valid-teacher",
                    "kind": "valid",
                    "actor": "teacher",
                    "outcome_code": "requested",
                },
                {
                    "case_id": "score-send-invalid-staff",
                    "kind": "invalid",
                    "actor": "staff",
                    "outcome_code": "forbidden",
                },
            ],
            "required_projections": [
                {"projection_id": f"{role}-result", "audience": role}
                for role in ("owner", "admin", "teacher", "staff", "student", "parent")
            ],
        },
        "required_viewports": ["desktop-1366", "mobile-390"],
        "cleanup_assertion": "Synthetic tenant, users, rows, queue items, and objects are zero.",
        "executable_evidence": [
            {
                "path": "tests/test_critical_academy_journey.py",
                "proves": [
                    "valid_action",
                    "invalid_case",
                    "persisted_outcome",
                    "reload_assertion",
                    "downstream_projections",
                    "notification_intent",
                    "required_viewports",
                    "cleanup_assertion",
                    "provider_dispatch_policy",
                ],
                "runner": "pytest",
            }
        ],
        "docs": ["docs/operations/user-journey-outcome-gate.md"],
    }


def _body(*, outcome: str = "The selected recipients have one persisted request.") -> str:
    return f"""<!-- academy-user-journey-evidence-v1 -->
### Journey: score-result-messaging
- Positive action: Teacher sends one explicit result letter.
- Persisted outcome: {outcome}
- Reload persistence: Reload shows the same recipient and state.
- Downstream projection: Student, guardian, and delivery-log views agree.
- Actors: owner, admin, teacher
- Projection audiences: owner, admin, teacher, staff, student, parent
- Role outcomes: owner=request succeeds; admin=request succeeds; teacher=request succeeds; staff=direct send is denied before dispatch; student=published result is visible; parent=selected student result is visible
- Viewports: desktop-1366, mobile-390
- Executable evidence: tests/test_critical_academy_journey.py
- Invalid case: Missing provider identity is rejected before dispatch.
- Notification intent: Only selected recipients are requested.
- Provider sends: disabled_in_synthetic_qa
- Cleanup: Synthetic tenant, users, rows, queues, and objects are zero.
"""


class UserJourneyOutcomeContractTests(unittest.TestCase):
    def test_registry_is_strict_and_references_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "tests/test_critical_academy_journey.py").write_text("# evidence\n")
            (root / "docs/operations").mkdir(parents=True)
            (root / "docs/operations/user-journey-outcome-gate.md").write_text("# owner\n")

            validate_registry({"schema_version": 1, "journeys": [_journey()]}, root)

            invalid = _journey()
            invalid["persisted_outcome"] = "guard only"
            with self.assertRaisesRegex(RegistryError, "positive persisted_outcome"):
                validate_registry({"schema_version": 1, "journeys": [invalid]}, root)

            self_certified = _journey()
            self_certified["seal_status"] = "sealed"
            self_certified["known_gaps"] = []
            validate_registry({"schema_version": 1, "journeys": [self_certified]}, root)
            with self.assertRaisesRegex(RegistryError, "valid exact-SHA same-artifact receipt"):
                validate_registry_transition(
                    {"schema_version": 1, "journeys": [_journey()]},
                    {"schema_version": 1, "journeys": [self_certified]},
                )
            validate_registry_transition(
                {"schema_version": 1, "journeys": [_journey()]},
                {"schema_version": 1, "journeys": [self_certified]},
                {"score-result-messaging"},
            )

    def test_changed_product_path_selects_the_registered_journey(self):
        registry = {"schema_version": 1, "journeys": [_journey()]}
        self.assertEqual(
            impacted_journey_ids(registry, ["apps/domains/results/services/report.py"]),
            {"score-result-messaging"},
        )
        self.assertEqual(impacted_journey_ids(registry, ["docs/README.md"]), set())

    def test_real_runtime_owners_map_to_all_applicable_journeys(self):
        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / "scripts/codex/user-journey-registry.json").read_text())
        cases = {
            "apps/worker/messaging_worker/sqs_main.py": {
                "score-result-messaging", "messaging-template-recipient-log"
            },
            "academy/adapters/db/django/repositories_messaging.py": {
                "score-result-messaging", "messaging-template-recipient-log"
            },
            "apps/domains/results/services/manual_exam_grading.py": {
                "score-result-messaging", "omr-manual-descriptive"
            },
            "apps/domains/exams/views/omr_generate_view.py": {"omr-manual-descriptive"},
            "academy/adapters/db/django/repositories_clinic_targets.py": {"clinic-state"},
            "apps/domains/student_app/results/views.py": {"score-result-messaging"},
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(impacted_journey_ids(registry, [path]), expected)
        self.assertEqual(
            impacted_journey_ids(registry, ["apps/domains/messaging/migrations/9999_drop.py"]),
            set(),
        )

    def test_changed_path_collection_includes_deletions(self):
        completed = CompletedProcess([], 0, stdout="apps/domains/messaging/service.py\n", stderr="")
        with patch("scripts.codex.check_user_journey_outcomes.subprocess.run", return_value=completed) as run:
            self.assertEqual(_changed_paths(Path("."), "base"), ["apps/domains/messaging/service.py"])
        self.assertIn("--diff-filter=ACMRTD", run.call_args.args[0])

    def test_pr_evidence_requires_positive_and_invalid_outcomes(self):
        registry = {"schema_version": 1, "journeys": [_journey()]}
        parsed = parse_pr_evidence(_body())
        validate_pr_evidence(registry, {"score-result-messaging"}, parsed)

        parsed = parse_pr_evidence(_body(outcome="guard only"))
        with self.assertRaisesRegex(RegistryError, "positive persisted outcome"):
            validate_pr_evidence(registry, {"score-result-messaging"}, parsed)

        parsed = parse_pr_evidence(_body().replace("- Reload persistence:", "- Guard result:"))
        with self.assertRaisesRegex(RegistryError, "Reload persistence"):
            validate_pr_evidence(registry, {"score-result-messaging"}, parsed)

        parsed = parse_pr_evidence(_body().replace("- Cleanup: Synthetic", "- Cleanup: N/A\n- Ignored: Synthetic"))
        with self.assertRaisesRegex(RegistryError, "concrete cleanup readback"):
            validate_pr_evidence(registry, {"score-result-messaging"}, parsed)

        fenced = f"{_body().splitlines()[0]}\n```markdown\n" + "\n".join(_body().splitlines()[1:]) + "\n```"
        with self.assertRaisesRegex(RegistryError, "missing PR journey evidence"):
            validate_pr_evidence(registry, {"score-result-messaging"}, parse_pr_evidence(fenced))

    def test_unsealed_ga_journey_blocks_affected_product_change(self):
        journey = _journey()
        journey["availability"] = "generally_available"
        journey["seal_status"] = "gap"
        journey["known_gaps"] = ["Same-artifact reload and cleanup evidence is missing."]
        registry = {"schema_version": 1, "journeys": [journey]}
        with self.assertRaisesRegex(RegistryError, "no valid exact-SHA receipt"):
            validate_pr_evidence(registry, {"score-result-messaging"}, parse_pr_evidence(_body()))
        validate_pr_evidence(
            registry,
            {"score-result-messaging"},
            parse_pr_evidence(_body()),
            {"score-result-messaging"},
        )

    def test_impacted_invocation_requires_pr_event_or_explicit_registry_only(self):
        with self.assertRaisesRegex(RegistryError, "require a pull-request event"):
            validate_invocation({"score-result-messaging"}, event_path=None, registry_only=False)
        validate_invocation({"score-result-messaging"}, event_path=None, registry_only=True)

    def test_receipt_runner_and_evidence_must_land_before_product_change(self):
        registry = {"schema_version": 1, "journeys": [_journey()]}
        with self.assertRaisesRegex(RegistryError, "must land before"):
            validate_receipt_bootstrap_boundary(
                {"score-result-messaging"},
                ["apps/domains/results/service.py", "tests/test_critical_academy_journey.py"],
                registry,
            )
        validate_receipt_bootstrap_boundary(
            set(),
            ["scripts/codex/user_journey_receipts.py"],
            registry,
        )

    def test_registry_transition_cannot_hide_existing_ga_coverage(self):
        old = _journey()
        old["availability"] = "generally_available"
        previous = {"schema_version": 1, "journeys": [old]}
        relabeled = _journey()
        relabeled["availability"] = "beta"
        with self.assertRaisesRegex(RegistryError, "cannot be relabeled beta"):
            validate_registry_transition(previous, {"schema_version": 1, "journeys": [relabeled]})

        weakened = _journey()
        weakened["availability"] = "generally_available"
        weakened["must_match"] = []
        with self.assertRaisesRegex(RegistryError, "must_match fixtures cannot be removed"):
            validate_registry_transition(previous, {"schema_version": 1, "journeys": [weakened]})

        policy_changed = _journey()
        policy_changed["availability"] = "generally_available"
        policy_changed["receipt_policy"]["ttl_seconds"] = 1
        with self.assertRaisesRegex(RegistryError, "receipt policy cannot change"):
            validate_registry_transition(previous, {"schema_version": 1, "journeys": [policy_changed]})


if __name__ == "__main__":
    unittest.main()
