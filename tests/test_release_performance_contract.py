import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "v1-build-and-push-latest.yml"
QUALITY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "quality-gate.yml"

PRODUCTION_DOCKERFILES = {
    "api": REPO_ROOT / "docker" / "api" / "Dockerfile",
    "video": REPO_ROOT / "docker" / "video-worker" / "Dockerfile",
    "messaging": REPO_ROOT / "docker" / "messaging-worker" / "Dockerfile",
    "ai": REPO_ROOT / "docker" / "ai-worker-cpu" / "Dockerfile",
    "tools": REPO_ROOT / "docker" / "tools-worker" / "Dockerfile",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_base_dependency_copy_does_not_duplicate_the_python_layer() -> None:
    dockerfile = _read(REPO_ROOT / "docker" / "Dockerfile.base")

    assert (
        "COPY --from=builder --chown=appuser:appuser "
        "/root/.local /home/appuser/.local"
    ) in dockerfile
    assert "RUN chown -R appuser:appuser /home/appuser" not in dockerfile


def test_selected_base_build_refreshes_apt_packages() -> None:
    dockerfile = _read(REPO_ROOT / "docker" / "Dockerfile.base")
    workflow = _read(WORKFLOW)

    assert dockerfile.count("ARG APT_REFRESH_TOKEN=manual") == 2
    assert dockerfile.count('RUN test -n "${APT_REFRESH_TOKEN}"') == 2
    assert (
        "build-args: APT_REFRESH_TOKEN="
        "${{ github.run_id }}-${{ github.run_attempt }}"
    ) in workflow
    assert 'changed_matches "^docker/native-security/" && BASE=true' in workflow


def test_native_security_changes_build_and_run_the_arm64_base_in_pr() -> None:
    workflow = _read(QUALITY_WORKFLOW)

    assert 'docker/Dockerfile.base docker/native-security/' in workflow
    assert "name: Native security arm64 image contract" in workflow
    assert "platforms: linux/arm64" in workflow
    assert "load: true" in workflow
    assert "academy-base:native-security-check sh -ec" in workflow
    assert "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8" in workflow
    assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in workflow
    assert "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a" in workflow


def test_runtime_dependencies_precede_frequently_changed_source() -> None:
    for service, path in PRODUCTION_DOCKERFILES.items():
        dockerfile = _read(path)
        source_copy = "COPY --chown=appuser:appuser academy ./academy"

        assert source_copy in dockerfile, service
        assert "RUN pip install" in dockerfile, service
        assert dockerfile.index("RUN pip install") < dockerfile.index(source_copy), service


def test_headless_opencv_images_do_not_explicitly_install_system_glib() -> None:
    for service in ("ai", "video"):
        dockerfile = _read(PRODUCTION_DOCKERFILES[service])

        assert "libglib2.0-0" not in dockerfile, service

    ai_requirements = _read(REPO_ROOT / "requirements" / "worker-ai-cpu.txt")
    video_requirements = _read(REPO_ROOT / "requirements" / "worker-video.txt")
    assert "opencv-python-headless" in ai_requirements
    assert "opencv-python-headless" in video_requirements
    assert "OpenCV wheel video support OK" in _read(PRODUCTION_DOCKERFILES["ai"])


def test_runtime_images_remove_unused_perl_after_their_final_apt_layer() -> None:
    required_checks = (
        "dpkg --purge --force-remove-essential perl-base",
        "dpkg --audit",
        "apt-get check",
        "! dpkg-query -W perl-base",
        "for package in perl 'perl-modules-*' 'libperl*' libdpkg-perl liberror-perl",
        "! command -v perl",
    )

    base_dockerfile = _read(REPO_ROOT / "docker" / "Dockerfile.base")
    assert base_dockerfile.rfind("apt-get install") < base_dockerfile.rfind(
        "dpkg --purge --force-remove-essential perl-base"
    )
    for check in required_checks:
        assert check in base_dockerfile

    for service in ("api", "video", "ai", "tools"):
        dockerfile = _read(PRODUCTION_DOCKERFILES[service])
        runtime_dockerfile = (
            dockerfile.split("FROM ${BASE_IMAGE} AS base", maxsplit=1)[1]
            if service == "video"
            else dockerfile
        )
        apt_layers = re.findall(
            r"RUN apt-get update.*?(?=\n\n)", runtime_dockerfile, flags=re.DOTALL
        )

        assert apt_layers, service
        for apt_layer in apt_layers:
            assert "perl-base \\" in apt_layer, service
            assert apt_layer.index("perl-base \\") < apt_layer.index(
                "dpkg --configure -a"
            ), service
            assert apt_layer.index("dpkg --configure -a") < apt_layer.index(
                "dpkg --purge --force-remove-essential perl-base"
            ), service
            assert "apt-get autoremove -y --purge" in apt_layer, service
            assert "apt-get clean" in apt_layer, service
            for check in required_checks:
                assert check in apt_layer, service

    video_dockerfile = _read(PRODUCTION_DOCKERFILES["video"])
    assert [
        line for line in video_dockerfile.splitlines() if line.startswith("COPY --from=")
    ] == ["COPY --from=ffmpeg-builder /opt/academy-ffmpeg /opt/academy-ffmpeg"]

    messaging_dockerfile = _read(PRODUCTION_DOCKERFILES["messaging"])
    assert "FROM ${BASE_IMAGE}" in messaging_dockerfile
    assert "apt-get install" not in messaging_dockerfile


def test_reviewed_runtime_images_own_exact_high_budgets() -> None:
    document = json.loads(
        _read(REPO_ROOT / "docs" / "ssot" / "ecr-high-risk-baseline.json")
    )
    baseline = document["maximumHighFindings"]

    assert document["schemaVersion"] == 3
    assert baseline == {
        "academy-base": 3,
        "academy-api": 16,
        "academy-video-worker": 3,
        "academy-messaging-worker": 3,
        "academy-ai-worker-cpu": 16,
        "academy-tools-worker": 16,
    }
    exact_counts = {repository: 0 for repository in baseline}
    assert "knownHighFindings" not in document
    assert all(
        finding["vendorTracker"]
        == f"https://security-tracker.debian.org/tracker/{finding['cve']}"
        and finding["expiresOn"]
        and len(finding["rationale"].strip()) >= 40
        for finding in document["acceptedHighFindings"]
    )
    for finding in document["acceptedHighFindings"]:
        for repository in finding["repositories"]:
            exact_counts[repository] += 1
    assert exact_counts == baseline
    assert all(
        finding["packageName"] != "perl"
        for finding in document["acceptedHighFindings"]
    )

    critical_document = json.loads(
        _read(REPO_ROOT / "docs" / "ssot" / "ecr-critical-risk-acceptance.json")
    )
    assert all(
        finding["packageName"] != "perl"
        for finding in critical_document["acceptedFindings"]
    )


def test_runtime_images_build_in_parallel_before_candidate_assembly() -> None:
    workflow = _read(WORKFLOW)
    prepare = workflow.split("\n  prepare-build:\n", maxsplit=1)[1].split(
        "\n  build-runtime-images:\n", maxsplit=1
    )[0]
    runtime_build = workflow.split("\n  build-runtime-images:\n", maxsplit=1)[1].split(
        "\n  build-and-push:\n", maxsplit=1
    )[0]
    assembly = workflow.split("\n  build-and-push:\n", maxsplit=1)[1].split(
        "\n  verify-api-development:\n", maxsplit=1
    )[0]

    assert "base_image_uri: ${{ steps.base-image.outputs.uri }}" in prepare
    assert "strategy:" in runtime_build
    assert "fail-fast: false" in runtime_build
    assert "max-parallel: 5" in runtime_build
    assert runtime_build.count("repository: academy-") == 5
    assert runtime_build.count("uses: docker/build-push-action@") == 1
    assert "build-args: BASE_IMAGE=${{ needs.prepare-build.outputs.base_image_uri }}" in runtime_build
    assert "needs: [detect-changes, prepare-build, build-runtime-images]" in assembly
    assert "Gate newly built images on completed ECR critical scan" in assembly


def test_production_source_copies_have_final_ownership() -> None:
    for service, path in PRODUCTION_DOCKERFILES.items():
        for line in _read(path).splitlines():
            stripped = line.strip()
            if stripped.startswith(("COPY academy", "COPY apps", "COPY libs")):
                raise AssertionError(f"{service} has an ownership-copy regression: {line}")
            if stripped.startswith(("COPY manage.py", "COPY scripts")):
                raise AssertionError(f"{service} has an ownership-copy regression: {line}")


def test_api_refresh_preserves_capacity_and_uses_native_replacement_headroom() -> None:
    workflow = _read(WORKFLOW)
    deploy_api = workflow.split("\n  deploy-api:\n", maxsplit=1)[1].split(
        "\n  deploy-messaging:\n", maxsplit=1
    )[0]

    assert "Ensure API ASG deploy headroom" not in deploy_api
    assert "--min-size" not in deploy_api
    assert "--desired-capacity" not in deploy_api
    assert "Ensure API ASG launch-before-terminate ceiling" in deploy_api
    assert '\\"MinHealthyPercentage\\":100' in deploy_api
    assert '\\"MaxHealthyPercentage\\":200' in deploy_api
    assert '\\"InstanceWarmup\\":${API_INSTANCE_WARMUP}' in deploy_api
    assert "Restore API ASG max-size ceiling" in deploy_api
    assert deploy_api.index("Restore API ASG max-size ceiling") < deploy_api.index(
        "Compensate failed API pin or refresh"
    )


def test_api_refresh_max_healthy_percentage_is_in_the_runtime_ssot() -> None:
    params = _read(REPO_ROOT / "docs" / "ssot" / "params.yaml")
    loader = _read(REPO_ROOT / "scripts" / "v1" / "core" / "ssot.ps1")
    manual_refresh = _read(
        REPO_ROOT / "scripts" / "v1" / "deploy-api-and-verify-workers.ps1"
    )

    assert "instanceRefreshMaxHealthyPercentage: 200" in params
    assert "ApiInstanceRefreshMaxHealthyPercentage" in loader
    assert "ApiInstanceRefreshMaxHealthyPercentage" in manual_refresh
    assert "$refreshRequiredMax = $refreshDesired + 1" in manual_refresh
    assert '"--max-size", [string]$refreshRequiredMax' in manual_refresh
    assert '"--max-size", [string]$refreshOriginalMax' in manual_refresh
    assert "API refresh ceiling readback mismatch after restore" in manual_refresh


def test_api_compensation_refresh_uses_and_restores_native_headroom() -> None:
    compensation = _read(REPO_ROOT / "scripts" / "v1" / "pin-asg-image.ps1")

    assert "$script:ApiInstanceRefreshMinHealthyPercentage" in compensation
    assert "$script:ApiInstanceRefreshMaxHealthyPercentage" in compensation
    assert "$script:ApiInstanceRefreshInstanceWarmup" in compensation
    assert "$requiredMaxSize = $desiredCapacity + 1" in compensation
    assert "restore $Service compensation refresh ceiling" in compensation
    assert "$Service compensation refresh ceiling readback mismatch" in compensation
