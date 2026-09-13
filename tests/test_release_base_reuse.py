"""Execute the release workflow's actual classification block without AWS writes."""

from __future__ import annotations

import os
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/v1-build-and-push-latest.yml"


def classify(tmp_path: Path, *, base: str = "", runtime: str = "", runtime_diffs=None) -> dict[str, str]:
    source = WORKFLOW.read_text(encoding="utf-8")
    block = source.split("          changed_matches()", maxsplit=1)[1]
    block = "          changed_matches()" + block
    block = block.split("\n  # ===", maxsplit=1)[0]
    # The next job is outside the 10-space run block. Do not execute later jobs.
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    script = textwrap.dedent("\n".join(lines))
    output = tmp_path / "outputs"
    env = dict(os.environ, GITHUB_OUTPUT=output.as_posix())
    env["CHANGED_BASE"] = base
    for name in ("RELEASE", "API", "VIDEO", "MSG", "AI", "TOOLS"):
        env[f"CHANGED_{name}"] = (runtime_diffs or {}).get(name, runtime)
    # This is the actual union above changed_matches, including the base-diff
    # distinction under test, not a Python copy of the classification policy.
    union = source.split("          CHANGED=$(printf", maxsplit=1)[1].split("          # Avoid echo|grep", maxsplit=1)[
        0
    ]
    full_build = source.split("          force_full_build() {", maxsplit=1)[1].split("          if ! jq", maxsplit=1)[0]
    script = (
        textwrap.dedent("          force_full_build() {" + full_build)
        + textwrap.dedent("          CHANGED=$(printf" + union)
        + script
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail"],
        input=script,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize(
    "changed",
    [
        "academy/application/use_cases/student_video_access_context.py",
        "libs/queue/client.py",
        "apps/core/models.py",
        "apps/shared/example.py",
        "apps/support/video/example.py",
        "apps/infrastructure/storage/r2.py",
        "apps/api/common/example.py",
        "apps/api/config/settings/worker.py",
        "apps/domains/students/models/profile.py",
        "manage.py",
        "docs/ssot/params.yaml",
    ],
)
def test_common_code_rebuilds_all_runtimes_without_recompiling_base(tmp_path, changed):
    outputs = classify(tmp_path, base=changed, runtime=changed)
    assert outputs == {
        "build_all": "false",
        "build_base": "false",
        "build_api": "true",
        "build_video": "true",
        "build_messaging": "true",
        "build_ai": "true",
        "build_tools": "true",
    }


@pytest.mark.parametrize(
    "changed",
    [
        ".dockerignore",
        "docker/Dockerfile.base",
        "docker/native-security/build-fixed-libs.sh",
        "requirements/common.txt",
        "requirements/constraints.txt",
    ],
)
def test_changed_base_input_rebuilds_every_consumer(tmp_path, changed):
    outputs = classify(tmp_path, base=changed, runtime=changed)
    assert set(outputs.values()) == {"true"}
    assert len(outputs) == 7


def test_previous_base_source_does_not_reintroduce_already_shipped_app_changes(tmp_path):
    outputs = classify(tmp_path, base="academy/domain/old_shipped.py")
    assert set(outputs.values()) == {"false"}
    assert len(outputs) == 7


def test_api_patch_after_all_runtimes_shipped_shared_change_does_not_rebuild_workers(tmp_path):
    outputs = classify(
        tmp_path, base="academy/domain/old_shipped.py\ndocker/api/Dockerfile", runtime="docker/api/Dockerfile"
    )
    assert [key for key, value in outputs.items() if value == "true"] == ["build_api"]


def test_lagged_messaging_shared_change_is_not_hidden_by_newer_release(tmp_path):
    outputs = classify(tmp_path, runtime_diffs={"MSG": "academy/domain/unshipped.py"})
    assert outputs["build_base"] == "false"
    assert all(outputs[f"build_{name}"] == "true" for name in ("api", "video", "messaging", "ai", "tools"))


@pytest.mark.parametrize(
    "changed",
    [
        ".dockerignore",
        "docker/Dockerfile.base",
        "docker/native-security/build-fixed-libs.sh",
        "requirements/constraints.txt",
        "requirements/common.txt",
    ],
)
def test_already_promoted_base_still_catches_up_a_lagged_runtime(tmp_path, changed):
    outputs = classify(tmp_path, runtime_diffs={"MSG": changed})
    assert outputs["build_base"] == "false"
    assert all(outputs[f"build_{name}"] == "true" for name in ("api", "video", "messaging", "ai", "tools"))


def test_unshipped_base_change_still_rebuilds_all_when_runtime_diff_is_empty(tmp_path):
    outputs = classify(tmp_path, base="docker/native-security/build-fixed-libs.sh")
    assert set(outputs.values()) == {"true"}


def test_api_only_input_keeps_selective_builds(tmp_path):
    outputs = classify(tmp_path, runtime="docker/api/Dockerfile")
    assert outputs["build_api"] == "true"
    assert [key for key, value in outputs.items() if value == "true"] == ["build_api"]


SPEC = importlib.util.spec_from_file_location("resolve_release_base", ROOT / "scripts/v1/resolve_release_base.py")
assert SPEC and SPEC.loader
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)
DIGEST = "sha256:" + "a" * 64
TAG = "sha-" + "b" * 40 + "-run-123-1"


def baseline():
    return {
        "schemaVersion": 1,
        "complete": True,
        "status": "successful",
        "images": {"academy-base": {"digest": DIGEST, "tag": TAG}},
    }


def response(*, digest=DIGEST, tags=None):
    return {
        "imageDetails": [
            {"repositoryName": "academy-base", "imageDigest": digest, "imageTags": [TAG] if tags is None else tags}
        ]
    }


def fake_ecr(monkeypatch, *, result=None, error=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 60
        return SimpleNamespace(
            returncode=1 if error else 0, stderr=error, stdout=json.dumps(response() if result is None else result)
        )

    monkeypatch.setattr(resolver.subprocess, "run", run)
    return calls


def test_reuse_uses_successful_immutable_digest_and_tag_not_latest(monkeypatch):
    calls = fake_ecr(monkeypatch)
    assert resolver.requires_rebuild(baseline(), "ap-northeast-2") is False
    assert calls == [
        [
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            "academy-base",
            "--image-ids",
            f"imageDigest={DIGEST}",
            "--region",
            "ap-northeast-2",
            "--output",
            "json",
        ]
    ]


@pytest.mark.parametrize("code", ["ImageNotFoundException", "RepositoryNotFoundException"])
def test_confirmed_missing_base_selects_official_full_rebuild(monkeypatch, code):
    fake_ecr(monkeypatch, error=f"An error occurred ({code}) when calling DescribeImages")
    assert resolver.requires_rebuild(baseline(), "ap-northeast-2") is True


@pytest.mark.parametrize(
    "error",
    [
        "An error occurred (AccessDeniedException)",
        "Unable to locate credentials",
        "Could not connect to the endpoint URL",
        "Unknown error mentioning ImageNotFoundException",
    ],
)
def test_unknown_or_authorization_failures_do_not_become_rebuild_success(monkeypatch, error):
    fake_ecr(monkeypatch, error=error)
    with pytest.raises(resolver.BaseIdentityError, match="identity is unverified"):
        resolver.requires_rebuild(baseline(), "ap-northeast-2")


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"imageDetails": []},
        {"imageDetails": [None]},
        {"imageDetails": [{}, {}]},
        response(digest="sha256:" + "c" * 64),
        response(tags=["latest"]),
    ],
)
def test_ambiguous_or_drifted_identity_is_blocked(monkeypatch, result):
    fake_ecr(monkeypatch, result=result)
    with pytest.raises(resolver.BaseIdentityError):
        resolver.requires_rebuild(baseline(), "ap-northeast-2")


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "candidate"},
        {"complete": False},
        {"schemaVersion": 2},
        {"images": {}},
        {"images": []},
        {"images": {"academy-base": {"digest": "latest", "tag": TAG}}},
    ],
)
def test_invalid_baseline_is_not_an_ecr_reuse_candidate(monkeypatch, patch):
    calls = fake_ecr(monkeypatch)
    with pytest.raises(resolver.BaseIdentityError):
        resolver.requires_rebuild({**baseline(), **patch}, "ap-northeast-2")
    assert not calls


def test_confirmed_missing_base_propagates_to_all_existing_build_and_deploy_flags():
    source = WORKFLOW.read_text(encoding="utf-8")
    outputs = source.split("    outputs:", maxsplit=1)[1].split("    steps:", maxsplit=1)[0]
    for name in ("base", "api", "video", "messaging", "ai", "tools"):
        assert (
            f"build_{name}: ${{{{ steps.base-reuse.outputs.rebuild == 'true' || "
            f"steps.changes.outputs.build_{name} == 'true' }}}}"
        ) in outputs
    assert (
        "force_full: ${{ steps.base-reuse.outputs.rebuild == 'true' || steps.changes.outputs.build_all == 'true' }}"
    ) in outputs


@pytest.mark.parametrize(
    "error, expected",
    [
        ("", "rebuild=false\n"),
        ("An error occurred (ImageNotFoundException)", "rebuild=true\n"),
        ("An error occurred (AccessDeniedException)", None),
    ],
)
def test_cli_emits_a_decision_only_after_exact_identity_read(tmp_path, monkeypatch, error, expected):
    prior = tmp_path / "baseline.json"
    prior.write_text(json.dumps(baseline()), encoding="utf-8")
    output = tmp_path / "github-output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resolve_release_base.py",
            "--baseline",
            str(prior),
            "--region",
            "ap-northeast-2",
            "--github-output",
            str(output),
        ],
    )
    fake_ecr(monkeypatch, error=error)
    assert resolver.main() == (1 if expected is None else 0)
    if expected is None:
        assert not output.exists()
    else:
        assert output.read_text(encoding="utf-8") == expected


def test_ecr_timeout_is_not_a_confirmed_missing_image(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("aws", 60)

    monkeypatch.setattr(resolver.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        resolver.requires_rebuild(baseline(), "ap-northeast-2")
