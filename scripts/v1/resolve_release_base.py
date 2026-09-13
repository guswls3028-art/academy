#!/usr/bin/env python3
"""Read an exact successful base identity; only confirmed absence selects rebuild."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


class BaseIdentityError(RuntimeError):
    pass


def requires_rebuild(baseline: dict, region: str) -> bool:
    # Source ancestry and unchanged Docker inputs are established by the
    # preceding workflow detector using this same captured baseline artifact.
    if (
        baseline.get("schemaVersion") != 1
        or baseline.get("complete") is not True
        or baseline.get("status") != "successful"
    ):
        raise BaseIdentityError("Base reuse requires a complete successful baseline")
    images = baseline.get("images")
    if not isinstance(images, dict):
        raise BaseIdentityError("Successful baseline images must be an object")
    image = images.get("academy-base")
    if not isinstance(image, dict):
        raise BaseIdentityError("Successful baseline has no academy-base identity")
    digest, tag = image.get("digest"), image.get("tag")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise BaseIdentityError("Successful base digest is invalid")
    if not isinstance(tag, str) or not re.fullmatch(r"sha-[0-9a-f]{8,40}(?:-run-.+)?", tag):
        raise BaseIdentityError("Successful base source tag is invalid")

    response = subprocess.run(
        [
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            "academy-base",
            "--image-ids",
            f"imageDigest={digest}",
            "--region",
            region,
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if response.returncode:
        # Do not confuse lack of read permission, timeouts, or malformed output
        # with a missing dependency, and do not print raw credential-bearing CLI
        # errors. This read-only path never starts a scan or mutates ECR.
        if re.search(r"\((ImageNotFoundException|RepositoryNotFoundException)\)", response.stderr):
            print("BASE_REBUILD_REQUIRED reason=confirmed_ecr_absence")
            return True
        raise BaseIdentityError("ECR base read failed; identity is unverified")
    try:
        details = json.loads(response.stdout)["imageDetails"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BaseIdentityError("ECR base identity response is malformed") from exc
    if not isinstance(details, list) or len(details) != 1 or not isinstance(details[0], dict):
        raise BaseIdentityError("ECR base identity is not unique")
    actual = details[0]
    if (
        actual.get("repositoryName") != "academy-base"
        or actual.get("imageDigest") != digest
        or not isinstance(actual.get("imageTags"), list)
        or tag not in actual["imageTags"]
    ):
        raise BaseIdentityError("ECR base identity differs from the successful baseline")
    print(f"BASE_REUSE_IDENTITY_PASS digest={digest}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if not isinstance(baseline, dict):
            raise BaseIdentityError("Successful baseline must be an object")
        rebuild = requires_rebuild(baseline, args.region)
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"rebuild={str(rebuild).lower()}\n")
    except (BaseIdentityError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        # Exact gate errors are deliberately PII/credential-free.
        message = str(exc) if isinstance(exc, BaseIdentityError) else "Base identity verification failed"
        print(f"::error::{message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
