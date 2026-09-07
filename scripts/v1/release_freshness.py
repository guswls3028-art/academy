#!/usr/bin/env python3
"""Fail closed when a release candidate is stale or not based on current main."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_MAIN_ADVANCE_PATHS = frozenset({
    "docs/reports/ci-build.latest.md",
    "docs/reports/release-manifest.latest.json",
})


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "git merge-base failed")
    return result.returncode == 0


def _changed_paths(repo: Path, older: str, newer: str) -> set[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{older}..{newer}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}


def classify_candidate(
    repo: Path,
    candidate_sha: str,
    deployed_sha: str,
    main_sha: str | None = None,
) -> str:
    candidate = candidate_sha.strip().lower()
    deployed = deployed_sha.strip().lower()
    main = (main_sha or candidate).strip().lower()
    if not all(SHA_RE.fullmatch(sha) for sha in (candidate, deployed, main)):
        raise ValueError("candidate, deployed, and main SHAs must be full 40-character hex values")
    if candidate != main:
        if not _is_ancestor(repo, candidate, main):
            return "off-main"
        if not _changed_paths(repo, candidate, main).issubset(ALLOWED_MAIN_ADVANCE_PATHS):
            return "behind-main"
    if candidate == deployed:
        return "same"
    if _is_ancestor(repo, deployed, candidate):
        return "forward"
    if _is_ancestor(repo, candidate, deployed):
        return "stale"
    return "divergent"


def classify_development_candidate(
    repo: Path,
    candidate_sha: str,
    main_sha: str,
) -> str:
    """Allow an unreleased development candidate only when current main is its ancestor."""
    candidate = candidate_sha.strip().lower()
    main = main_sha.strip().lower()
    if not all(SHA_RE.fullmatch(sha) for sha in (candidate, main)):
        raise ValueError("candidate and main SHAs must be full 40-character hex values")
    if candidate == main:
        return "main"
    if _is_ancestor(repo, main, candidate):
        return "main-descendant"
    if _is_ancestor(repo, candidate, main):
        return "behind-main"
    return "off-main"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--main-sha", required=True)
    parser.add_argument(
        "--deployment-scope",
        choices=("production", "development"),
        default="production",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.deployment_scope == "development":
            classification = classify_development_candidate(
                args.repo.resolve(),
                args.candidate_sha,
                args.main_sha,
            )
        else:
            classification = classify_candidate(
                args.repo.resolve(),
                args.candidate_sha,
                args.deployed_sha,
                args.main_sha,
            )
    except (RuntimeError, ValueError) as exc:
        print(f"[release-freshness] unable to prove freshness: {exc}", file=sys.stderr)
        return 2

    print(
        "[release-freshness] "
        f"candidate={args.candidate_sha} deployed={args.deployed_sha} "
        f"main={args.main_sha} scope={args.deployment_scope} "
        f"classification={classification}"
    )
    allowed = (
        {"main", "main-descendant"}
        if args.deployment_scope == "development"
        else {"same", "forward"}
    )
    if classification in allowed:
        return 0
    print(
        "[release-freshness] refusing stale, divergent, or unbased candidate",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
