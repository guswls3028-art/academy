from __future__ import annotations

from pathlib import Path
import re
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "v1"

STANDALONE_WRITERS = (
    "ensure-product-analytics-hash-key.ps1",
    "remove-solapi-mock-from-workers-ssm.ps1",
    "set-dev-alerts-webhook.ps1",
    "set-messaging-tenant-binding-enforcement.ps1",
    "set-messaging-tenant-hold.ps1",
    "set-tenant-db-usage-telemetry.ps1",
    "set-toss-billing.ps1",
    "update-api-env-sqs.ps1",
    "update-messaging-whitelist-ssm.ps1",
    "update-workers-env-sqs.ps1",
)

NON_PRODUCTION_PARAMETER_WRITERS = {
    "converge-api-preprod-database.ps1",
    "publish-api-development-env.ps1",
    "publish-api-preprod-env.ps1",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_every_standalone_production_runtime_writer_owns_one_lock_scope() -> None:
    for name in STANDALONE_WRITERS:
        content = _text(SCRIPTS / name)
        assert 'core\\runtime-env-lock.ps1' in content, name
        assert content.count("Enter-AcademyRuntimeEnvMutationLock") == 1, name
        assert content.count("Exit-AcademyRuntimeEnvMutationLock") == 1, name
        assert content.index("Enter-AcademyRuntimeEnvMutationLock") < content.rindex(
            "Exit-AcademyRuntimeEnvMutationLock"
        ), name
        if name != "set-messaging-tenant-binding-enforcement.ps1":
            assert "Assert-AcademyRuntimeEnvMutationLock" in content, name
            assert content.rindex("Assert-AcademyRuntimeEnvMutationLock") < content.rindex(
                "put-parameter"
            ), name
        assert re.search(
            r"finally\s*\{[^{}]*Exit-AcademyRuntimeEnvMutationLock",
            content,
            flags=re.DOTALL,
        ), name


def test_direct_runtime_parameter_writer_inventory_is_explicit() -> None:
    detected: set[str] = set()
    protected_reference = re.compile(
        r"/academy/(?:api|workers)/env|SsmApiEnv(?![A-Za-z])|SsmWorkersEnv(?![A-Za-z])"
    )
    for path in SCRIPTS.rglob("*.ps1"):
        content = _text(path)
        if "put-parameter" in content and protected_reference.search(content):
            detected.add(path.relative_to(SCRIPTS).as_posix())

    detected -= NON_PRODUCTION_PARAMETER_WRITERS
    expected = {
        name
        for name in STANDALONE_WRITERS
        if name != "set-messaging-tenant-binding-enforcement.ps1"
    } | {
        "core/bootstrap.ps1",
        "core/ssm-safe-update.ps1",
        "core/sync_env.ps1",
    }
    assert detected == expected


def test_python_and_workflow_direct_runtime_writers_are_explicit() -> None:
    python_writers = {
        path.relative_to(ROOT).as_posix()
        for path in SCRIPTS.rglob("*.py")
        if (
            "put_parameter" in _text(path)
            or "runtime.put_environment(" in _text(path)
        )
        and (
            "/academy/api/env" in _text(path)
            or "/academy/workers/env" in _text(path)
        )
    }
    assert python_writers == {
        "scripts/v1/reconcile_common_alimtalk_owner_tenant.py",
        "scripts/v1/reconcile_common_alimtalk_sender.py",
    }

    for pattern in ("*.yml", "*.yaml"):
        for path in (ROOT / ".github" / "workflows").glob(pattern):
            content = _text(path)
            if "/academy/api/env" in content or "/academy/workers/env" in content:
                assert "put-parameter" not in content, path


def test_delegating_worker_restart_inherits_one_lock_across_update_and_refresh() -> None:
    content = _text(SCRIPTS / "restart-workers.ps1")
    enter = content.index("Enter-AcademyRuntimeEnvMutationLock")
    validate = content.index("Assert-AcademyWorkerRefreshTargets", enter)
    update = content.index("update-workers-env-sqs.ps1", enter)
    refresh = content.index("Start-AcademyInstanceRefresh", update)
    wait = content.index("Wait-AcademyInstanceRefresh", refresh)
    complete = content.index("Complete-AcademyRuntimeRefreshBoundary", wait)
    exit_lock = content.index("Exit-AcademyRuntimeEnvMutationLock", complete)

    assert "ACADEMY_RUNTIME_ENV_LOCK_OWNER" in _text(
        SCRIPTS / "core" / "runtime-env-lock.ps1"
    )
    assert enter < validate < update < refresh < wait < complete < exit_lock
    assert content.count("Complete-AcademyRuntimeRefreshBoundary") == 1
    assert re.search(
        r"\n\}\s*\nComplete-AcademyRuntimeRefreshBoundary",
        content[wait:complete + len("Complete-AcademyRuntimeRefreshBoundary")],
    )


@pytest.mark.parametrize(
    "names",
    (
        "@('messaging-asg', '', 'tools-asg')",
        "@('messaging-asg', 'messaging-asg', 'tools-asg')",
        "@('messaging-asg', 'ai-asg')",
        "@(' messaging-asg', 'ai-asg', 'tools-asg')",
    ),
    ids=("blank", "duplicate", "missing", "whitespace-drift"),
)
def test_worker_refresh_rejects_invalid_target_sets_before_mutation(names: str) -> None:
    helper = (SCRIPTS / "core" / "runtime-env-lock.ps1").as_posix()
    script = textwrap.dedent(
        f"""
        . '{helper}'
        try {{
            @(Assert-AcademyWorkerRefreshTargets -Names {names}) | Out-Null
            exit 91
        }} catch {{
            "TARGET_SET_REJECTED"
        }}
        """
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "TARGET_SET_REJECTED" in output


def test_worker_refresh_accepts_exact_three_distinct_targets() -> None:
    helper = (SCRIPTS / "core" / "runtime-env-lock.ps1").as_posix()
    script = textwrap.dedent(
        f"""
        . '{helper}'
        $targets = @(
            Assert-AcademyWorkerRefreshTargets `
                -Names @('messaging-asg', 'ai-asg', 'tools-asg')
        )
        if ($targets.Count -ne 3) {{ exit 91 }}
        "TARGET_SET_ACCEPTED"
        """
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "TARGET_SET_ACCEPTED" in output


def test_optional_runtime_refreshes_wait_terminal_before_lock_release() -> None:
    for name in (
        "set-dev-alerts-webhook.ps1",
        "set-messaging-tenant-binding-enforcement.ps1",
        "set-toss-billing.ps1",
    ):
        content = _text(SCRIPTS / name)
        refresh = content.index("Start-AcademyInstanceRefresh")
        wait = content.index("Wait-AcademyInstanceRefresh", refresh)
        complete = content.index("Complete-AcademyRuntimeRefreshBoundary", wait)
        exit_lock = content.rindex("Exit-AcademyRuntimeEnvMutationLock")
        assert refresh < wait < complete < exit_lock, name
        if name in {"set-dev-alerts-webhook.ps1", "set-toss-billing.ps1"}:
            health = content.index("Assert-AcademyPublicApiHealth", wait)
            assert wait < health < complete, name
    assert "send-command" not in _text(SCRIPTS / "set-dev-alerts-webhook.ps1")


def test_dev_alerts_webhook_writer_keeps_receiver_and_requirement_in_one_ssot() -> None:
    content = _text(SCRIPTS / "set-dev-alerts-webhook.ps1")

    assert content.count('NotePropertyName "DEV_ALERTS_WEBHOOK_REQUIRED"') == 2
    assert 'NotePropertyValue $false' in content
    assert 'NotePropertyValue $true' in content
    persist = content.index("$newJson =")
    assert content.rindex('NotePropertyName "DEV_ALERTS_WEBHOOK_URL"') < persist
    assert content.rindex('NotePropertyName "DEV_ALERTS_WEBHOOK_REQUIRED"') < persist


def _run_lock_helper_scenario(body: str) -> subprocess.CompletedProcess[str]:
    helper = (SCRIPTS / "core" / "runtime-env-lock.ps1").as_posix()
    script = textwrap.dedent(
        f"""
        . '{helper}'
        $script:RuntimeEnvMutationLockAcquired = $true
        $script:RuntimeEnvMutationLockOwnedHere = $true
        $script:RuntimeEnvMutationLockOwner = 'contract-test-owner'
        $script:RuntimeEnvMutationLockReleaseAllowed = $true
        function Assert-AcademyRuntimeEnvMutationLock {{}}
        function Renew-AcademyRuntimeEnvMutationLock {{}}
        {body}
        Exit-AcademyRuntimeEnvMutationLock -Region 'ap-northeast-2'
        if ($script:RuntimeEnvMutationLockReleaseAllowed) {{ exit 91 }}
        "LOCK_RETAINED_ASSERTED"
        """
    )
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "body",
    (
        """
        function aws {
            $global:LASTEXITCODE = 1
            return ''
        }
        try {
            Start-AcademyInstanceRefresh -AutoScalingGroupName 'test-asg'
        } catch {}
        """,
        """
        function aws {
            $global:LASTEXITCODE = 0
            return 'None'
        }
        try {
            Start-AcademyInstanceRefresh -AutoScalingGroupName 'test-asg'
        } catch {}
        """,
        """
        $script:RuntimeEnvMutationLockReleaseAllowed = $false
        function aws {
            $global:LASTEXITCODE = 1
            return ''
        }
        try {
            Wait-AcademyInstanceRefresh -AutoScalingGroupName 'test-asg' `
                -InstanceRefreshId 'refresh-1' -MaxAttempts 1 -PollSeconds 5
        } catch {}
        """,
        """
        $script:RuntimeEnvMutationLockReleaseAllowed = $false
        function aws {
            $global:LASTEXITCODE = 0
            return '{"InstanceRefreshes":[{"Status":"InProgress"}]}'
        }
        try {
            Wait-AcademyInstanceRefresh -AutoScalingGroupName 'test-asg' `
                -InstanceRefreshId 'refresh-1' -MaxAttempts 1 -PollSeconds 5
        } catch {}
        """,
        """
        $script:RuntimeEnvMutationLockReleaseAllowed = $false
        function aws {
            $global:LASTEXITCODE = 0
            return '{"InstanceRefreshes":[{"Status":"Failed"}]}'
        }
        try {
            Wait-AcademyInstanceRefresh -AutoScalingGroupName 'test-asg' `
                -InstanceRefreshId 'refresh-1' -MaxAttempts 1 -PollSeconds 5
        } catch {}
        """,
        """
        $script:RuntimeEnvMutationLockReleaseAllowed = $false
        function Invoke-RestMethod { throw 'health unavailable' }
        try { Assert-AcademyPublicApiHealth } catch {}
        """,
    ),
    ids=(
        "ambiguous-start",
        "invalid-start-id",
        "readback-failure",
        "readback-timeout",
        "terminal-failure",
        "health-failure",
    ),
)
def test_refresh_failures_retain_shared_lock(body: str) -> None:
    result = _run_lock_helper_scenario(body)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "LOCK_RETAINED owner=contract-test-owner" in output
    assert "LOCK_RETAINED_ASSERTED" in output


def test_generic_safe_update_and_deploy_owned_writers_assert_lock_ownership() -> None:
    safe_update = _text(SCRIPTS / "core" / "ssm-safe-update.ps1")
    bootstrap = _text(SCRIPTS / "core" / "bootstrap.ps1")
    sync_env = _text(SCRIPTS / "core" / "sync_env.ps1")
    deploy = _text(SCRIPTS / "deploy.ps1")

    assert "Assert-AcademyRuntimeEnvMutationLock -Region $Region" in safe_update
    assert "Assert-DeployLockAcquired -Reg $script:Region" in bootstrap
    assert sync_env.count("Assert-DeployLockAcquired -Reg $script:Region") >= 4
    acquire = deploy.index("Acquire-DeployLock")
    bootstrap_call = deploy.index("Invoke-Bootstrap ", acquire)
    sync_call = deploy.index("Invoke-SyncEnvFromSSOT", bootstrap_call)
    release = deploy.index("Release-DeployLock", sync_call)
    assert acquire < bootstrap_call < sync_call < release


def test_sender_reconcile_renews_and_conditionally_releases_shared_lock() -> None:
    content = _text(SCRIPTS / "reconcile_common_alimtalk_sender.py")

    assert 'LOCK_KEY = "__deployment_control_v2__"' in content
    assert 'LOCK_TABLE = "academy-v1-video-job-lock"' in content
    assert content.count("runtime.renew_lock(owner)") >= 6
    final_snapshot = content.index("final_snapshot = runtime.read_snapshot()")
    assert_owned = content.index("runtime.assert_lock_owned(owner)", final_snapshot)
    release = content.index("runtime.release_lock(owner)", assert_owned)
    success_output = content.index("output(success_message)", release)
    assert final_snapshot < assert_owned < release < success_output
    assert 'ConditionExpression="#owner = :owner AND #ttl >= :now"' in content


def test_owner_reconcile_preserves_one_lock_through_final_runtime_readback() -> None:
    content = _text(SCRIPTS / "reconcile_common_alimtalk_owner_tenant.py")

    assert 'EXPECTED_OWNER_TENANT_ID = "1"' in content
    assert "_assert_only_owner_changed" in content
    assert "_assert_source_freshness" in content
    assert content.count("runtime.renew_lock(owner)") >= 6
    messaging_refresh = content.index("runtime.refresh_service(MESSAGING_ASG)")
    api_refresh = content.index("runtime.refresh_service(API_ASG)", messaging_refresh)
    final_snapshot = content.index("final_snapshot = runtime.read_snapshot()", api_refresh)
    runtime_readback = content.index("_runtime_owner_evidence(", api_refresh)
    assert_owned = content.index("runtime.assert_lock_owned(owner)", final_snapshot)
    release = content.index("runtime.release_lock(owner)", assert_owned)
    assert messaging_refresh < api_refresh < runtime_readback < final_snapshot
    assert final_snapshot < assert_owned < release
    assert "runtime.runtime_owner_digests(" in content
    assert "runtime_environment_rollback_failed_lock_retained" in content
    assert "forward_convergence_required_lock_retained" in content


def test_runbooks_do_not_publish_direct_runtime_parameter_overwrites() -> None:
    for root in (ROOT / "docs", ROOT / "infra"):
        for path in root.rglob("*.md"):
            content = _text(path)
            fenced_blocks = "\n".join(
                match.group(1)
                for match in re.finditer(
                    r"```[^\n]*\n(.*?)```",
                    content,
                    flags=re.DOTALL,
                )
            )
            assert not re.search(
                r"aws\s+ssm\s+put-parameter",
                fenced_blocks,
                flags=re.IGNORECASE,
            ), path
            assert not re.search(
                r"Update-AcademySSMParameter\b",
                fenced_blocks,
                flags=re.IGNORECASE,
            ), path
