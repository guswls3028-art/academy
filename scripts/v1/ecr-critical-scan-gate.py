#!/usr/bin/env python3
"""Fail closed on unaccepted Critical or regressed High ECR findings."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORIES = {
    "academy-base",
    "academy-api",
    "academy-video-worker",
    "academy-messaging-worker",
    "academy-ai-worker-cpu",
    "academy-tools-worker",
}


class GateError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"JSON root must be an object: {path}")
    return value


def load_acceptances(path: Path, today: date) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    document = _read_json(path)
    if document.get("schemaVersion") != 1:
        raise GateError("critical risk acceptance schemaVersion must be 1")
    entries = document.get("acceptedFindings")
    if not isinstance(entries, list):
        raise GateError("acceptedFindings must be an array")

    accepted: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise GateError(f"acceptedFindings[{index}] must be an object")
        cve = entry.get("cve")
        package = entry.get("packageName")
        version = entry.get("packageVersion")
        repositories = entry.get("repositories")
        expires_raw = entry.get("expiresOn")
        rationale = entry.get("rationale")
        tracker = entry.get("vendorTracker")
        if not isinstance(cve, str) or not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve):
            raise GateError(f"acceptedFindings[{index}] has an invalid CVE")
        if not isinstance(package, str) or not package:
            raise GateError(f"acceptedFindings[{index}] has an invalid packageName")
        if not isinstance(version, str) or not version:
            raise GateError(f"acceptedFindings[{index}] has an invalid packageVersion")
        if not isinstance(repositories, list) or not repositories:
            raise GateError(f"acceptedFindings[{index}] must name repositories")
        if not isinstance(rationale, str) or len(rationale.strip()) < 40:
            raise GateError(f"acceptedFindings[{index}] needs a durable rationale")
        if not isinstance(tracker, str) or not tracker.startswith(
            "https://security-tracker.debian.org/tracker/"
        ):
            raise GateError(f"acceptedFindings[{index}] needs a Debian vendor tracker URL")
        try:
            expires_on = date.fromisoformat(expires_raw)
        except (TypeError, ValueError) as exc:
            raise GateError(f"acceptedFindings[{index}] has an invalid expiresOn") from exc
        if today > expires_on:
            raise GateError(f"critical risk acceptance expired: {cve} expired {expires_on}")

        for repository in repositories:
            if repository not in REPOSITORIES:
                raise GateError(f"acceptedFindings[{index}] names unknown repository {repository}")
            key = (repository, cve, package, version)
            if key in accepted:
                raise GateError(f"duplicate critical risk acceptance: {key}")
            accepted[key] = {**entry, "expiresOn": expires_on.isoformat()}
    return accepted


def load_high_baselines(
    path: Path,
    today: date | None = None,
) -> tuple[dict[str, int], set[tuple[str, str, str, str]]]:
    document = _read_json(path)
    if document.get("schemaVersion") != 3:
        raise GateError("high risk baseline schemaVersion must be 3")
    today = today or datetime.now(timezone.utc).date()
    baselines = document.get("maximumHighFindings")
    if not isinstance(baselines, dict) or set(baselines) != REPOSITORIES:
        raise GateError(
            "maximumHighFindings must contain exactly the six governed repositories"
        )
    for repository, count in baselines.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GateError(f"invalid High finding baseline for {repository}")

    accepted_entries = document.get("acceptedHighFindings")
    if not isinstance(accepted_entries, list):
        raise GateError("acceptedHighFindings must be an array")
    if "knownHighFindings" in document:
        raise GateError(
            "knownHighFindings is not allowed in schemaVersion 3; "
            "every High finding needs reviewed acceptance metadata"
        )

    known: set[tuple[str, str, str, str]] = set()
    for index, entry in enumerate(accepted_entries):
        label = "acceptedHighFindings"
        if not isinstance(entry, dict):
            raise GateError(f"{label}[{index}] must be an object")
        cve = entry.get("cve")
        package = entry.get("packageName")
        version = entry.get("packageVersion")
        repositories = entry.get("repositories")
        rationale = entry.get("rationale")
        tracker = entry.get("vendorTracker")
        expires_raw = entry.get("expiresOn")
        if not isinstance(cve, str) or not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve):
            raise GateError(f"{label}[{index}] has an invalid CVE")
        if not isinstance(package, str) or not package:
            raise GateError(f"{label}[{index}] has an invalid packageName")
        if not isinstance(version, str) or not version:
            raise GateError(f"{label}[{index}] has an invalid packageVersion")
        if not isinstance(repositories, list) or not repositories:
            raise GateError(f"{label}[{index}] must name repositories")
        if not isinstance(rationale, str) or len(rationale.strip()) < 40:
            raise GateError(f"{label}[{index}] needs a durable rationale")
        if not isinstance(tracker, str) or tracker != (
            f"https://security-tracker.debian.org/tracker/{cve}"
        ):
            raise GateError(f"{label}[{index}] needs the exact Debian tracker URL")
        try:
            expires_on = date.fromisoformat(expires_raw)
        except (TypeError, ValueError) as exc:
            raise GateError(f"{label}[{index}] has an invalid expiresOn") from exc
        if today > expires_on:
            raise GateError(f"High risk acceptance expired: {cve} expired {expires_on}")

        for repository in repositories:
            if not isinstance(repository, str) or repository not in REPOSITORIES:
                raise GateError(f"{label}[{index}] names unknown repository {repository}")
            key = (repository, cve, package, version)
            if key in known:
                raise GateError(f"duplicate High finding policy entry: {key}")
            known.add(key)

    for repository, maximum in baselines.items():
        exact_count = sum(key[0] == repository for key in known)
        if exact_count != maximum:
            raise GateError(
                "High finding identity baseline count mismatch "
                f"for {repository}: identities={exact_count} maximum={maximum}"
            )
    return baselines, known


def _run_aws_json(arguments: list[str], *, scan_may_be_absent: bool = False) -> dict[str, Any]:
    process = subprocess.run(
        ["aws", *arguments, "--output", "json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if process.returncode != 0:
        stderr = process.stderr.strip()
        if scan_may_be_absent and "ScanNotFoundException" in stderr:
            return {}
        concise = stderr.splitlines()[-1] if stderr else "unknown AWS CLI error"
        raise GateError(f"AWS ECR command failed: {concise}")
    if not process.stdout.strip():
        return {}
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise GateError("AWS ECR command returned invalid JSON") from exc
    return value if isinstance(value, dict) else {}


def _describe_scan(repository: str, digest: str, region: str) -> dict[str, Any]:
    return _run_aws_json(
        [
            "ecr",
            "describe-image-scan-findings",
            "--repository-name",
            repository,
            "--image-id",
            f"imageDigest={digest}",
            "--region",
            region,
        ],
        scan_may_be_absent=True,
    )


def wait_for_completed_scan(
    repository: str,
    digest: str,
    region: str,
    attempts: int,
    interval_seconds: float,
) -> dict[str, Any]:
    findings = _describe_scan(repository, digest, region)
    status_block = findings.get("imageScanStatus") or {}
    status = status_block.get("status")
    if not status:
        try:
            _run_aws_json(
                [
                    "ecr",
                    "start-image-scan",
                    "--repository-name",
                    repository,
                    "--image-id",
                    f"imageDigest={digest}",
                    "--region",
                    region,
                ]
            )
            print(f"ECR_SCAN_STARTED repo={repository} digest={digest}")
        except GateError as exc:
            if "LimitExceededException" not in str(exc):
                raise
            print(
                f"::warning::ECR scan start quota is already consumed for "
                f"{repository}@{digest}; require the existing scan to complete"
            )

    for attempt in range(1, attempts + 1):
        findings = _describe_scan(repository, digest, region)
        status_block = findings.get("imageScanStatus") or {}
        status = status_block.get("status")
        if status == "COMPLETE":
            return findings
        if status in {"FAILED", "UNSUPPORTED_IMAGE"}:
            raise GateError(
                f"ECR scan failed repo={repository} digest={digest} status={status}"
            )
        if attempt < attempts:
            time.sleep(interval_seconds)
    raise GateError(f"ECR scan did not complete repo={repository} digest={digest}")


def critical_finding_keys(
    repository: str, findings: dict[str, Any]
) -> list[tuple[str, str, str, str]]:
    keys: list[tuple[str, str, str, str]] = []
    findings_block = findings.get("imageScanFindings") or {}
    raw_findings = findings_block.get("findings", [])
    if not isinstance(raw_findings, list):
        raise GateError(f"ECR findings are malformed for {repository}")
    for finding in raw_findings:
        if not isinstance(finding, dict) or finding.get("severity") != "CRITICAL":
            continue
        attributes = {
            item.get("key"): item.get("value")
            for item in finding.get("attributes", [])
            if isinstance(item, dict)
        }
        cve = finding.get("name")
        package = attributes.get("package_name")
        version = attributes.get("package_version")
        if not all(isinstance(value, str) and value for value in (cve, package, version)):
            raise GateError(f"critical ECR finding lacks exact identity for {repository}")
        keys.append((repository, cve, package, version))
    return sorted(set(keys))


def evaluate_findings(
    repository: str,
    findings: dict[str, Any],
    acceptances: dict[tuple[str, str, str, str], dict[str, Any]],
) -> list[tuple[tuple[str, str, str, str], dict[str, Any]]]:
    accepted_live: list[tuple[tuple[str, str, str, str], dict[str, Any]]] = []
    unaccepted: list[tuple[str, str, str, str]] = []
    for key in critical_finding_keys(repository, findings):
        acceptance = acceptances.get(key)
        if acceptance is None:
            unaccepted.append(key)
        else:
            accepted_live.append((key, acceptance))
    if unaccepted:
        detail = ", ".join(f"{cve}/{package}/{version}" for _, cve, package, version in unaccepted)
        raise GateError(f"unaccepted critical ECR findings repo={repository}: {detail}")
    return accepted_live


def evaluate_high_budget(
    repository: str,
    findings: dict[str, Any],
    baselines: dict[str, int],
    known_findings: set[tuple[str, str, str, str]],
) -> int:
    findings_block = findings.get("imageScanFindings") or {}
    counts = findings_block.get("findingSeverityCounts") or {}
    if not isinstance(counts, dict):
        raise GateError(f"ECR severity counts are malformed for {repository}")
    high = counts.get("HIGH", 0)
    if isinstance(high, bool) or not isinstance(high, int) or high < 0:
        raise GateError(f"ECR High finding count is malformed for {repository}")
    maximum = baselines.get(repository)
    if maximum is None:
        raise GateError(f"High finding baseline is missing for {repository}")
    if high > maximum:
        raise GateError(
            f"ECR High findings regressed repo={repository}: high={high} maximum={maximum}"
        )

    observed = set(high_finding_keys(repository, findings))
    if len(observed) != high:
        raise GateError(
            "ECR High severity count does not match exact identities "
            f"for {repository}: high={high} identities={len(observed)}"
        )
    expected = {key for key in known_findings if key[0] == repository}
    unknown = sorted(observed - expected)
    if unknown:
        detail = ", ".join(
            f"{cve}/{package}/{version}"
            for _, cve, package, version in unknown
        )
        raise GateError(
            f"unreviewed High ECR finding identities repo={repository}: {detail}"
        )
    missing = sorted(expected - observed)
    if missing:
        detail = ", ".join(
            f"{cve}/{package}/{version}"
            for _, cve, package, version in missing
        )
        raise GateError(
            f"stale High ECR finding baseline repo={repository}: missing {detail}"
        )
    return high


def high_finding_keys(
    repository: str, findings: dict[str, Any]
) -> list[tuple[str, str, str, str]]:
    keys: list[tuple[str, str, str, str]] = []
    findings_block = findings.get("imageScanFindings") or {}
    raw_findings = findings_block.get("findings", [])
    if not isinstance(raw_findings, list):
        raise GateError(f"ECR findings are malformed for {repository}")
    for finding in raw_findings:
        if not isinstance(finding, dict) or finding.get("severity") != "HIGH":
            continue
        attributes = {
            item.get("key"): item.get("value")
            for item in finding.get("attributes", [])
            if isinstance(item, dict)
        }
        cve = finding.get("name")
        package = attributes.get("package_name")
        version = attributes.get("package_version")
        if not all(isinstance(value, str) and value for value in (cve, package, version)):
            raise GateError(f"High ECR finding lacks exact identity for {repository}")
        keys.append((repository, cve, package, version))
    return sorted(set(keys))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--acceptances", type=Path, required=True)
    parser.add_argument("--high-baseline", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--attempts", type=int, default=40)
    parser.add_argument("--interval-seconds", type=float, default=15)
    args = parser.parse_args()
    if args.attempts < 1 or args.interval_seconds < 0:
        raise GateError("scan retry settings are invalid")

    candidate = _read_json(args.candidate)
    images = candidate.get("images")
    if not isinstance(images, dict) or set(images) != REPOSITORIES:
        raise GateError("release candidate must contain exactly the six governed repositories")
    today = datetime.now(timezone.utc).date()
    acceptances = load_acceptances(args.acceptances, today)
    high_baselines, known_high_findings = load_high_baselines(
        args.high_baseline,
        today,
    )

    for repository in sorted(REPOSITORIES):
        image = images[repository]
        if not isinstance(image, dict):
            raise GateError(f"candidate image entry is malformed: {repository}")
        if image.get("source") not in {"built", "prior-success"}:
            raise GateError(f"candidate image source is invalid: {repository}")
        digest = image.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise GateError(f"candidate digest is invalid: {repository}")
        findings = wait_for_completed_scan(
            repository,
            digest,
            args.region,
            args.attempts,
            args.interval_seconds,
        )
        accepted_live = evaluate_findings(repository, findings, acceptances)
        for (_, cve, package, version), acceptance in accepted_live:
            print(
                "ECR_CRITICAL_ACCEPTED "
                f"repo={repository} cve={cve} package={package} version={version} "
                f"expires={acceptance['expiresOn']}"
            )
        findings_block = findings.get("imageScanFindings") or {}
        counts = findings_block.get("findingSeverityCounts", {})
        critical = counts.get("CRITICAL", 0)
        high = evaluate_high_budget(
            repository,
            findings,
            high_baselines,
            known_high_findings,
        )
        if high:
            print(
                f"::notice::ECR High findings match the exact reviewed baseline for "
                f"{repository}@{digest} (high={high})"
            )
        print(
            f"ECR_SCAN_PASS repo={repository} digest={digest} "
            f"critical={critical} acceptedCritical={len(accepted_live)} high={high}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
