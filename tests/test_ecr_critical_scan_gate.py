from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "v1" / "ecr-critical-scan-gate.py"
SPEC = importlib.util.spec_from_file_location("ecr_critical_scan_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _finding(cve: str, package: str, version: str, severity: str = "CRITICAL") -> dict:
    return {
        "name": cve,
        "severity": severity,
        "attributes": [
            {"key": "package_name", "value": package},
            {"key": "package_version", "value": version},
        ],
    }


def _scan(*findings: dict) -> dict:
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "")
        counts[severity] = counts.get(severity, 0) + 1
    return {
        "imageScanFindings": {
            "findings": list(findings),
            "findingSeverityCounts": counts,
        }
    }


def _high_policy_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "cve": "CVE-2099-9999",
        "packageName": "demo",
        "packageVersion": "1",
        "repositories": ["academy-api"],
        "expiresOn": "2099-12-31",
        "vendorTracker": "https://security-tracker.debian.org/tracker/CVE-2099-9999",
        "rationale": (
            "This test-only finding has a reviewed non-reachable runtime path and "
            "a bounded acceptance used solely to exercise the fail-closed schema."
        ),
    }
    entry.update(overrides)
    return entry


def _high_policy_document(*entries: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 3,
        "maximumHighFindings": {
            repository: sum(
                repository in entry.get("repositories", []) for entry in entries
            )
            for repository in gate.REPOSITORIES
        },
        "acceptedHighFindings": list(entries),
    }


def test_current_acceptance_is_exact_and_time_bounded() -> None:
    acceptances = gate.load_acceptances(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-critical-risk-acceptance.json",
        date(2026, 7, 31),
    )
    accepted = gate.evaluate_findings(
        "academy-api",
        _scan(_finding("CVE-2026-5450", "glibc", "2.41-12+deb13u3")),
        acceptances,
    )
    assert len(accepted) == 1
    assert {acceptance["expiresOn"] for _, acceptance in accepted} == {
        "2026-09-19"
    }


def test_removed_perl_findings_fail_closed() -> None:
    acceptances = gate.load_acceptances(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-critical-risk-acceptance.json",
        date(2026, 7, 31),
    )
    with pytest.raises(gate.GateError, match="unaccepted critical ECR findings"):
        gate.evaluate_findings(
            "academy-base",
            _scan(
                _finding("CVE-2026-12087", "perl", "5.40.1-6"),
                _finding("CVE-2026-13221", "perl", "5.40.1-6"),
                _finding("CVE-2026-57433", "perl", "5.40.1-6"),
            ),
            acceptances,
        )


@pytest.mark.parametrize(
    "cve", ["CVE-2026-8924", "CVE-2026-8927"]
)
def test_fixed_curl_critical_findings_cannot_be_accepted(cve: str) -> None:
    acceptances = gate.load_acceptances(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-critical-risk-acceptance.json",
        date(2026, 9, 5),
    )

    for repository in (
        "academy-api",
        "academy-ai-worker-cpu",
        "academy-tools-worker",
    ):
        with pytest.raises(gate.GateError, match="unaccepted critical"):
            gate.evaluate_findings(
                repository,
                _scan(_finding(cve, "curl", "8.14.1-2+deb13u4")),
                acceptances,
            )


@pytest.mark.parametrize(
    ("cve", "package", "version"),
    [
        ("CVE-2099-9999", "glibc", "2.41-12+deb13u3"),
        ("CVE-2026-5450", "glibc", "2.41-12+deb13u4"),
        ("CVE-2026-5450", "other", "2.41-12+deb13u3"),
    ],
)
def test_unknown_or_changed_critical_finding_fails_closed(
    cve: str, package: str, version: str
) -> None:
    acceptances = gate.load_acceptances(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-critical-risk-acceptance.json",
        date(2026, 7, 31),
    )
    with pytest.raises(gate.GateError, match="unaccepted critical"):
        gate.evaluate_findings(
            "academy-api", _scan(_finding(cve, package, version)), acceptances
        )


def test_expired_acceptance_blocks_before_scanning() -> None:
    with pytest.raises(gate.GateError, match="expired"):
        gate.load_acceptances(
            Path(__file__).parents[1]
            / "docs"
            / "ssot"
            / "ecr-critical-risk-acceptance.json",
            date(2026, 9, 20),
        )


def test_retired_mbedtls_critical_findings_fail_closed() -> None:
    acceptances = gate.load_acceptances(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-critical-risk-acceptance.json",
        date(2026, 8, 20),
    )

    with pytest.raises(gate.GateError, match="unaccepted critical"):
        gate.evaluate_findings(
            "academy-api",
            _scan(_finding("CVE-2026-34872", "mbedtls", "3.6.5-0.1~deb13u1")),
            acceptances,
        )


def test_high_finding_does_not_consume_critical_acceptance() -> None:
    accepted = gate.evaluate_findings(
        "academy-api", _scan(_finding("CVE-2099-9999", "demo", "1", "HIGH")), {}
    )
    assert accepted == []


def test_high_baseline_is_exact_and_allows_non_increase() -> None:
    baselines, known = gate.load_high_baselines(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-high-risk-baseline.json",
        date(2026, 8, 23),
    )
    findings = _scan(
        *(
            _finding(cve, package, version, "HIGH")
            for repository, cve, package, version in sorted(known)
            if repository == "academy-base"
        )
    )

    assert gate.evaluate_high_budget("academy-base", findings, baselines, known) == 3


def test_current_high_acceptances_are_exact_and_time_bounded() -> None:
    path = Path(__file__).parents[1] / "docs" / "ssot" / "ecr-high-risk-baseline.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    accepted = document["acceptedHighFindings"]

    libssh2 = [entry for entry in accepted if entry["packageName"] == "libssh2"]
    glib = [entry for entry in accepted if entry["packageName"] == "glib2.0"]
    openssl = [entry for entry in accepted if entry["packageName"] == "openssl"]
    assert len(accepted) == 16
    assert {entry["cve"] for entry in libssh2} == {
        "CVE-2026-58050",
        "CVE-2026-58051",
        "CVE-2026-66032",
        "CVE-2026-66033",
        "CVE-2026-66034",
        "CVE-2026-66035",
    }
    assert {entry["expiresOn"] for entry in accepted} == {"2026-09-19"}
    assert all(
        entry["repositories"]
        == ["academy-api", "academy-ai-worker-cpu", "academy-tools-worker"]
        for entry in libssh2
    )
    assert {entry["cve"] for entry in glib} == {
        "CVE-2026-16118",
        "CVE-2026-58010",
        "CVE-2026-58011",
        "CVE-2026-58012",
        "CVE-2026-58013",
        "CVE-2026-58014",
        "CVE-2026-58015",
    }
    assert all(
        entry["repositories"]
        == ["academy-api", "academy-ai-worker-cpu", "academy-tools-worker"]
        for entry in glib
    )
    assert openssl == []
    assert all(
        entry["vendorTracker"]
        == f"https://security-tracker.debian.org/tracker/{entry['cve']}"
        and len(entry["rationale"].strip()) >= 40
        for entry in accepted
    )
    assert {
        (entry["cve"], entry["packageName"], entry["packageVersion"])
        for entry in accepted
    } == {
        ("CVE-2026-11822", "sqlite3", "3.46.1-7+deb13u1"),
        ("CVE-2026-11824", "sqlite3", "3.46.1-7+deb13u1"),
        ("CVE-2026-5928", "glibc", "2.41-12+deb13u3"),
        ("CVE-2026-58010", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-58011", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-58012", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-58013", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-58014", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-58015", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-16118", "glib2.0", "2.84.4-3~deb13u3"),
        ("CVE-2026-58050", "libssh2", "1.11.1-1+deb13u1"),
        ("CVE-2026-58051", "libssh2", "1.11.1-1+deb13u1"),
        ("CVE-2026-66032", "libssh2", "1.11.1-1+deb13u1"),
        ("CVE-2026-66033", "libssh2", "1.11.1-1+deb13u1"),
        ("CVE-2026-66034", "libssh2", "1.11.1-1+deb13u1"),
        ("CVE-2026-66035", "libssh2", "1.11.1-1+deb13u1"),
    }

    baselines, known = gate.load_high_baselines(path, date(2026, 8, 23))
    api_findings = _scan(
        *(
            _finding(cve, package, version, "HIGH")
            for repository, cve, package, version in sorted(known)
            if repository == "academy-api"
        )
    )
    assert gate.evaluate_high_budget("academy-api", api_findings, baselines, known) == 16
    tools_findings = _scan(
        *(
            _finding(cve, package, version, "HIGH")
            for repository, cve, package, version in sorted(known)
            if repository == "academy-tools-worker"
        )
    )
    assert (
        gate.evaluate_high_budget(
            "academy-tools-worker",
            tools_findings,
            baselines,
            known,
        )
        == 16
    )


def test_expired_high_acceptance_blocks_before_scanning() -> None:
    with pytest.raises(gate.GateError, match="High risk acceptance expired"):
        gate.load_high_baselines(
            Path(__file__).parents[1]
            / "docs"
            / "ssot"
            / "ecr-high-risk-baseline.json",
            date(2026, 9, 20),
        )


def test_high_acceptance_remains_valid_through_expiry_day() -> None:
    baselines, reviewed = gate.load_high_baselines(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-high-risk-baseline.json",
        date(2026, 9, 19),
    )
    assert baselines["academy-api"] == 16
    assert len([key for key in reviewed if key[0] == "academy-api"]) == 16


def test_base_image_requires_security_fixed_openssl() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "docker" / "Dockerfile.base"
    ).read_text(encoding="utf-8")

    assert "openssl \\" in dockerfile
    assert 'dpkg --compare-versions "$openssl_version" ge "3.5.7-1~deb13u2"' in dockerfile


def test_base_image_requires_security_fixed_util_linux() -> None:
    dockerfile = (
        Path(__file__).parents[1] / "docker" / "Dockerfile.base"
    ).read_text(encoding="utf-8")

    assert "util-linux \\" in dockerfile
    assert (
        'dpkg --compare-versions "$util_linux_version" ge "2.41.5-0+deb13u1"'
        in dockerfile
    )


def test_base_image_backports_new_native_library_fixes_without_acceptance() -> None:
    repository = Path(__file__).parents[1]
    dockerfile = (repository / "docker" / "Dockerfile.base").read_text(
        encoding="utf-8"
    )
    build_script = (
        repository / "docker" / "native-security" / "build-fixed-libs.sh"
    ).read_text(encoding="utf-8")
    verifier = (
        repository / "docker" / "native-security" / "verify-fixed-libs.sh"
    ).read_text(encoding="utf-8")
    baseline = json.loads(
        (repository / "docs" / "ssot" / "ecr-high-risk-baseline.json").read_text(
            encoding="utf-8"
        )
    )

    assert not {
        "CVE-2026-85091",
        "CVE-2026-86140",
        "CVE-2026-86145",
    } & {entry["cve"] for entry in baseline["acceptedHighFindings"]}
    assert "COPY docker/native-security/build-fixed-libs.sh" in dockerfile
    assert "sh /usr/local/bin/build-fixed-libs.sh /build/native-security" in dockerfile
    assert "dpkg -i /tmp/academy-native-security/*.deb" in dockerfile
    for build_tool in ("autoconf", "automake", "libtool"):
        assert f"    {build_tool} \\" in dockerfile
    for package, minimum in (
        ("zlib1g", "1:1.3.3+really1.3.2.1+academy.git20260904.e3dc0a8-1"),
        ("libpcre2-8-0", "10.48-2~academy1"),
        ("libxml2", "2.15.4+really2.9.14-2.1+deb13u3+academy1"),
    ):
        assert f"dpkg-query -W -f='${{Version}}' {package}" in dockerfile
        assert f'ge "{minimum}"' in dockerfile
    for cve in ("CVE-2026-85091", "CVE-2026-86140", "CVE-2026-86145"):
        assert cve in build_script
    assert (
        "https://download.gnome.org/sources/libxml2/2.9/libxml2-2.9.14.tar.xz"
        in build_script
    )
    assert "60d74a257d1ccec0475e749cba2f21559e48139efba6ff28224357c7c798dfee" in build_script
    assert "autoreconf --force --install" in build_script
    assert build_script.count("sha256sum --check") == 1
    assert build_script.count("download \\") == 5
    assert 'dpkg --compare-versions "${zlib_version}" gt "1:1.3.3"' in verifier
    assert 'dpkg --compare-versions "${zlib_version}" lt "1:1.3.4"' in verifier
    for runtime_contract in (
        "dpkg -L zlib1g | grep -Ei '(py)?minizip'",
        "zipOpenNewFileInZip4_64",
    ):
        assert runtime_contract in dockerfile
        assert runtime_contract in verifier


def test_zlib_core_package_has_truthful_component_identity_and_compensating_gates() -> None:
    repository = Path(__file__).parents[1]
    build_script = (
        repository / "docker" / "native-security" / "build-fixed-libs.sh"
    ).read_text(encoding="utf-8")
    verifier = (
        repository / "docker" / "native-security" / "verify-fixed-libs.sh"
    ).read_text(encoding="utf-8")
    dockerfile = (repository / "docker" / "Dockerfile.base").read_text(
        encoding="utf-8"
    )

    assert (
        "zlib_version='1:1.3.3+really1.3.2.1+academy.git20260904.e3dc0a8-1'"
        in build_script
    )
    assert "'academy-zlib-core'" in build_script
    assert "academy-zlib-core" in verifier
    assert "academy-zlib-core" in dockerfile
    assert "e3dc0a85b7032e98380dec011bc8f2c2ee0d8fca.tar.gz" in build_script
    assert "33356dac6140d584347fe46bcf7083bd949dec49ac4b52417ae334ec70e3dbc3" in build_script
    assert '#define ZLIB_VERSION "1.3.2.1-motley"' in build_script
    assert "strm->next_in == NULL" in build_script
    assert "make test" in build_script
    assert "find \"${zlib_package}\"" in build_script
    assert "-iname '*minizip*'" in build_script
    assert "-iname '*pyminizip*'" in build_script
    assert "zipOpenNewFileInZip4_64" in build_script
    assert "zipOpenNewFileInZip4_64" in verifier
    assert "zlib.decompress(zlib.compress" in verifier


def test_tesseract_runtimes_pin_security_fixed_libcurl() -> None:
    repository = Path(__file__).parents[1]
    affected = {
        "api": repository / "docker" / "api" / "Dockerfile",
        "ai": repository / "docker" / "ai-worker-cpu" / "Dockerfile",
        "tools": repository / "docker" / "tools-worker" / "Dockerfile",
    }

    for service, path in affected.items():
        dockerfile = path.read_text(encoding="utf-8")
        assert "FROM ${BASE_IMAGE} AS curl-fixed-system" in dockerfile, service
        assert "FROM curl-fixed-system AS base" in dockerfile, service
        assert "ARG LIBCURL_VERSION=8.21.0-2~bpo13+1" in dockerfile, service
        assert "ARG LIBNGHTTP3_VERSION=1.15.0-1~bpo13+1" in dockerfile, service
        assert "ARG LIBNGTCP2_VERSION=1.22.1-1~bpo13+1" in dockerfile, service
        assert (
            "deb https://deb.debian.org/debian trixie-backports main" in dockerfile
        ), service
        assert 'libnghttp3-9="${LIBNGHTTP3_VERSION}"' in dockerfile, service
        assert 'libngtcp2-16="${LIBNGTCP2_VERSION}"' in dockerfile, service
        assert (
            'libngtcp2-crypto-ossl0="${LIBNGTCP2_VERSION}"' in dockerfile
        ), service
        assert 'libcurl4t64="${LIBCURL_VERSION}"' in dockerfile, service
        assert (
            'test "$libcurl_version" = "${LIBCURL_VERSION}"' in dockerfile
        ), service
        assert (
            'test "$libnghttp3_version" = "${LIBNGHTTP3_VERSION}"' in dockerfile
        ), service
        assert (
            'test "$libngtcp2_version" = "${LIBNGTCP2_VERSION}"' in dockerfile
        ), service
        assert (
            'test "$libngtcp2_crypto_version" = "${LIBNGTCP2_VERSION}"'
            in dockerfile
        ), service
        assert (
            "rm -f /etc/apt/sources.list.d/academy-trixie-backports.list"
            in dockerfile
        )

    for path in (
        repository / "docker" / "Dockerfile.base",
        repository / "docker" / "video-worker" / "Dockerfile",
        repository / "docker" / "messaging-worker" / "Dockerfile",
    ):
        dockerfile = path.read_text(encoding="utf-8")
        assert "LIBCURL_VERSION" not in dockerfile
        assert "LIBNGHTTP3_VERSION" not in dockerfile
        assert "LIBNGTCP2_VERSION" not in dockerfile
        assert "academy-trixie-backports.list" not in dockerfile


def test_high_finding_regression_fails_closed() -> None:
    with pytest.raises(gate.GateError, match="High findings regressed"):
        gate.evaluate_high_budget(
            "academy-api",
            _scan(
                _finding("CVE-2099-0001", "demo", "1", "HIGH"),
                _finding("CVE-2099-0002", "demo", "1", "HIGH"),
            ),
            {"academy-api": 1},
            set(),
        )


def test_high_baseline_requires_all_governed_repositories(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        '{"schemaVersion": 3, "maximumHighFindings": {"academy-api": 1}, '
        '"acceptedHighFindings": []}',
        encoding="utf-8",
    )

    with pytest.raises(gate.GateError, match="exactly the six"):
        gate.load_high_baselines(baseline)


@pytest.mark.parametrize(
    ("replacement_cve", "replacement_version"),
    [
        ("CVE-2099-9999", "3.46.1-7+deb13u1"),
        ("CVE-2026-11822", "3.46.1-7+deb13u2"),
    ],
)
def test_same_count_high_identity_substitution_fails_closed(
    replacement_cve: str,
    replacement_version: str,
) -> None:
    baselines, known = gate.load_high_baselines(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-high-risk-baseline.json"
    )
    expected = sorted(key for key in known if key[0] == "academy-base")
    findings = [
        _finding(cve, package, version, "HIGH")
        for _, cve, package, version in expected
    ]
    findings[0] = _finding(
        replacement_cve,
        "sqlite3",
        replacement_version,
        "HIGH",
    )

    with pytest.raises(gate.GateError, match="unreviewed High"):
        gate.evaluate_high_budget(
            "academy-base",
            _scan(*findings),
            baselines,
            known,
        )


def test_removed_high_requires_reviewed_baseline_reduction() -> None:
    baselines, known = gate.load_high_baselines(
        Path(__file__).parents[1] / "docs" / "ssot" / "ecr-high-risk-baseline.json"
    )
    expected = sorted(key for key in known if key[0] == "academy-base")
    findings = _scan(
        *(
            _finding(cve, package, version, "HIGH")
            for _, cve, package, version in expected[1:]
        )
    )

    with pytest.raises(gate.GateError, match="stale High"):
        gate.evaluate_high_budget("academy-base", findings, baselines, known)


def test_high_finding_without_exact_identity_fails_closed() -> None:
    malformed = _finding("CVE-2099-9999", "demo", "1", "HIGH")
    malformed["attributes"] = [{"key": "package_name", "value": "demo"}]

    with pytest.raises(gate.GateError, match="lacks exact identity"):
        gate.evaluate_high_budget(
            "academy-api",
            _scan(malformed),
            {"academy-api": 1},
            {("academy-api", "CVE-2099-9999", "demo", "1")},
        )


def test_high_baseline_identity_count_must_match_budget(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    document = {
        "schemaVersion": 3,
        "maximumHighFindings": {repository: 0 for repository in gate.REPOSITORIES},
        "acceptedHighFindings": [_high_policy_entry()],
    }
    baseline.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(gate.GateError, match="identity baseline count mismatch"):
        gate.load_high_baselines(baseline)


def test_temporary_high_acceptance_requires_exact_review_metadata(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    document = _high_policy_document(_high_policy_entry(rationale="short"))
    baseline.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(gate.GateError, match="needs a durable rationale"):
        gate.load_high_baselines(baseline, date(2026, 8, 23))


@pytest.mark.parametrize("expires_on", [None, "not-a-date"])
def test_high_acceptance_requires_valid_expiry(
    tmp_path: Path,
    expires_on: object,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(_high_policy_document(_high_policy_entry(expiresOn=expires_on))),
        encoding="utf-8",
    )

    with pytest.raises(gate.GateError, match="invalid expiresOn"):
        gate.load_high_baselines(baseline, date(2026, 8, 23))


def test_high_acceptance_requires_exact_cve_tracker(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            _high_policy_document(
                _high_policy_entry(
                    vendorTracker=(
                        "https://security-tracker.debian.org/tracker/CVE-2099-9998"
                    )
                )
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(gate.GateError, match="exact Debian tracker URL"):
        gate.load_high_baselines(baseline, date(2026, 8, 23))


def test_high_acceptance_rejects_duplicate_identity(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    entry = _high_policy_entry()
    baseline.write_text(
        json.dumps(_high_policy_document(entry, dict(entry))),
        encoding="utf-8",
    )

    with pytest.raises(gate.GateError, match="duplicate High finding policy entry"):
        gate.load_high_baselines(baseline, date(2026, 8, 23))


def test_schema_three_rejects_metadata_free_known_findings(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    document = _high_policy_document(_high_policy_entry())
    document["knownHighFindings"] = []
    baseline.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(gate.GateError, match="knownHighFindings is not allowed"):
        gate.load_high_baselines(baseline, date(2026, 8, 23))


def test_runtime_base_is_digest_pinned_without_unused_postgres_client() -> None:
    dockerfile = (Path(__file__).parents[1] / "docker" / "Dockerfile.base").read_text(
        encoding="utf-8"
    )
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(
        line.startswith(
            "FROM python:3.11-slim@sha256:"
            "90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff"
        )
        for line in from_lines
    )
    assert "postgresql-client" not in dockerfile


def test_patched_ffmpeg_is_source_pinned_and_isolated_to_video_worker() -> None:
    repository = Path(__file__).parents[1]
    api = (repository / "docker" / "api" / "Dockerfile").read_text(encoding="utf-8")
    ai = (repository / "docker" / "ai-worker-cpu" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    video = (repository / "docker" / "video-worker" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "    ffmpeg \\" not in api
    assert "ffprobe -version" not in api
    assert "    ffmpeg \\" not in ai
    assert "cv2.getBuildInformation" in ai
    assert "'YES' in line.split()" in ai
    assert "    ffmpeg \\" not in video
    assert "FFMPEG_COMMIT=db05df9d135fb56a4babb836d5e9f5c1d984e087" in video
    assert "https://github.com/FFmpeg/FFmpeg.git" in video
    assert 'test "$(git -C /tmp/ffmpeg rev-parse HEAD)" = "${FFMPEG_COMMIT}"' in video
    assert "/opt/academy-ffmpeg/academy-source-commit" in video
    assert (
        'test "$(cat /opt/academy-ffmpeg/academy-source-commit)" '
        '= "${FFMPEG_COMMIT}"' in video
    )
    assert "CVE-2026-70628/70632" in video
    assert 'ffmpeg -hide_banner -encoders | grep -q "libx264"' in video
    assert "/tmp/ffmpeg-smoke/stream.m3u8" in video
    assert "    libx264-164 \\" in video


def test_video_source_build_uses_native_arm64_runner() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "v1-build-and-push-latest.yml"
    ).read_text(encoding="utf-8")

    assert "runs-on: ${{ matrix.runner }}" in workflow
    assert "runner: ubuntu-24.04-arm" in workflow
    assert (
        "if: steps.selection.outputs.should_build == 'true' "
        "&& matrix.service != 'video'" in workflow
    )


def test_missing_scan_result_is_started_then_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    descriptions = iter(
        [
            {"imageScanStatus": None, "imageScanFindings": None},
            {"imageScanStatus": {"status": "COMPLETE"}, "imageScanFindings": {}},
        ]
    )
    starts: list[list[str]] = []
    monkeypatch.setattr(gate, "_describe_scan", lambda *_args: next(descriptions))
    monkeypatch.setattr(
        gate,
        "_run_aws_json",
        lambda arguments, **_kwargs: starts.append(arguments) or {},
    )

    completed = gate.wait_for_completed_scan(
        "academy-base", "sha256:" + "a" * 64, "ap-northeast-2", 2, 0
    )

    assert completed["imageScanStatus"]["status"] == "COMPLETE"
    assert starts and starts[0][:2] == ["ecr", "start-image-scan"]


def test_scan_start_quota_still_requires_completed_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptions = iter(
        [
            {"imageScanStatus": None, "imageScanFindings": None},
            {"imageScanStatus": {"status": "COMPLETE"}, "imageScanFindings": {}},
        ]
    )
    monkeypatch.setattr(gate, "_describe_scan", lambda *_args: next(descriptions))

    def _quota(*_args, **_kwargs):
        raise gate.GateError("AWS ECR command failed: LimitExceededException")

    monkeypatch.setattr(gate, "_run_aws_json", _quota)

    completed = gate.wait_for_completed_scan(
        "academy-base", "sha256:" + "b" * 64, "ap-northeast-2", 2, 0
    )

    assert completed["imageScanStatus"]["status"] == "COMPLETE"
