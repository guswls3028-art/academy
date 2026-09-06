"""Validate Academy journey coverage and pull-request outcome evidence."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import datetime as dt
from pathlib import Path
from typing import Iterable

if __package__:
    from scripts.codex.user_journey_receipts import ReceiptError, current_ci_context, validate_receipt
else:
    from user_journey_receipts import ReceiptError, current_ci_context, validate_receipt


MARKER = "<!-- academy-user-journey-evidence-v1 -->"
CORE_JOURNEY_IDS = {
    "score-result-messaging",
    "omr-manual-descriptive",
    "clinic-state",
    "messaging-template-recipient-log",
}
CANONICAL_ROLES = {"owner", "admin", "teacher", "staff", "student", "parent"}
PROTECTED_RECEIPT_PATHS = {
    ".github/workflows/quality-gate.yml",
    "scripts/codex/check_user_journey_outcomes.py",
    "scripts/codex/user_journey_receipts.py",
    "scripts/codex/user-journey-registry.json",
}
REQUIRED_FIELDS = {
    "start_state",
    "valid_action",
    "invalid_case",
    "persisted_outcome",
    "reload_assertion",
    "notification_intent",
    "cleanup_assertion",
}
POSITIVE_FIELDS = {
    "valid_action",
    "persisted_outcome",
    "reload_assertion",
}
PR_FIELDS = {
    "Positive action": "positive_action",
    "Persisted outcome": "persisted_outcome",
    "Reload persistence": "reload_persistence",
    "Downstream projection": "downstream_projection",
    "Actors": "actors",
    "Projection audiences": "projection_audiences",
    "Role outcomes": "role_outcomes",
    "Viewports": "viewports",
    "Executable evidence": "executable_evidence",
    "Invalid case": "invalid_case",
    "Notification intent": "notification_intent",
    "Provider sends": "provider_sends",
    "Cleanup": "cleanup",
}


class RegistryError(ValueError):
    pass


def _positive(value: object) -> bool:
    if not isinstance(value, str) or len(value.strip()) < 8:
        return False
    normalized = re.sub(r"[\s_-]+", " ", value.strip().lower())
    if normalized in {
        "n/a",
        "none",
        "guard",
        "guard only",
        "denied only",
        "blocked only",
        "not applicable",
        "skip",
        "skipped",
    }:
        return False
    return not re.search(r"\b(todo|tbd|guard only|denied only|blocked only|not applicable|skipped?)\b", normalized)


def _require_local_file(root: Path, relative: object, label: str) -> None:
    if not isinstance(relative, str) or not relative.strip():
        raise RegistryError(f"{label} must be a non-empty repository-relative path")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RegistryError(f"{label} escapes the repository: {relative}") from exc
    if not candidate.is_file():
        raise RegistryError(f"{label} does not exist: {relative}")
    worktree = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if worktree.returncode == 0:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if tracked.returncode:
            raise RegistryError(f"{label} is not tracked by Git: {relative}")


def validate_registry(registry: object, root: Path) -> None:
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise RegistryError("registry schema_version must be 1")
    journeys = registry.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        raise RegistryError("registry journeys must be a non-empty list")

    seen: set[str] = set()
    for journey in journeys:
        if not isinstance(journey, dict):
            raise RegistryError("each journey must be an object")
        journey_id = journey.get("id")
        if not isinstance(journey_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", journey_id):
            raise RegistryError(f"invalid journey id: {journey_id!r}")
        if journey_id in seen:
            raise RegistryError(f"duplicate journey id: {journey_id}")
        seen.add(journey_id)

        if journey.get("availability") not in {"generally_available", "beta"}:
            raise RegistryError(f"{journey_id}: invalid availability")
        if journey.get("seal_status") not in {"gap", "sealed"}:
            raise RegistryError(f"{journey_id}: seal_status must be gap or sealed")
        for field in REQUIRED_FIELDS:
            if not isinstance(journey.get(field), str) or not journey[field].strip():
                raise RegistryError(f"{journey_id}: {field} must be non-empty")
        for field in POSITIVE_FIELDS:
            if not _positive(journey[field]):
                raise RegistryError(f"{journey_id}: positive {field} is required; guard-only evidence cannot close it")

        for role_field in ("actors", "projection_audiences"):
            roles = journey.get(role_field)
            if not isinstance(roles, list) or not roles or len(roles) != len(set(roles)):
                raise RegistryError(f"{journey_id}: {role_field} must be a unique non-empty list")
            if not set(roles) <= CANONICAL_ROLES:
                raise RegistryError(f"{journey_id}: unsupported {role_field}: {sorted(set(roles) - CANONICAL_ROLES)}")
        role_outcomes = journey.get("role_outcomes")
        if not isinstance(role_outcomes, dict) or set(role_outcomes) != CANONICAL_ROLES:
            raise RegistryError(f"{journey_id}: role_outcomes must cover every canonical role")
        if not all(_positive(outcome) for outcome in role_outcomes.values()):
            raise RegistryError(f"{journey_id}: role_outcomes must be concrete for every canonical role")

        patterns = journey.get("path_patterns")
        if not isinstance(patterns, list) or not patterns or not all(_valid_pattern(item) for item in patterns):
            raise RegistryError(f"{journey_id}: path_patterns must be non-empty strings")
        for fixture_field in ("must_match", "must_not_match"):
            fixtures = journey.get(fixture_field)
            if not isinstance(fixtures, list) or not fixtures or not all(_valid_pattern(item) for item in fixtures):
                raise RegistryError(f"{journey_id}: {fixture_field} must contain normalized repository paths")
        projections = journey.get("downstream_projections")
        if not isinstance(projections, list) or not projections or not all(_positive(item) for item in projections):
            raise RegistryError(f"{journey_id}: downstream_projections must describe positive visible outcomes")
        if journey.get("availability") == "generally_available":
            if set(journey.get("required_viewports", [])) != {"desktop-1366", "mobile-390"}:
                raise RegistryError(f"{journey_id}: generally available journeys require desktop-1366 and mobile-390")
        if journey.get("provider_dispatch_policy") != "disabled_in_synthetic_qa":
            raise RegistryError(f"{journey_id}: synthetic QA must disable provider dispatch")

        receipt_policy = journey.get("receipt_policy")
        if not isinstance(receipt_policy, dict) or set(receipt_policy) != {
            "ttl_seconds",
            "notification_mode",
            "required_cases",
            "required_projections",
        }:
            raise RegistryError(f"{journey_id}: receipt_policy is incomplete")
        if receipt_policy["ttl_seconds"] != 21600:
            raise RegistryError(f"{journey_id}: receipt TTL must be exactly 21600 seconds")
        if receipt_policy["notification_mode"] not in {"explicit_recipients", "no_notification", "separate_optional"}:
            raise RegistryError(f"{journey_id}: receipt notification mode is invalid")
        cases = receipt_policy["required_cases"]
        if not isinstance(cases, list) or not cases:
            raise RegistryError(f"{journey_id}: receipt_policy requires valid and invalid cases")
        case_keys = {
            (item.get("case_id"), item.get("kind"), item.get("actor"), item.get("outcome_code"))
            for item in cases
            if isinstance(item, dict) and set(item) == {"case_id", "kind", "actor", "outcome_code"}
        }
        if len(case_keys) != len(cases) or {item[1] for item in case_keys} != {"valid", "invalid"}:
            raise RegistryError(f"{journey_id}: receipt cases must be unique and include valid and invalid")
        if not all(
            re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(item[0]))
            and item[2] in CANONICAL_ROLES
            and re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", str(item[3]))
            and item[3] not in {"todo", "tbd", "n_a", "skip", "skipped", "guard_only", "blocked_only"}
            for item in case_keys
        ):
            raise RegistryError(f"{journey_id}: receipt case identity is invalid")
        projections = receipt_policy["required_projections"]
        if not isinstance(projections, list):
            raise RegistryError(f"{journey_id}: receipt projections must be a list")
        projection_keys = {
            (item.get("projection_id"), item.get("audience"))
            for item in projections
            if isinstance(item, dict) and set(item) == {"projection_id", "audience"}
        }
        if len(projection_keys) != len(projections) or {item[1] for item in projection_keys} != set(
            journey["projection_audiences"]
        ):
            raise RegistryError(f"{journey_id}: receipt projections must cover every projection audience exactly")
        if not all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(item[0])) for item in projection_keys):
            raise RegistryError(f"{journey_id}: receipt projection identity is invalid")

        evidence = journey.get("executable_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise RegistryError(f"{journey_id}: executable_evidence must be non-empty")
        for item in evidence:
            if not isinstance(item, dict):
                raise RegistryError(f"{journey_id}: executable evidence entries must be objects")
            _require_local_file(root, item.get("path"), f"{journey_id} evidence")
            proves = item.get("proves")
            if not isinstance(proves, list) or not proves or not all(isinstance(value, str) and value for value in proves):
                raise RegistryError(f"{journey_id}: evidence proves must be non-empty strings")
            if item.get("runner") != "pytest" or not item["path"].endswith(".py"):
                raise RegistryError(f"{journey_id}: backend evidence must use a pytest .py path")
        known_gaps = journey.get("known_gaps")
        if not isinstance(known_gaps, list) or not all(_positive(gap) for gap in known_gaps):
            raise RegistryError(f"{journey_id}: known_gaps must be a list of concrete gaps")
        if journey["seal_status"] == "gap" and not known_gaps:
            raise RegistryError(f"{journey_id}: gap journeys must name at least one known gap")
        if journey["seal_status"] == "sealed" and known_gaps:
            raise RegistryError(f"{journey_id}: sealed journeys cannot retain known gaps")
        docs = journey.get("docs")
        if not isinstance(docs, list) or not docs:
            raise RegistryError(f"{journey_id}: docs must be non-empty")
        for doc in docs:
            _require_local_file(root, doc, f"{journey_id} doc")

    for journey in journeys:
        for fixture in journey["must_match"]:
            if journey["id"] not in impacted_journey_ids(registry, [fixture]):
                raise RegistryError(f"{journey['id']}: must_match is not covered by path_patterns: {fixture}")
        for fixture in journey["must_not_match"]:
            if journey["id"] in impacted_journey_ids(registry, [fixture]):
                raise RegistryError(f"{journey['id']}: must_not_match is covered by path_patterns: {fixture}")


def _valid_pattern(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\\" not in value
        and not value.startswith("./")
    )


def validate_core_journeys(registry: dict) -> None:
    current = {journey["id"] for journey in registry["journeys"]}
    missing = sorted(CORE_JOURNEY_IDS - current)
    if missing:
        raise RegistryError(f"core journey entries cannot be removed: {', '.join(missing)}")


def validate_registry_transition(
    previous: dict | None,
    current: dict,
    valid_receipts: set[str] | None = None,
) -> None:
    valid_receipts = valid_receipts or set()
    if previous is None:
        return
    previous_by_id = {journey["id"]: journey for journey in previous["journeys"]}
    current_by_id = {journey["id"]: journey for journey in current["journeys"]}
    for journey_id, old in previous_by_id.items():
        new = current_by_id.get(journey_id)
        if new is None:
            raise RegistryError(f"existing journey cannot be removed: {journey_id}")
        if old["availability"] == "generally_available" and new["availability"] != "generally_available":
            raise RegistryError(f"{journey_id}: generally available journey cannot be relabeled beta")
        if old["seal_status"] == "sealed" and new["seal_status"] != "sealed":
            raise RegistryError(f"{journey_id}: sealed journey cannot regress to gap")
        if old["seal_status"] == "gap" and new["seal_status"] == "sealed" and journey_id not in valid_receipts:
            raise RegistryError(f"{journey_id}: gap cannot be promoted without a valid exact-SHA same-artifact receipt")
        if old["receipt_policy"] != new["receipt_policy"]:
            raise RegistryError(f"{journey_id}: receipt policy cannot change in a journey product PR")
        for role_field in ("actors", "projection_audiences"):
            removed_roles = sorted(set(old[role_field]) - set(new[role_field]))
            if removed_roles:
                raise RegistryError(f"{journey_id}: existing {role_field} cannot be removed: {', '.join(removed_roles)}")
        for fixture_field in ("must_match", "must_not_match"):
            removed_fixtures = sorted(set(old[fixture_field]) - set(new[fixture_field]))
            if removed_fixtures:
                raise RegistryError(
                    f"{journey_id}: existing {fixture_field} fixtures cannot be removed: {', '.join(removed_fixtures)}"
                )
        new_evidence = {item["path"]: set(item["proves"]) for item in new["executable_evidence"]}
        for item in old["executable_evidence"]:
            if item["path"] not in new_evidence:
                raise RegistryError(f"{journey_id}: existing evidence path cannot be removed: {item['path']}")
            removed_proofs = sorted(set(item["proves"]) - new_evidence[item["path"]])
            if removed_proofs:
                raise RegistryError(f"{journey_id}: existing evidence claims cannot be removed: {', '.join(removed_proofs)}")


def impacted_journey_ids(registry: dict, changed_paths: Iterable[str]) -> set[str]:
    normalized = [
        normalized_path
        for path in changed_paths
        if _is_runtime_path(normalized_path := path.replace("\\", "/").removeprefix("./"))
    ]
    return {
        journey["id"]
        for journey in registry["journeys"]
        if any(
            fnmatch.fnmatchcase(path, pattern)
            for path in normalized
            for pattern in journey["path_patterns"]
        )
    }


def validate_receipt_bootstrap_boundary(
    impacted: set[str],
    changed_paths: Iterable[str],
    *registries: dict | None,
) -> None:
    if not impacted:
        return
    protected = set(PROTECTED_RECEIPT_PATHS)
    for registry in registries:
        if registry:
            protected.update(
                evidence["path"]
                for journey in registry["journeys"]
                for evidence in journey["executable_evidence"]
            )
    overlap = sorted(set(changed_paths) & protected)
    if overlap:
        raise RegistryError(
            "receipt infrastructure/evidence must land before an affected product PR: " + ", ".join(overlap)
        )


def _is_runtime_path(path: str) -> bool:
    parts = path.split("/")
    filename = parts[-1]
    return not (
        path.startswith(("docs/", "tests/"))
        or "tests" in parts
        or "migrations" in parts
        or filename == "tests.py"
        or filename.startswith("test_")
        or path.endswith(("_test.py", ".md"))
    )


def parse_pr_evidence(body: str) -> dict[str, dict[str, str]]:
    if MARKER not in body:
        return {}
    sanitized = re.sub(r"```[\s\S]*?```", "", body)
    sanitized = re.sub(r"<!--[\s\S]*?-->", "", sanitized)
    headings = list(re.finditer(r"^### Journey: ([a-z0-9]+(?:-[a-z0-9]+)*)\s*$", sanitized, re.MULTILINE))
    parsed: dict[str, dict[str, str]] = {}
    for index, heading in enumerate(headings):
        journey_id = heading.group(1)
        if journey_id in parsed:
            raise RegistryError(f"duplicate PR evidence block: {journey_id}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(sanitized)
        fields: dict[str, str] = {}
        for match in re.finditer(r"^- ([^:\n]+):\s*(.+?)\s*$", sanitized[heading.end():end], re.MULTILINE):
            label, value = match.groups()
            if label in PR_FIELDS:
                key = PR_FIELDS[label]
                if key in fields:
                    raise RegistryError(f"{journey_id}: duplicate PR field {label}")
                fields[key] = value.strip()
        parsed[journey_id] = fields
    return parsed


def _split_values(value: str) -> set[str]:
    return {item.strip() for item in re.split(r"[,;]", value) if item.strip()}


def _role_outcomes(value: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in value.split(";"):
        if "=" not in item:
            continue
        role, outcome = item.split("=", 1)
        parsed[role.strip()] = outcome.strip()
    return parsed


def validate_pr_evidence(
    registry: dict,
    impacted: set[str],
    parsed: dict[str, dict[str, str]],
    valid_receipts: set[str] | None = None,
) -> None:
    valid_receipts = valid_receipts or set()
    by_id = {journey["id"]: journey for journey in registry["journeys"]}
    missing = sorted(impacted - parsed.keys())
    if missing:
        raise RegistryError(f"missing PR journey evidence blocks: {', '.join(missing)}")
    for journey_id in sorted(impacted):
        journey = by_id[journey_id]
        if journey["availability"] == "generally_available" and journey_id not in valid_receipts:
            detail = (
                "close registered gaps before product change: " + "; ".join(journey["known_gaps"])
                if journey["seal_status"] == "gap"
                else "a sealed journey still requires a fresh receipt for each affected product head"
            )
            raise RegistryError(f"{journey_id}: no valid exact-SHA receipt; {detail}")
        fields = parsed[journey_id]
        for label, key in PR_FIELDS.items():
            if not fields.get(key):
                raise RegistryError(f"{journey_id}: missing PR field {label}")
        for key, label in (
            ("positive_action", "positive action"),
            ("persisted_outcome", "positive persisted outcome"),
            ("reload_persistence", "positive reload persistence"),
            ("downstream_projection", "positive downstream projection"),
            ("invalid_case", "concrete invalid case"),
            ("notification_intent", "concrete notification intent"),
            ("cleanup", "concrete cleanup readback"),
        ):
            if not _positive(fields[key]):
                raise RegistryError(f"{journey_id}: {label} is required; denial or guard-only evidence is insufficient")
        if not set(journey["actors"]) <= _split_values(fields["actors"]):
            raise RegistryError(f"{journey_id}: PR Actors must cover {', '.join(journey['actors'])}")
        if not set(journey["projection_audiences"]) <= _split_values(fields["projection_audiences"]):
            raise RegistryError(
                f"{journey_id}: PR Projection audiences must cover {', '.join(journey['projection_audiences'])}"
            )
        outcomes = _role_outcomes(fields["role_outcomes"])
        if set(outcomes) != CANONICAL_ROLES or not all(_positive(value) for value in outcomes.values()):
            raise RegistryError(f"{journey_id}: PR Role outcomes must give a concrete result for every canonical role")
        if not set(journey["required_viewports"]) <= _split_values(fields["viewports"]):
            raise RegistryError(f"{journey_id}: PR Viewports must cover desktop-1366 and mobile-390")
        known_evidence = {item["path"] for item in journey["executable_evidence"]}
        missing_evidence = sorted(known_evidence - _split_values(fields["executable_evidence"]))
        if missing_evidence:
            raise RegistryError(f"{journey_id}: PR Executable evidence is missing registered paths: {', '.join(missing_evidence)}")
        if fields["provider_sends"] != journey["provider_dispatch_policy"]:
            raise RegistryError(f"{journey_id}: PR Provider sends must be disabled_in_synthetic_qa")


def _changed_paths(root: Path, base_ref: str) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRTD", f"{base_ref}...HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RegistryError(completed.stderr.strip() or f"git diff failed for {base_ref}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _registry_at_ref(root: Path, base_ref: str, relative_path: str) -> dict | None:
    completed = subprocess.run(
        ["git", "show", f"{base_ref}:{relative_path}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"base registry is invalid JSON at {base_ref}:{relative_path}") from exc


def validate_invocation(impacted: set[str], *, event_path: str | None, registry_only: bool) -> None:
    if impacted and not event_path and not registry_only:
        raise RegistryError("impacted journeys require a pull-request event; use --registry-only only for non-PR schema checks")


def validate_receipts(
    receipt_paths: Iterable[str],
    registry: dict,
    root: Path,
    expected_head: str,
    now: dt.datetime,
) -> set[str]:
    paths = list(receipt_paths)
    if not paths:
        return set()
    try:
        context = current_ci_context()
        valid = set()
        for relative in paths:
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError as exc:
                raise RegistryError(f"receipt path escapes the repository: {relative}") from exc
            valid.add(
                validate_receipt(
                    json.loads(candidate.read_text(encoding="utf-8")),
                    registry,
                    root,
                    expected_head,
                    context,
                    now,
                )
            )
    except ReceiptError as exc:
        raise RegistryError(str(exc)) from exc
    if len(valid) != len(paths):
        raise RegistryError("duplicate journey receipts are not allowed")
    return valid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="scripts/codex/user-journey-registry.json")
    parser.add_argument("--base-ref")
    parser.add_argument("--event-path")
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--receipt", action="append", default=[])
    parser.add_argument("--registry-only", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        registry = json.loads((root / args.registry).read_text(encoding="utf-8"))
        validate_registry(registry, root)
        validate_core_journeys(registry)
        changed = list(args.changed_path)
        previous_registry = None
        if args.base_ref:
            changed.extend(_changed_paths(root, args.base_ref))
            previous_registry = _registry_at_ref(root, args.base_ref, args.registry)
        impacted = impacted_journey_ids(registry, changed)
        if previous_registry is not None:
            impacted |= impacted_journey_ids(previous_registry, changed)
        validate_receipt_bootstrap_boundary(impacted, changed, registry, previous_registry)
        validate_invocation(impacted, event_path=args.event_path, registry_only=args.registry_only)
        valid_receipts: set[str] = set()
        if args.event_path:
            event = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
            expected_head = ((event.get("pull_request") or {}).get("head") or {}).get("sha")
            if not isinstance(expected_head, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
                raise RegistryError("pull-request head SHA is missing")
            valid_receipts = validate_receipts(
                args.receipt,
                registry,
                root,
                expected_head,
                dt.datetime.now(dt.timezone.utc),
            )
            if impacted:
                body = (event.get("pull_request") or {}).get("body") or ""
                parsed = parse_pr_evidence(body)
                validate_pr_evidence(registry, impacted, parsed, valid_receipts)
        if not args.registry_only:
            validate_registry_transition(previous_registry, registry, valid_receipts)
        suffix = ",".join(sorted(impacted)) or "none"
        print(f"USER_JOURNEY_OUTCOME_GATE_PASS impacted={suffix}")
        return 0
    except (OSError, json.JSONDecodeError, RegistryError) as exc:
        print(f"USER_JOURNEY_OUTCOME_GATE_FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
