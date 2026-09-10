"""Issue and verify exact-SHA, same-run Academy journey receipts."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


REPOSITORY = "guswls3028-art/academy-backend"
MACHINE_KIND = "academy.user-journey-machine-result"
RECEIPT_KIND = "academy.user-journey-receipt"
PRODUCER = "academy-user-journey-receipt/v1"
VIEWPORT_WIDTHS = {"desktop-1366": 1366, "mobile-390": 390}
CLEANUP_KEYS = {
    "tenants",
    "users",
    "rows",
    "queue_items",
    "objects",
    "preview_tokens",
    "provider_requests",
}
WORKER_KEYS = {"messaging", "ai", "tools", "video"}
TOP_LEVEL_KEYS = {
    "schema_version",
    "kind",
    "producer",
    "journey_id",
    "repository",
    "head_sha",
    "contract_sha256",
    "ci",
    "artifacts",
    "environment",
    "actor",
    "actions",
    "persistence",
    "reload",
    "projections",
    "viewports",
    "test_provenance",
    "notification_intent",
    "provider_dispatch",
    "cleanup",
    "generated_at",
    "expires_at",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER_PATTERN = re.compile(r"(?:^|[-_])(todo|tbd|n/?a|skip(?:ped)?|guard|blocked)(?:$|[-_])", re.IGNORECASE)


class ReceiptError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def journey_contract_sha256(journey: dict) -> str:
    return _hash_bytes(_canonical(journey))


def _strict_object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReceiptError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _concrete_identity(value: object) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 8 and not PLACEHOLDER_PATTERN.search(value.strip())


def _relative_file(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative or relative != relative.strip() or "\\" in relative:
        raise ReceiptError(f"{label} must be a normalized repository-relative path")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ReceiptError(f"{label} escapes the repository: {relative}") from exc
    if not candidate.is_file():
        raise ReceiptError(f"{label} does not exist: {relative}")
    return candidate


def _parse_time(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ReceiptError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.utcoffset() != dt.timedelta(0):
        raise ReceiptError(f"{label} must use UTC")
    return parsed


def _validate_ci(ci: object, expected: dict[str, str]) -> None:
    ci = _strict_object(
        ci,
        {"provider", "event_name", "run_id", "run_attempt", "workflow_ref"},
        "ci identity",
    )
    if ci["provider"] != "github_actions":
        raise ReceiptError("receipt provider must be github_actions")
    if ci["event_name"] not in {"pull_request", "push", "workflow_dispatch"}:
        raise ReceiptError("receipt event_name is unsupported")
    for key in ("event_name", "run_id", "run_attempt", "workflow_ref"):
        if str(ci[key]) != str(expected.get(key, "")):
            raise ReceiptError(f"receipt ci {key} does not match the current machine run")
    if not re.search(r"\.github/workflows/(quality-gate|user-journey-receipt)\.yml@", str(ci["workflow_ref"])):
        raise ReceiptError("receipt workflow_ref must identify a trusted journey workflow")


def _validate_artifacts(artifacts: object, expected_head: str) -> None:
    artifacts = _strict_object(artifacts, {"backend", "frontend", "workers"}, "artifact identities")
    backend = _strict_object(artifacts["backend"], {"repository_sha", "api_digest"}, "backend artifact")
    frontend = _strict_object(artifacts["frontend"], {"repository_sha", "bundle_sha256"}, "frontend artifact")
    if backend["repository_sha"] != expected_head:
        raise ReceiptError("receipt backend artifact does not match the exact repository head")
    if not SHA_PATTERN.fullmatch(str(frontend["repository_sha"])):
        raise ReceiptError("frontend repository SHA is invalid")
    if not DIGEST_PATTERN.fullmatch(str(backend["api_digest"])):
        raise ReceiptError("backend API digest is invalid")
    if not HASH_PATTERN.fullmatch(str(frontend["bundle_sha256"])):
        raise ReceiptError("frontend bundle hash is invalid")

    workers = artifacts["workers"]
    if not isinstance(workers, dict) or set(workers) != WORKER_KEYS:
        raise ReceiptError("all messaging, AI, tools, and video worker identities are required")
    for worker_name, worker in workers.items():
        worker = _strict_object(worker, {"repository_sha", "digest"}, f"{worker_name} worker artifact")
        if worker["repository_sha"] != backend["repository_sha"]:
            raise ReceiptError(f"{worker_name} worker is cross-SHA")
        if not DIGEST_PATTERN.fullmatch(str(worker["digest"])):
            raise ReceiptError(f"{worker_name} worker digest is invalid")


def _validate_junit(report_path: Path, source_path: str) -> None:
    try:
        root = ET.parse(report_path).getroot()
    except ET.ParseError as exc:
        raise ReceiptError(f"pytest report is not valid JUnit XML: {report_path}") from exc
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", "0")) + int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    if tests < 1 or failures or skipped:
        raise ReceiptError(f"pytest report must contain only passed, non-skipped tests: {report_path}")
    if Path(source_path).stem not in report_path.read_text(encoding="utf-8"):
        raise ReceiptError(f"pytest report does not identify registered source {source_path}")


def _validate_result_files(payload: dict, journey: dict, root: Path, *, issuing: bool) -> None:
    known = {(item["path"], item["runner"]) for item in journey["executable_evidence"]}
    results = payload["test_provenance"]
    if not isinstance(results, list):
        raise ReceiptError("test_provenance must be a list")
    actual = {(item.get("source_path"), item.get("runner")) for item in results if isinstance(item, dict)}
    if len(actual) != len(results) or actual != known:
        raise ReceiptError("test provenance must cover every registered evidence source and runner exactly once")
    for item in results:
        _strict_object(
            item,
            {"source_path", "runner", "report_path", "report_sha256", "result"},
            "test provenance entry",
        )
        if item["result"] != "passed":
            raise ReceiptError("test provenance cannot use skipped, flaky, denied-only, or failed results")
        report_path = _relative_file(root, item["report_path"], "test result report")
        _validate_junit(report_path, item["source_path"])
        actual_hash = _hash_file(report_path)
        if issuing:
            item["report_sha256"] = actual_hash
        elif item["report_sha256"] != actual_hash:
            raise ReceiptError(f"test result report hash mismatch: {item['report_path']}")

    viewports = payload["viewports"]
    if not isinstance(viewports, list) or len(viewports) != len(VIEWPORT_WIDTHS):
        raise ReceiptError("desktop-1366 and mobile-390 viewport evidence are both required")
    by_id = {item.get("id"): item for item in viewports if isinstance(item, dict)}
    if len(by_id) != len(viewports) or set(by_id) != set(journey["required_viewports"]):
        raise ReceiptError("viewport evidence must cover each registered viewport exactly once")
    if len({item.get("artifact_path") for item in viewports}) != len(viewports):
        raise ReceiptError("viewport artifacts must be distinct")
    for viewport_id, width in VIEWPORT_WIDTHS.items():
        evidence = _strict_object(
            by_id[viewport_id],
            {"id", "width", "browser", "artifact_path", "artifact_sha256", "result"},
            f"{viewport_id} evidence",
        )
        if evidence["width"] != width or evidence["browser"] != "chromium" or evidence["result"] != "passed":
            raise ReceiptError(f"{viewport_id} evidence identity/result is invalid")
        artifact = _relative_file(root, evidence["artifact_path"], f"{viewport_id} artifact")
        actual_hash = _hash_file(artifact)
        if issuing:
            evidence["artifact_sha256"] = actual_hash
        elif evidence["artifact_sha256"] != actual_hash:
            raise ReceiptError(f"{viewport_id} artifact hash mismatch")


def _validate_actions(payload: dict, journey: dict) -> None:
    required = journey["receipt_policy"]["required_cases"]
    expected = {(item["case_id"], item["kind"], item["actor"], item["outcome_code"]) for item in required}
    actions = payload["actions"]
    if not isinstance(actions, list):
        raise ReceiptError("actions must be a list")
    actual = {
        (item.get("case_id"), item.get("kind"), item.get("actor"), item.get("outcome_code"))
        for item in actions
        if isinstance(item, dict)
    }
    if len(actual) != len(actions) or actual != expected:
        raise ReceiptError("receipt actions do not exactly cover the registered valid and invalid cases")
    for action in actions:
        _strict_object(
            action,
            {
                "case_id",
                "kind",
                "actor",
                "outcome_code",
                "mutation_delta",
                "provider_attempt_delta",
                "correlation_sha256",
                "result",
            },
            "action",
        )
        if action["result"] != "passed" or not HASH_PATTERN.fullmatch(str(action["correlation_sha256"])):
            raise ReceiptError("action result or correlation hash is invalid")
        if type(action["mutation_delta"]) is not int or type(action["provider_attempt_delta"]) is not int:
            raise ReceiptError("action deltas must be integers")
        if action["kind"] == "valid" and action["mutation_delta"] < 1:
            raise ReceiptError("positive action must prove a product mutation")
        if action["kind"] == "invalid" and action["mutation_delta"] != 0:
            raise ReceiptError("invalid action must prove zero adjacent mutation")
        if action["provider_attempt_delta"] != 0:
            raise ReceiptError("synthetic action must prove zero provider attempts")


def _validate_outcomes(payload: dict, journey: dict) -> None:
    persistence = _strict_object(
        payload["persistence"],
        {"assertion_id", "write_count", "outcome_sha256", "result"},
        "persistence assertion",
    )
    reload_result = _strict_object(
        payload["reload"],
        {"assertion_id", "fresh_auth_session", "outcome_sha256", "result"},
        "reload assertion",
    )
    if (
        persistence["assertion_id"] != "persisted-outcome"
        or type(persistence["write_count"]) is not int
        or persistence["write_count"] < 1
        or persistence["result"] != "passed"
        or not HASH_PATTERN.fullmatch(str(persistence["outcome_sha256"]))
    ):
        raise ReceiptError("positive persisted outcome is invalid")
    if (
        reload_result["assertion_id"] != "fresh-session-reload"
        or reload_result["fresh_auth_session"] is not True
        or reload_result["result"] != "passed"
        or reload_result["outcome_sha256"] != persistence["outcome_sha256"]
    ):
        raise ReceiptError("reload must prove the identical persisted outcome in a fresh auth session")

    required = journey["receipt_policy"]["required_projections"]
    expected = {(item["projection_id"], item["audience"]) for item in required}
    projections = payload["projections"]
    if not isinstance(projections, list):
        raise ReceiptError("projections must be a list")
    actual = {(item.get("projection_id"), item.get("audience")) for item in projections if isinstance(item, dict)}
    if len(actual) != len(projections) or actual != expected:
        raise ReceiptError("downstream projections do not cover the registered audiences exactly")
    for projection in projections:
        _strict_object(projection, {"projection_id", "audience", "outcome_sha256", "result"}, "projection")
        if projection["result"] != "passed" or not HASH_PATTERN.fullmatch(str(projection["outcome_sha256"])):
            raise ReceiptError("downstream projection result is invalid")


def _validate_provider_and_cleanup(payload: dict, journey: dict, generated: dt.datetime, now: dt.datetime) -> None:
    notification = _strict_object(
        payload["notification_intent"],
        {"selected_recipient_count", "persisted_request_count", "unselected_request_count", "result"},
        "notification intent",
    )
    counts = [notification[key] for key in ("selected_recipient_count", "persisted_request_count", "unselected_request_count")]
    if any(type(value) is not int for value in counts):
        raise ReceiptError("notification intent counts must be integers")
    mode = journey["receipt_policy"]["notification_mode"]
    valid_intent = {
        "explicit_recipients": counts[0] >= 1 and counts[1] == counts[0] and counts[2] == 0,
        "no_notification": counts == [0, 0, 0],
        "separate_optional": counts[0] >= 0 and counts[1] == counts[0] and counts[2] == 0,
    }[mode]
    if not valid_intent or notification["result"] != "passed":
        raise ReceiptError("notification intent must prove only the explicitly selected recipients")

    provider = _strict_object(
        payload["provider_dispatch"],
        {
            "policy",
            "mode_readback",
            "alimtalk_attempt_count",
            "sms_attempt_count",
            "lms_attempt_count",
            "provider_message_id_count",
            "network_dispatch_count",
            "result",
        },
        "provider dispatch readback",
    )
    provider_counts = [
        provider[key]
        for key in (
            "alimtalk_attempt_count",
            "sms_attempt_count",
            "lms_attempt_count",
            "provider_message_id_count",
            "network_dispatch_count",
        )
    ]
    if (
        provider["policy"] != "disabled_in_synthetic_qa"
        or provider["mode_readback"] != "disabled"
        or provider["result"] != "passed"
        or any(type(value) is not int or value != 0 for value in provider_counts)
    ):
        raise ReceiptError("provider dispatch must be disabled with exact zero attempts, IDs, and network sends")

    cleanup = _strict_object(payload["cleanup"], {"completed_at", "residue", "result"}, "cleanup")
    residue = _strict_object(cleanup["residue"], CLEANUP_KEYS, "cleanup residue")
    completed = _parse_time(cleanup["completed_at"], "cleanup.completed_at")
    if (
        cleanup["result"] != "passed"
        or completed < generated
        or completed > now + dt.timedelta(minutes=5)
        or any(type(value) is not int or value != 0 for value in residue.values())
    ):
        raise ReceiptError("cleanup residue must be numeric zero after all assertions")


def _validate_payload(
    payload: dict,
    registry: dict,
    root: Path,
    expected_head: str,
    ci_context: dict[str, str],
    now: dt.datetime,
    *,
    issuing: bool,
) -> None:
    expected_keys = TOP_LEVEL_KEYS if issuing else TOP_LEVEL_KEYS | {"receipt_sha256"}
    _strict_object(payload, expected_keys, "journey receipt")
    expected_kind = MACHINE_KIND if issuing else RECEIPT_KIND
    if payload["schema_version"] != 1 or payload["kind"] != expected_kind or payload["producer"] != PRODUCER:
        raise ReceiptError(f"receipt kind/producer must be {expected_kind} and {PRODUCER}")
    if payload["repository"] != REPOSITORY or payload["head_sha"] != expected_head:
        raise ReceiptError("receipt repository/head SHA does not match the exact checked out commit")
    journey = next((item for item in registry["journeys"] if item["id"] == payload["journey_id"]), None)
    if journey is None:
        raise ReceiptError("receipt journey is not registered")
    if payload["contract_sha256"] != journey_contract_sha256(journey):
        raise ReceiptError("receipt registry contract hash does not match the exact journey definition")

    _validate_ci(payload["ci"], ci_context)
    _validate_artifacts(payload["artifacts"], expected_head)
    environment = _strict_object(
        payload["environment"],
        {"name", "release_id", "deployment_id", "api_instance_id"},
        "environment identity",
    )
    if environment["name"] != "persistent-development" or not all(
        _concrete_identity(environment[key])
        for key in ("release_id", "deployment_id", "api_instance_id")
    ):
        raise ReceiptError("receipt must identify the persistent-development release and deployment")
    actor = _strict_object(payload["actor"], {"role", "subject_sha256"}, "actor identity")
    if actor["role"] not in journey["actors"] or not HASH_PATTERN.fullmatch(str(actor["subject_sha256"])):
        raise ReceiptError("actor role or PII-free subject fingerprint is invalid")

    generated = _parse_time(payload["generated_at"], "generated_at")
    expires = _parse_time(payload["expires_at"], "expires_at")
    ttl = journey["receipt_policy"]["ttl_seconds"]
    if (
        generated > now + dt.timedelta(minutes=5)
        or expires <= now
        or expires <= generated
        or expires - generated > dt.timedelta(seconds=ttl)
    ):
        raise ReceiptError("receipt timestamp is future, stale, expired, or has an excessive lifetime")

    _validate_actions(payload, journey)
    _validate_outcomes(payload, journey)
    _validate_result_files(payload, journey, root, issuing=issuing)
    _validate_provider_and_cleanup(payload, journey, generated, now)


def issue_receipt(
    machine_result: dict,
    registry: dict,
    root: Path,
    expected_head: str,
    ci_context: dict[str, str],
    now: dt.datetime,
) -> dict:
    receipt = copy.deepcopy(machine_result)
    _validate_payload(receipt, registry, root, expected_head, ci_context, now, issuing=True)
    receipt["kind"] = RECEIPT_KIND
    receipt["receipt_sha256"] = _hash_bytes(_canonical(receipt))
    return receipt


def validate_receipt(
    receipt: dict,
    registry: dict,
    root: Path,
    expected_head: str,
    ci_context: dict[str, str],
    now: dt.datetime,
) -> str:
    _validate_payload(receipt, registry, root, expected_head, ci_context, now, issuing=False)
    claimed = receipt["receipt_sha256"]
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not HASH_PATTERN.fullmatch(str(claimed)) or claimed != _hash_bytes(_canonical(unsigned)):
        raise ReceiptError("receipt_sha256 does not match the canonical receipt")
    return receipt["journey_id"]


def current_ci_context() -> dict[str, str]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ReceiptError("journey receipts can only be issued or verified in official GitHub Actions")
    return {
        "event_name": os.environ.get("GITHUB_EVENT_NAME", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
    }


def _head(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("issue", "verify"))
    parser.add_argument("--registry", default="scripts/codex/user-journey-registry.json")
    parser.add_argument("--machine-result")
    parser.add_argument("--receipt")
    parser.add_argument("--output")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    now = dt.datetime.now(dt.timezone.utc)
    try:
        registry = json.loads((root / args.registry).read_text(encoding="utf-8"))
        context = current_ci_context()
        if args.action == "issue":
            if not args.machine_result or not args.output:
                raise ReceiptError("issue requires --machine-result and --output")
            source = json.loads(_relative_file(root, args.machine_result, "machine result").read_text(encoding="utf-8"))
            receipt = issue_receipt(source, registry, root, _head(root), context, now)
            output = (root / args.output).resolve()
            try:
                output.relative_to(root)
            except ValueError as exc:
                raise ReceiptError("output path escapes the repository") from exc
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"USER_JOURNEY_RECEIPT_ISSUED journey={receipt['journey_id']} path={args.output}")
        else:
            if not args.receipt:
                raise ReceiptError("verify requires --receipt")
            receipt = json.loads(_relative_file(root, args.receipt, "receipt").read_text(encoding="utf-8"))
            journey_id = validate_receipt(receipt, registry, root, _head(root), context, now)
            print(f"USER_JOURNEY_RECEIPT_PASS journey={journey_id}")
        return 0
    except (OSError, json.JSONDecodeError, ReceiptError, subprocess.CalledProcessError) as exc:
        print(f"USER_JOURNEY_RECEIPT_FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
