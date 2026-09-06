from __future__ import annotations

import re
import subprocess
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PRUNE = REPO_ROOT / "scripts" / "v1" / "core" / "prune.ps1"
SSOT = REPO_ROOT / "scripts" / "v1" / "core" / "ssot.ps1"
BOOTSTRAP = REPO_ROOT / "scripts" / "v1" / "core" / "bootstrap.ps1"
VERIFY = REPO_ROOT / "scripts" / "v1" / "verify.ps1"
DEPLOY = REPO_ROOT / "scripts" / "v1" / "deploy.ps1"
DEPLOY_AND_VERIFY = REPO_ROOT / "scripts" / "v1" / "deploy-api-and-verify-workers.ps1"
RUN_DEPLOY_VERIFICATION = REPO_ROOT / "scripts" / "v1" / "run-deploy-verification.ps1"
PIN_ASG_IMAGE = REPO_ROOT / "scripts" / "v1" / "pin-asg-image.ps1"
CONVERGE_WORKER_BASELINE = (
    REPO_ROOT / "scripts" / "v1" / "converge-worker-warm-baseline.ps1"
)
COST_WASTE_AUDIT = REPO_ROOT / "scripts" / "v1" / "run-cost-waste-audit.ps1"
DIFF = REPO_ROOT / "scripts" / "v1" / "core" / "diff.ps1"
SYNC_ENV = REPO_ROOT / "scripts" / "v1" / "core" / "sync_env.ps1"
WORKER_USERDATA = REPO_ROOT / "scripts" / "v1" / "resources" / "worker_userdata.ps1"
API_RESOURCE = REPO_ROOT / "scripts" / "v1" / "resources" / "api.ps1"
REFRESH_API_ENV = REPO_ROOT / "scripts" / "v1" / "inline" / "refresh-api-env.sh"
REFRESH_API_ENV_PS1 = REPO_ROOT / "scripts" / "v1" / "refresh-api-env.ps1"
CLOUDWATCH_RESOURCE = REPO_ROOT / "scripts" / "v1" / "resources" / "cloudwatch.ps1"
ASG_TOOLS_RESOURCE = REPO_ROOT / "scripts" / "v1" / "resources" / "asg_tools.ps1"
IAM_RESOURCE = REPO_ROOT / "scripts" / "v1" / "resources" / "iam.ps1"
WORKER_BEDROCK_POLICY = (
    REPO_ROOT / "scripts" / "v1" / "templates" / "iam"
    / "policy_workers_bedrock_problem_transcription.json"
)
EC2_CLOUDWATCH_LOGS_POLICY = (
    REPO_ROOT / "scripts" / "v1" / "templates" / "iam"
    / "policy_ec2_cloudwatch_logs.json"
)
WORKERS_SQS_POLICY = (
    REPO_ROOT / "scripts" / "v1" / "templates" / "iam"
    / "policy_workers_sqs.json"
)
CONVERGE_WORKERS_SQS_IAM = (
    REPO_ROOT / "scripts" / "v1" / "converge-runtime-worker-sqs-iam.ps1"
)
ECR_RESOURCE = REPO_ROOT / "scripts" / "v1" / "resources" / "ecr.ps1"
DYNAMODB_RESOURCE = REPO_ROOT / "scripts" / "v1" / "resources" / "dynamodb.ps1"
ECR_CLEANUP = REPO_ROOT / "scripts" / "v1" / "ecr-cleanup.py"
DEPLOYMENT_LOCK = REPO_ROOT / "scripts" / "v1" / "deployment_lock.py"
RELEASE_FRESHNESS = REPO_ROOT / "scripts" / "v1" / "release_freshness.py"
WEEKLY_CLEANUP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "weekly-ecr-cleanup.yml"
DEV_ALERTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "dev-alerts-cron.yml"
STATIC_GHA_IAM = REPO_ROOT / "infra" / "worker_asg" / "iam_policy_gha_ecr_build.json"
ROLLBACK_ASG = REPO_ROOT / "scripts" / "v1" / "rollback-asg.ps1"
ROLLBACK_VIDEO = REPO_ROOT / "scripts" / "v1" / "rollback-video.ps1"
RELEASE_PREREQUISITES = (
    REPO_ROOT / "scripts" / "v1" / "converge-release-prerequisites.ps1"
)
PARAMS = REPO_ROOT / "docs" / "ssot" / "params.yaml"
DEPLOY_ARCH_DOC = REPO_ROOT / "docs" / "infrastructure" / "deployment-architecture.md"
V1_README = REPO_ROOT / "scripts" / "v1" / "README.md"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "v1-build-and-push-latest.yml"
ROLLBACK_SCRIPTS = {
    "api": REPO_ROOT / "scripts" / "v1" / "rollback-api.ps1",
    "messaging": REPO_ROOT / "scripts" / "v1" / "rollback-messaging.ps1",
    "ai": REPO_ROOT / "scripts" / "v1" / "rollback-ai.ps1",
    "tools": REPO_ROOT / "scripts" / "v1" / "rollback-tools.ps1",
}
REMOTE_API_TOOLS = (
    REPO_ROOT / "scripts" / "v1" / "run-qna-e2e-verify.ps1",
    REPO_ROOT / "scripts" / "v1" / "run-api-management-remote.ps1",
)
CLOUDFLARE_MUTATION_SCRIPTS = (
    REPO_ROOT / "scripts" / "add-cloudflare-zone.ps1",
    REPO_ROOT / "scripts" / "pages-add-custom-domain.ps1",
    REPO_ROOT / "scripts" / "zone-dns-add-pages-cname.ps1",
    REPO_ROOT / "scripts" / "zone-dns-update-record.ps1",
)

PINNED_CHECKOUT_ACTION = re.compile(
    r"uses: actions/checkout@[0-9a-f]{40} # v7\.0\.1"
)
PINNED_AWS_CREDENTIALS_ACTION = re.compile(
    r"uses: aws-actions/configure-aws-credentials@[0-9a-f]{40} # v6"
)


def _pinned_action_position(block: str, pattern: re.Pattern[str]) -> int:
    match = pattern.search(block)
    assert match, f"pinned action not found: {pattern.pattern}"
    return match.start()


def test_cloudflare_mutation_scripts_require_explicit_should_process() -> None:
    for path in CLOUDFLARE_MUTATION_SCRIPTS:
        source = path.read_text(encoding="utf-8-sig")
        assert "SupportsShouldProcess = $true" in source
        assert "ConfirmImpact = 'High'" in source
        assert "$PSCmdlet.ShouldProcess" in source


def test_worker_release_converges_warm_baseline_before_digest_refresh() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    converge = CONVERGE_WORKER_BASELINE.read_text(encoding="utf-8-sig")

    ai = workflow.split("\n  deploy-ai:\n", maxsplit=1)[1].split(
        "\n  deploy-tools:\n", maxsplit=1
    )[0]
    tools = workflow.split("\n  deploy-tools:\n", maxsplit=1)[1].split(
        "\n  deploy-video:\n", maxsplit=1
    )[0]

    assert "converge-worker-warm-baseline.ps1 -Service ai -Ci" in ai
    assert "converge-worker-warm-baseline.ps1 -Service tools -Ci" in tools
    assert ai.index("converge-worker-warm-baseline.ps1") < ai.index(
        "Pin AI Launch Template"
    )
    assert tools.index("converge-worker-warm-baseline.ps1") < tools.index(
        "Pin Tools Launch Template"
    )
    for block in (ai, tools):
        assert '\"MinHealthyPercentage\":100' in block
        assert '\"MaxHealthyPercentage\":200' in block
        assert "skip_refresh" not in block
        assert "desired=0" not in block
    for token in (
        "Assert-AwsMutationIdentity",
        "Load-SSOT -Env prod",
        '"--min-size", [string]$expectedMin',
        '"--desired-capacity", [string]$targetDesired',
        "expectedMin -lt 1",
        "healthyCount -ge $expectedMin",
        "WORKER_WARM_BASELINE_READY",
    ):
        assert token in converge

    verify = workflow.split("\n  verify-deployment:\n", maxsplit=1)[1].split(
        "\n  release-production-lock:\n", maxsplit=1
    )[0]
    assert "warm baseline drift" in verify
    assert "warm runtime desired capacity is invalid" in verify
    assert "no instances (desired=0" not in verify
    assert "no running digest to inspect" not in verify


def test_cost_audit_preserves_development_runtime_and_fails_closed_on_dry_run_errors() -> None:
    audit = COST_WASTE_AUDIT.read_text(encoding="utf-8-sig")

    assert '$name -eq $script:ApiDevelopmentInstanceName' in audit
    assert '$managedBy -eq $script:ApiDevelopmentManagedByTag' in audit
    assert 'Key -eq "aws:autoscaling:groupName"' in audit
    assert '$asgName -like "$prefix*"' in audit
    assert '-not $isBatchRuntime' in audit
    assert '$developmentInstances.Count -ne 1' in audit
    assert '[string]$PythonExecutable = "python"' in audit
    assert 'Invoke-ProcessText $PythonExecutable' in audit
    assert '$batchStatus -like "failed*"' in audit
    assert "blocked; investigate runtime or AWS access before deregistration" in audit
    assert "$redisFreeMemory.Long.Low -lt 0.75GB" in audit
    assert "$redisCredits.Long.Low -lt 50" in audit


def test_api_user_impact_alarm_requires_zero_healthy_targets() -> None:
    source = CLOUDWATCH_RESOURCE.read_text(encoding="utf-8-sig")
    api_alarms = source.split(
        "function Ensure-ApiCloudWatchAlarms {", maxsplit=1
    )[1].split("function Ensure-RdsCloudWatchAlarms", maxsplit=1)[0]
    healthy_target_alarm = api_alarms.split(
        '"--alarm-name", "academy-api-UnHealthyHosts"', maxsplit=1
    )[1].split("Invoke-Aws $unhealthyArgs", maxsplit=1)[0]
    ssot = SSOT.read_text(encoding="utf-8-sig")
    params = PARAMS.read_text(encoding="utf-8")

    assert '"--alarm-name", "academy-api-UnHealthyHosts"' in api_alarms
    assert '"--metric-name", "HealthyHostCount"' in healthy_target_alarm
    assert '"--statistic", "Minimum"' in healthy_target_alarm
    assert '"--comparison-operator", "LessThanThreshold"' in healthy_target_alarm
    assert '"--treat-missing-data", "breaching"' in healthy_target_alarm
    assert 'UnHealthyHostCount' not in healthy_target_alarm
    assert 'ALARM("academy-api-UnHealthyHosts")' in api_alarms
    assert "$minimumHealthyHosts = 1" in api_alarms
    assert "ObservabilityApiMinimumHealthyHosts" not in ssot
    assert "apiMinimumHealthyHosts" not in params
    assert "apiTargetUnhealthyThreshold" not in ssot
    assert "apiTargetUnhealthyThreshold" not in params


def test_api_runtime_sync_and_refresh_fail_closed() -> None:
    sync_env = SYNC_ENV.read_text(encoding="utf-8-sig")
    api_resource = API_RESOURCE.read_text(encoding="utf-8-sig")
    refresh = REFRESH_API_ENV.read_text(encoding="utf-8")
    refresh_ps1 = REFRESH_API_ENV_PS1.read_text(encoding="utf-8-sig")

    assert '"DJANGO_SETTINGS_MODULE"' in sync_env
    assert '"apps.api.config.settings.prod"' in sync_env
    assert "Resolve-ApiLaunchTemplateDeploymentId" in api_resource
    assert '$deploymentId = Get-Date -Format "o"' not in api_resource
    assert "Resolve-EvidenceApiHealthUrl" in api_resource
    assert "set -euo pipefail" in refresh
    assert '|| echo "docker run failed"' not in refresh
    assert "API env refresh docker run failed" in refresh
    assert "API_ENV_REFRESH_ROLLBACK_PASS previous container restored" in refresh
    assert "API_ENV_REFRESH_ROLLBACK_FAILED" in refresh
    assert "API_ENV_REFRESH_PASS healthz=200 health=200" in refresh
    assert 'ENV_FILE="${ACADEMY_API_ENV_FILE:-/opt/api.env}"' in refresh
    assert 'mv "${ENV_PREVIOUS}.next" "$ENV_PREVIOUS"' in refresh
    assert 'mv "$ENV_PREVIOUS" "$ENV_FILE"' in refresh
    restore = refresh.split("restore_previous() {", maxsplit=1)[1].split(
        "on_exit() {", maxsplit=1
    )[0]
    assert restore.index('mv "$ENV_PREVIOUS" "$ENV_FILE"') < restore.index(
        'docker start "$container"'
    )
    assert "exit 1" in refresh
    assert "API env refresh failed for:" in refresh_ps1
    assert "API env refresh failed closed:" in api_resource


def test_problem_studio_bedrock_policy_is_model_scoped_and_converged() -> None:
    policy = json.loads(WORKER_BEDROCK_POLICY.read_text(encoding="utf-8"))
    statements = policy["Statement"]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Action"] == "bedrock:InvokeModel"
    assert statement["Resource"] == [
        "arn:aws:bedrock:ap-northeast-2:809466760795:inference-profile/global.amazon.nova-2-lite-v1:0",
        "arn:aws:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0",
    ]

    iam = IAM_RESOURCE.read_text(encoding="utf-8-sig")
    assert "policy_workers_bedrock_problem_transcription.json" in iam
    assert "academy-workers-bedrock-problem-transcription" in iam


def test_read_only_deploy_verification_loads_manifest_dependency_before_drift() -> None:
    source = RUN_DEPLOY_VERIFICATION.read_text(encoding="utf-8-sig")

    manifest_dependency = 'resources\\worker_userdata.ps1'
    drift_dependency = 'core\\diff.ps1'
    assert manifest_dependency in source
    assert source.index(manifest_dependency) < source.index(drift_dependency)


def test_shared_mutation_lock_uses_one_ssot_table_and_owner_everywhere() -> None:
    lock = DEPLOYMENT_LOCK.read_text(encoding="utf-8")
    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    weekly = WEEKLY_CLEANUP_WORKFLOW.read_text(encoding="utf-8")
    params = PARAMS.read_text(encoding="utf-8")
    iam = STATIC_GHA_IAM.read_text(encoding="utf-8")
    table = "academy-v1-video-job-lock"

    assert f'lockTableName: {table}' in params
    assert f'DEFAULT_TABLE = "{table}"' in lock
    assert f"ACADEMY_DEPLOY_LOCK_TABLE: {table}" in deploy
    assert f"ACADEMY_DEPLOY_LOCK_TABLE: {table}" in weekly
    assert f"table/{table}" in iam
    assert 'attribute_not_exists(videoId) OR #ttl < :now' in lock
    assert '"#owner = :owner"' in lock
    assert '"#owner = :owner AND #ttl >= :now"' in lock
    assert "ACADEMY_DEPLOY_LOCK_OWNER: ci-deploy:${{ github.run_id }}:${{ github.run_attempt }}" in deploy
    assert "ACADEMY_DEPLOY_LOCK_OWNER: weekly-cleanup:${{ github.run_id }}:${{ github.run_attempt }}" in weekly
    assert deploy.count("deployment_lock.py renew") >= 7
    assert "action = \"acquire\" if owned_here else \"renew\"" in ECR_CLEANUP.read_text(encoding="utf-8")


def test_every_lock_renewing_deploy_job_checks_out_scripts_first() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    for job_name in (
        "prepare-build",
        "build-runtime-images",
        "build-and-push",
        "run-migrations",
        "deploy-api",
        "deploy-messaging",
        "deploy-ai",
        "deploy-tools",
        "deploy-video",
    ):
        block = _job_block(workflow, job_name)
        assert "deployment_lock.py renew" in block
        assert _pinned_action_position(block, PINNED_CHECKOUT_ACTION) < block.index(
            "deployment_lock.py renew"
        )


def test_release_and_weekly_workflows_share_repository_and_atomic_lock_concurrency() -> None:
    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    weekly = WEEKLY_CLEANUP_WORKFLOW.read_text(encoding="utf-8")
    for workflow in (deploy, weekly):
        assert "group: academy-production-mutation" in workflow
        assert "cancel-in-progress: false" in workflow
    assert "release-production-lock:" in deploy
    assert "if: always() && needs.acquire-production-lock.result == 'success'" in deploy
    assert "docs/reports/release-manifest.latest.json" in weekly


def test_every_deploy_oidc_step_has_a_bounded_action_timeout() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    steps = re.split(r"(?m)(?=^      - name: )", workflow)
    credential_steps = [
        step for step in steps if PINNED_AWS_CREDENTIALS_ACTION.search(step)
    ]

    assert credential_steps
    for step in credential_steps:
        step_body = step.split("\n      - name:", maxsplit=1)[0]
        assert "action-timeout-s: 180" in step_body


def test_release_smoke_installs_the_same_api_dependencies_as_the_runtime() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    smoke = _job_block(workflow, "run-tests")

    assert "requirements/requirements.txt" in smoke
    assert "requirements/api.txt" in smoke
    assert (
        "pip install -r requirements/requirements.txt -r requirements/api.txt"
        in smoke
    )


def _job_block(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        workflow,
    )
    assert match, f"workflow job not found: {job_name}"
    return match.group(1)


def test_prune_discovery_is_closed_world_and_scoped() -> None:
    source = PRUNE.read_text(encoding="utf-8-sig")
    discovery = source.split("function Get-PruneCandidateName", maxsplit=1)[0]

    assert "$script:PruneLegacyAllowlist" in discovery
    assert '"--compute-environments"' in discovery
    assert '"--job-queues"' in discovery
    assert '"--job-definition-name"' in discovery
    assert '("events", "describe-rule", "--name", $ruleName' in discovery

    account_wide_queries = (
        '("iam", "list-roles"',
        '("autoscaling", "describe-auto-scaling-groups", "--region"',
        '("ecs", "list-clusters"',
        '("ec2", "describe-addresses", "--region"',
        '("ssm", "get-parameters-by-path"',
        '("ecr", "describe-repositories", "--region"',
        '("events", "list-rules"',
    )
    for unsafe_query in account_wide_queries:
        assert unsafe_query not in discovery

    assert "Assert-PruneCandidatesAllowlisted -Candidates $cand" in source
    assert "Assert-PruneCandidatesAllowlisted -Candidates $Candidates" in source


def test_prune_ssot_protects_canonical_and_domain_scheduler_resources() -> None:
    source = SSOT.read_text(encoding="utf-8-sig")

    assert "$script:SSOT_ECR = @($script:EcrBaseRepo," in source
    assert "$script:SSOT_SSM = @($script:SsmApiEnv, $script:SsmWorkersEnv, $script:DeployLockParamName)" in source
    assert "$script:SSOT_ProtectedEventBridgeRule" in source
    for scheduler_rule in (
        "academy-v1-process-billing",
        "academy-v1-process-scheduled-notifications",
        "academy-v1-send-clinic-reminders",
        "academy-v1-purge-soft-deleted",
    ):
        assert f'"{scheduler_rule}"' in source


def test_cleanup_orphan_scheduler_is_protected_but_not_purged() -> None:
    ssot = SSOT.read_text(encoding="utf-8-sig")
    prune = PRUNE.read_text(encoding="utf-8-sig")
    managed = ssot.split("$script:SSOT_EventBridgeRule = @(", maxsplit=1)[1].split(
        "\n    )", maxsplit=1
    )[0]
    external = ssot.split("$script:SSOT_ExternalEventBridgeRule = @(", maxsplit=1)[
        1
    ].split("\n    )", maxsplit=1)[0]

    assert "$script:EventBridgeCleanupOrphanRule" not in managed
    assert "$script:EventBridgeCleanupOrphanRule" in external
    assert '$plan["EventBridge Rules"] = @($script:SSOT_EventBridgeRule)' in prune


def test_verify_only_previews_prune_legacy() -> None:
    source = VERIFY.read_text(encoding="utf-8-sig")
    prune_arg_lines = [line for line in source.splitlines() if "PruneLegacy = $true" in line]

    assert prune_arg_lines
    assert all("Plan = $true" in line for line in prune_arg_lines)
    assert "No deletes executed" in source


def test_worker_deploy_jobs_require_migration_gate() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    for job_name in ("deploy-messaging", "deploy-ai", "deploy-tools", "deploy-video"):
        block = _job_block(workflow, job_name)
        assert (
            "needs: [detect-changes, build-and-push, verify-api-preprod, "
            "run-migrations]"
        ) in block
        assert "always() &&" in block
        assert "needs.build-and-push.result == 'success'" in block
        assert "needs.verify-api-preprod.result == 'success'" in block
        assert "needs.run-migrations.result == 'success'" in block
        assert "needs.run-migrations.result == 'skipped'" in block


def test_workflow_pins_build_inputs_migration_and_asg_runtime_images() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    prepare = _job_block(workflow, "prepare-build")
    runtime_build = _job_block(workflow, "build-runtime-images")
    migrations = _job_block(workflow, "run-migrations")

    assert "Resolve immutable academy-base build input" in prepare
    assert 'BASE_URI="${{ env.ECR_REGISTRY }}/academy-base@${BASE_DIGEST}"' in prepare
    assert "base_image_uri: ${{ steps.base-image.outputs.uri }}" in prepare
    assert "fail-fast: false" in runtime_build
    assert runtime_build.count("repository: academy-") == 5
    assert "build-args: BASE_IMAGE=${{ needs.prepare-build.outputs.base_image_uri }}" in runtime_build
    assert 'IMAGE_TAG="${{ env.RELEASE_IMAGE_TAG }}"' in migrations
    assert 'ECR_IMAGE="${ECR_HOST}/academy-api@${IMAGE_DIGEST}"' in migrations
    assert "academy-api:latest" not in migrations
    assert _pinned_action_position(migrations, PINNED_CHECKOUT_ACTION) < migrations.index(
        "deployment_lock.py renew"
    )

    service_jobs = {
        "deploy-api": "api",
        "deploy-messaging": "messaging",
        "deploy-ai": "ai",
        "deploy-tools": "tools",
    }
    for job_name, service in service_jobs.items():
        block = _job_block(workflow, job_name)
        assert "pwsh ./scripts/v1/pin-asg-image.ps1" in block
        assert f"-Service {service}" in block
        assert '-ImageTag "${{ env.RELEASE_IMAGE_TAG }}"' in block
        assert block.index("pin-asg-image.ps1") < block.index("start-instance-refresh")


def test_release_artifact_is_downloaded_under_the_same_common_root() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    build = _job_block(workflow, "build-and-push")
    verify = _job_block(workflow, "verify-deployment")
    assert "name: ci-build-report" in build
    assert "docs/reports/release-manifest.candidate.json" in build
    download = verify.split("Download exact release candidate", maxsplit=1)[1].split(
        "- name:", maxsplit=1
    )[0]
    assert "path: docs/reports" in download
    assert "docs/reports/release-manifest.candidate.json" in verify


def test_asg_pin_state_is_compensated_and_zero_desired_is_verified() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    pin = PIN_ASG_IMAGE.read_text(encoding="utf-8-sig")
    for field in (
        "AsgVersionReference", "DefaultVersion", "PreviousVersion",
        "PreviousDigest", "PreviousRuntimeDigest", "PreviousUserData", "TargetDigest", "NewVersion",
        "PreviousMinSize", "PreviousDesiredCapacity", "PreviousMaxSize",
    ):
        assert field in pin
    assert "cancel-instance-refresh" in pin
    assert "create-launch-template-version" in pin
    assert '"--source-version", [string]$state.PreviousVersion' in pin
    restore_block = pin.split("if ($needsVersionRestore)", maxsplit=1)[1].split(
        "$defaultResult", maxsplit=1
    )[0]
    assert '"--launch-template-data", $restoreDataRef' in restore_block
    assert "Remove-TempFiles @($restoreDataFile)" in restore_block
    assert "DefaultVersion" in pin and "default version changed" in pin
    assert 'if ($desired -eq 0)' in pin
    assert "launchTemplateDigest=$ltDigest" in pin
    assert '"RollbackSuccessful"' in pin
    assert "function Wait-AsgRuntimeInventory" in pin
    assert "healthyInService=$($instances.Count) desired=$desired" in pin
    assert "Wait-AsgRuntimeInventory -AsgName $deployment.ASG" in pin
    assert "TimeoutSec = 600" in pin
    for job_name, service in {
        "deploy-api": "api", "deploy-messaging": "messaging",
        "deploy-ai": "ai", "deploy-tools": "tools",
    }.items():
        block = _job_block(workflow, job_name)
        assert f"/tmp/{service}-pin-state.json" in block
        assert "-VerifyStatePath" in block
        assert "-RestoreStatePath" in block
        assert "if: failure() && steps.pin.outcome != 'skipped'" in block
        assert f"if [ ! -f /tmp/{service}-pin-state.json ]" in block


def test_selective_build_graph_covers_shared_runtime_and_copied_inputs() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    detect = _job_block(workflow, "detect-changes")
    force_patterns = re.findall(
        r'changed_matches "([^"]+)" && FORCE_ALL=true', detect
    )
    assert 'changed_matches() { grep -qE "$1" <<< "$CHANGED"; }' in detect
    assert 'echo "$CHANGED" | grep -qE' not in detect

    shared_runtime_changes = (
        ".dockerignore",
        "docs/ssot/params.yaml",
        "academy/application/use_cases/example.py",
        "libs/queue/client.py",
        "apps/shared/contracts/example.py",
        "apps/support/submissions/dependencies.py",
        "apps/core/models.py",
        "apps/api/common/middleware.py",
        "apps/infrastructure/storage/r2.py",
        "apps/api/config/settings/worker.py",
        "apps/api/config/settings/__init__.py",
        "apps/worker/__init__.py",
        "apps/domains/students/models/profile.py",
        "apps/domains/messaging/apps.py",
        "apps/domains/video/signals/handlers.py",
        "manage.py",
        "requirements/constraints.txt",
    )
    for path in shared_runtime_changes:
        assert any(re.search(pattern, path) for pattern in force_patterns), path

    api_section = detect.split("# API:", maxsplit=1)[1].split(
        "# Video worker", maxsplit=1
    )[0]
    video_section = detect.split("# Video worker", maxsplit=1)[1].split(
        "# Messaging worker", maxsplit=1
    )[0]
    ai_section = detect.split("# AI worker", maxsplit=1)[1].split(
        "# Tools worker", maxsplit=1
    )[0]
    assert 'changed_matches "^scripts/" && API=true' in api_section
    assert 'changed_matches "^requirements/requirements\\.txt$" && API=true' in api_section
    assert 'changed_matches "^requirements/requirements\\.txt$" && VIDEO=true' in video_section
    assert not any(
        re.search(pattern, "requirements/requirements.txt")
        for pattern in force_patterns
    )
    assert 'changed_matches "^scripts/" && AI=true' in ai_section
    assert 'changed_matches "^models/" && AI=true' in ai_section


def test_selective_build_diffs_from_each_last_verified_runtime_image() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    detect = _job_block(workflow, "detect-changes")

    assert "github.event.before" not in detect
    assert (
        'MANIFEST="$RUNNER_TEMP/release-baseline/release-manifest.latest.json"'
        in detect
    )
    assert "resolve_image_base()" in detect
    assert 'git merge-base --is-ancestor "$resolved" HEAD' in detect
    assert 'CHANGED_RELEASE=$(git diff --name-only "$RELEASE_PREV" HEAD)' in detect
    for flag, repo in {
        "BASE": "academy-base",
        "API": "academy-api",
        "VIDEO": "academy-video-worker",
        "MSG": "academy-messaging-worker",
        "AI": "academy-ai-worker-cpu",
        "TOOLS": "academy-tools-worker",
    }.items():
        assert f'{flag}_PREV=$(resolve_image_base "{repo}")' in detect
        assert f'CHANGED_{flag}=$(git diff --name-only "${flag}_PREV" HEAD)' in detect
        if flag in {"API", "AI"}:
            assert f'CHANGED="$(runtime_changes "$CHANGED_{flag}")"' in detect
        else:
            assert f'CHANGED="$CHANGED_{flag}"' in detect
    assert 'force_full_build "academy-tools-worker source commit is unavailable"' in detect


def test_queued_release_uses_manifest_promoted_after_concurrency_wait() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    detect = _job_block(workflow, "detect-changes")
    prepare = _job_block(workflow, "prepare-build")
    assemble = _job_block(workflow, "build-and-push")

    baseline = "$RUNNER_TEMP/release-baseline/release-manifest.latest.json"
    assert "Capture current verified release baseline" in detect
    assert "git fetch --no-tags origin main" in detect
    assert (
        'git show origin/main:docs/reports/release-manifest.latest.json '
        f'> "{baseline}"'
    ) in detect
    assert f'MANIFEST="{baseline}"' in detect
    assert "name: release-baseline" in detect
    assert detect.index("Capture current verified release baseline") < detect.index(
        "Detect changes"
    )
    for block in (prepare, assemble):
        assert "Download current verified release baseline" in block
        assert "name: release-baseline" in block
        assert "path: ${{ runner.temp }}/release-baseline" in block
    assert f'PRIOR="{baseline}"' in assemble


def test_deploy_freshness_uses_immutable_runtime_evidence_not_latest() -> None:
    source = DEPLOY_AND_VERIFY.read_text(encoding="utf-8-sig")

    assert "imageTag=latest" not in source
    assert '"imageDigest=$ciDigest"' in source
    assert "Get-AsgRuntimeImageEvidence" in source
    assert "Get-BatchRuntimeImageEvidence" in source
    assert 'Repo="academy-tools-worker"' in source
    assert "$script:SSOT_JobDef" in source
    assert "runtime digest mismatch" in source
    assert source.count("models/|scripts/") == 2
    assert source.index("Stage 0 immutable image evidence failed") < source.index(
        "start-instance-refresh"
    )


def test_ci_report_digest_parser_does_not_require_latest_alias(tmp_path: Path) -> None:
    digest = "sha256:" + ("c" * 64)
    report = tmp_path / "ci-build.md"
    report.write_text(
        "\n".join(
            (
                "**gitSha:** deadbeef",
                "| repo | tags | imageDigest |",
                "|---|---|---|",
                f"| academy-api | sha-deadbeef | {digest} |",
            )
        ),
        encoding="utf-8",
    )
    api_path = str(API_RESOURCE).replace("'", "''")
    report_path = str(report).replace("'", "''")
    script = rf"""
. '{api_path}'
$result = Get-CiBuildImageDigests -Path '{report_path}'
if ($result.GitSha -ne "deadbeef") {{ exit 41 }}
if ($result.Digests["academy-api"] -ne "{digest}") {{ exit 42 }}
exit 0
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_immutable_evidence_and_selective_build_docs_match_execution() -> None:
    architecture = DEPLOY_ARCH_DOC.read_text(encoding="utf-8")
    readme = V1_README.read_text(encoding="utf-8")

    assert "`:latest` — compatibility alias only; moved only after every production" in architecture
    assert "exact digest readback succeeds" in architecture
    assert "actual InService containers, and every active Video Batch job definition" in architecture
    assert "apps/{shared,support,core,infrastructure}/" in architecture
    assert "Django startup import" in architecture
    assert "refresh는 terminal `Successful`까지 기다리며" in readme
    assert "selective-build 안전 경계" in readme


def test_ssot_and_deploy_scripts_enforce_digest_runtime_contract() -> None:
    params = PARAMS.read_text(encoding="utf-8")
    userdata = WORKER_USERDATA.read_text(encoding="utf-8-sig")
    api = API_RESOURCE.read_text(encoding="utf-8-sig")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8-sig")
    deploy = DEPLOY.read_text(encoding="utf-8-sig")
    pin = PIN_ASG_IMAGE.read_text(encoding="utf-8-sig")
    iam = IAM_RESOURCE.read_text(encoding="utf-8-sig")
    drift = DIFF.read_text(encoding="utf-8-sig")

    assert "useLatestTag: false" in params
    assert "immutableTagRequired: true" in params
    assert "@sha256:[0-9a-f]{64}$" in userdata
    assert "[0-9a-f]{40}-run-[0-9]+-[0-9]+" in userdata
    assert "Get-ImmutableEcrImageUri -RepoName $repo" in api
    assert "Get-ReleaseManifestImage" in bootstrap
    assert "EcrRepoUri is not immutable" in deploy

    for service in ("api", "messaging", "ai", "tools"):
        assert f'"{service}"' in pin
    assert '"--source-version", \'$Latest\'' in pin
    assert "digest baseline" in pin
    assert "originalAsgVersionReference" in pin
    assert "move $Service ASG to digest-pinned latest Launch Template" in pin
    assert '"ec2", "create-launch-template-version"' in pin
    assert '"ec2", "modify-launch-template"' not in pin
    normal_pin = pin.split('if (-not $ImageTag)', maxsplit=1)[1]
    assert '"autoscaling", "update-auto-scaling-group"' in normal_pin
    assert '"autoscaling", "update-auto-scaling-group"' in pin.split(
        "if ($RestoreStatePath)", maxsplit=1
    )[1].split('if (-not $ImageTag)', maxsplit=1)[0]
    assert "ec2:CreateLaunchTemplateVersion" in iam
    assert "LaunchTemplateImagePinRead" in iam
    assert "LaunchTemplateImagePinWrite" in iam
    assert "@$($releaseImage.Digest)" in drift
    assert "immutableTagRequired=true cannot use :latest" in drift
    assert "$apiVersionSelector = if ($script:EcrImmutableTagRequired) { '$Latest' }" in drift
    assert '"--versions", $apiVersionSelector' in drift
    assert "[string]$apiLtReference.Version -ne '$Latest'" in drift


def test_rollbacks_pin_the_selected_digest_before_instance_refresh() -> None:
    shared = ROLLBACK_ASG.read_text(encoding="utf-8-sig")
    assert "STATEFUL_IMAGE_ROLLBACK_BLOCKED" in shared
    assert '$Service -in @("api", "messaging")' in shared
    assert "Roll forward by building the desired source" in shared
    assert shared.index("STATEFUL_IMAGE_ROLLBACK_BLOCKED") < shared.index(
        "Get-CurrentRuntimeDigest"
    )
    assert "Get-CurrentRuntimeDigest" in shared
    assert "No prior immutable image exists" in shared
    assert "pin-asg-image.ps1" in shared
    assert "MinHealthyPercentage=100" in shared
    assert "MaxHealthyPercentage=200" in shared
    assert "Wait-Refresh" in shared
    assert "Assert-Runtime" in shared
    assert "exactly one distinct" in shared
    assert "must have been pushed before the current runtime" in shared
    assert "aws ecr put-image" not in shared
    assert shared.index("pin-asg-image.ps1") < shared.index("start-instance-refresh")

    for service, path in ROLLBACK_SCRIPTS.items():
        source = path.read_text(encoding="utf-8-sig")

        assert "rollback-asg.ps1" in source
        assert f"-Service {service}" in source
        assert "-ImageTag $Sha" in source
        assert "aws ecr put-image" not in source

    video = ROLLBACK_VIDEO.read_text(encoding="utf-8-sig")
    assert "Video rollback requires exactly eight" in video
    assert "No prior immutable video image exists" in video
    assert "must have been pushed before the current runtime" in video
    assert "register-job-definition" in video
    assert "VALID" in video and "ENABLED" in video


@pytest.mark.parametrize("service", ["api", "messaging"])
def test_stateful_image_rollback_fails_closed_before_aws(service: str) -> None:
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROLLBACK_ASG),
            "-Service",
            service,
            "-AwsProfile",
            "",
            "-WhatIf",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    output = completed.stdout + completed.stderr
    normalized_output = " ".join(
        re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).split()
    )
    assert completed.returncode != 0
    assert f"STATEFUL_IMAGE_ROLLBACK_BLOCKED service={service}" in normalized_output
    assert re.search(r"immutable\s+(?:\|\s*)?release image", normalized_output)


def test_remote_api_tools_reuse_the_running_digest_pinned_image() -> None:
    for path in REMOTE_API_TOOLS:
        source = path.read_text(encoding="utf-8-sig")

        assert "docker inspect --format '{{.Config.Image}}' academy-api" in source
        assert "@sha256:[0-9a-f]{64}$" in source
        assert "academy-api:latest" not in source


def test_qna_e2e_wrapper_uses_tenant_scoped_cleanup_safe_command() -> None:
    source = REMOTE_API_TOOLS[0].read_text(encoding="utf-8-sig")

    assert "python manage.py verify_qna_e2e_safe --tenant-id 1" in source
    assert '[verify_qna_e2e_safe] ALL CHECKS PASSED' in source
    assert "python manage.py verify_qna_e2e 2>&1" not in source
    assert "$script:FrontDomainApi" in source
    assert 'StartsWith("https://"' in source
    assert '"http://$albDns"' not in source


def test_digest_resolution_and_userdata_render_with_mocked_ecr() -> None:
    userdata_path = str(WORKER_USERDATA).replace("'", "''")
    script = rf"""
$script:AccountId = "123456789012"
$script:Region = "ap-northeast-2"
$script:EcrUseLatestTag = $false
$script:EcrImmutableTagRequired = $true
$script:Calls = 0
function Invoke-AwsJson {{
    param([object[]]$Arguments)
    $script:Calls++
    return [PSCustomObject]@{{ imageDetails = @([PSCustomObject]@{{
        imageDigest = "sha256:$('a' * 64)"
        imageTags = @("sha-deadbeef", "latest")
        imagePushedAt = "2026-07-13T00:00:00Z"
    }}) }}
}}
. '{userdata_path}'

$uri = Get-ImmutableEcrImageUri -RepoName "academy-messaging-worker" -ImageTag "sha-deadbeef"
if ($uri -ne "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/academy-messaging-worker@sha256:$('a' * 64)") {{ exit 21 }}
$rendered = Get-WorkerLaunchTemplateUserData -ImageUri $uri -Region $script:Region -SsmParam "/academy/workers/env" -ContainerName "academy-messaging-worker"
if ($rendered -notmatch [regex]::Escape($uri)) {{ exit 22 }}
if ($rendered -notmatch 'if \[ -n "" \]') {{ exit 26 }}
$aiRendered = Get-WorkerLaunchTemplateUserData -ImageUri $uri -Region $script:Region -SsmParam "/academy/workers/env" -ContainerName "academy-ai-worker-cpu" -LogGroup "/academy/ai-worker"
if ($aiRendered -notmatch 'if \[ -n "/academy/ai-worker" \]') {{ exit 30 }}
if ($aiRendered -notmatch "--log-driver awslogs") {{ exit 27 }}
if ($aiRendered -notmatch "awslogs-group=/academy/ai-worker") {{ exit 28 }}
if ($aiRendered -notmatch "awslogs-stream=academy-ai-worker-cpu/") {{ exit 29 }}
if ($aiRendered -notmatch "X-aws-ec2-metadata-token-ttl-seconds: 21600") {{ exit 31 }}
if ($aiRendered -notmatch 'X-aws-ec2-metadata-token: \$IMDS_TOKEN') {{ exit 32 }}
if ($aiRendered -notmatch 'IMDSv2 instance id unavailable') {{ exit 33 }}
if ([regex]::Matches($aiRendered, "latest/meta-data/instance-id").Count -ne 1) {{ exit 34 }}
$toolsRendered = Get-WorkerLaunchTemplateUserData -ImageUri $uri -Region $script:Region -SsmParam "/academy/workers/env" -ContainerName "academy-tools-worker" -LogGroup "/academy/tools-worker"
if ($toolsRendered -notmatch "awslogs-group=/academy/tools-worker") {{ exit 35 }}
if ($toolsRendered -notmatch "awslogs-stream=academy-tools-worker/") {{ exit 36 }}

try {{
    Get-ImmutableEcrImageUri -RepoName "academy-messaging-worker" -ImageTag "latest"
    exit 23
}} catch {{
    if ($_.Exception.Message -notmatch "Only CI sha") {{ exit 24 }}
}}
if ($script:Calls -ne 1) {{ exit 25 }}
exit 0
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_api_userdata_uses_imdsv2_scoped_cloudwatch_logging() -> None:
    api_path = str(API_RESOURCE).replace("'", "''")
    script = rf"""
. '{api_path}'
$rendered = Get-ApiLaunchTemplateUserData -ApiImageUri "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/academy-api@sha256:$('a' * 64)" -Region "ap-northeast-2" -SsmApiEnvParam "/academy/api/env" -LogGroup "/academy/api"
if ($rendered -notmatch "X-aws-ec2-metadata-token-ttl-seconds: 21600") {{ exit 41 }}
if ($rendered -notmatch 'X-aws-ec2-metadata-token: \$IMDS_TOKEN') {{ exit 42 }}
if ($rendered -notmatch "awslogs-group=/academy/api") {{ exit 43 }}
if ($rendered -notmatch "awslogs-stream=academy-api/") {{ exit 44 }}
if ($rendered -notmatch "refusing untraceable CloudWatch API logging") {{ exit 45 }}
exit 0
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runtime_container_cloudwatch_policy_is_exact_and_write_only() -> None:
    policy = json.loads(EC2_CLOUDWATCH_LOGS_POLICY.read_text(encoding="utf-8"))
    statement = policy["Statement"][0]

    assert set(statement["Action"]) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
    }
    assert statement["Resource"] == [
        "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/api",
        "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/api:log-stream:*",
        "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/ai-worker",
        "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/ai-worker:log-stream:*",
        "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/tools-worker",
        "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/tools-worker:log-stream:*",
    ]


def test_runtime_container_log_groups_are_ensured_and_wired() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8-sig")
    cloudwatch = CLOUDWATCH_RESOURCE.read_text(encoding="utf-8-sig")
    asg_tools = ASG_TOOLS_RESOURCE.read_text(encoding="utf-8-sig")
    pin_asg_image = PIN_ASG_IMAGE.read_text(encoding="utf-8-sig")

    assert "$script:ApiLogGroup" in cloudwatch
    assert "$script:AiWorkerLogGroup" in cloudwatch
    assert "$script:ToolsWorkerLogGroup" in cloudwatch
    assert "-LogGroup $script:ToolsWorkerLogGroup" in asg_tools
    assert "-LogGroup $script:ApiLogGroup" in pin_asg_image
    assert "-LogGroup $script:ToolsWorkerLogGroup" in pin_asg_image
    assert deploy.index("Ensure-RuntimeLogRetention") < deploy.index("Ensure-API")
    assert deploy.index("Ensure-RuntimeLogRetention") < deploy.index("Ensure-ASGAi")


def test_drift_contract_compares_the_resolved_api_digest() -> None:
    diff_path = str(DIFF).replace("'", "''")
    script = rf"""
$script:AccountId = "123456789012"
$script:Region = "ap-northeast-2"
$script:EcrApiRepo = "academy-api"
$script:EcrUseLatestTag = $false
$script:EcrImmutableTagRequired = $true
function Invoke-AwsJson {{
    param([object[]]$Arguments)
    return [PSCustomObject]@{{ imageDetails = @([PSCustomObject]@{{
        imageDigest = "sha256:$('b' * 64)"
        imageTags = @("sha-feedface", "latest")
        imagePushedAt = "2026-07-13T00:00:00Z"
    }}) }}
}}
function Get-ReleaseManifestImage {{
    param([string]$RepoName)
    return [PSCustomObject]@{{ Digest = "sha256:$('b' * 64)" }}
}}
. '{diff_path}'

$uri = Get-ExpectedApiImageUriForDrift
if ($uri -ne "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/academy-api@sha256:$('b' * 64)") {{ exit 31 }}
$script:EcrUseLatestTag = $true
try {{
    Get-ExpectedApiImageUriForDrift
    exit 32
}} catch {{
    if ($_.Exception.Message -notmatch "immutableTagRequired=true") {{ exit 33 }}
}}
exit 0
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_prune_delete_guard_and_failure_aggregation_with_mocked_aws() -> None:
    prune_path = str(PRUNE).replace("'", "''")
    script = rf"""
$script:Region = "test-region"
$script:PruneLegacyAllowlist = @{{
    "Batch CE" = @(); "Batch Queue" = @(); "Batch JobDef" = @()
    "EventBridge Rule" = @("legacy-one", "legacy-two")
    "IAM Role" = @(); "ASG" = @(); "ECS Cluster" = @()
    "EIP" = @(); "SSM" = @(); "ECR" = @()
}}
$script:SSOT_CE = @(); $script:SSOT_Queue = @(); $script:SSOT_JobDef = @()
$script:SSOT_ProtectedEventBridgeRule = @("canonical-rule")
$script:SSOT_IAMRoles = @(); $script:SSOT_ASG = @(); $script:SSOT_ECSClusterPatterns = @()
$script:SSOT_EIP = @(); $script:SSOT_SSM = @(); $script:SSOT_ECR = @("academy-base")
$script:InvokeCalls = 0

function Write-Warn {{ param([string]$Message) }}
function Invoke-AwsJson {{
    param([object[]]$Arguments)
    return [PSCustomObject]@{{ Targets = @() }}
}}
function Invoke-Aws {{
    param([object[]]$Arguments, [string]$ErrorMessage)
    $script:InvokeCalls++
    throw "mock delete failure"
}}

. '{prune_path}'

function New-Candidates {{
    return @{{
        "Batch CE" = @(); "Batch Queue" = @(); "Batch JobDef" = @()
        "EventBridge Rule" = @(); "IAM Role" = @(); "ASG" = @()
        "ECS Cluster" = @(); "EIP" = @(); "SSM" = @(); "ECR" = @()
    }}
}}

$blocked = New-Candidates
$blocked["EventBridge Rule"] = @("unrelated-account-rule")
try {{
    Invoke-PruneLegacyDeletes -Candidates $blocked
    exit 11
}}
catch {{
    if ($_.Exception.Message -notmatch "not explicitly allowlisted") {{ exit 12 }}
    if ($script:InvokeCalls -ne 0) {{ exit 13 }}
}}

$script:PruneLegacyAllowlist["ECR"] = @("academy-base")
$protected = New-Candidates
$protected["ECR"] = @("academy-base")
try {{
    Invoke-PruneLegacyDeletes -Candidates $protected
    exit 14
}}
catch {{
    if ($_.Exception.Message -notmatch "protected by SSOT") {{ exit 15 }}
    if ($script:InvokeCalls -ne 0) {{ exit 16 }}
}}

$failing = New-Candidates
$failing["EventBridge Rule"] = @("legacy-one", "legacy-two")
try {{
    Invoke-PruneLegacyDeletes -Candidates $failing
    exit 17
}}
catch {{
    if ($script:InvokeCalls -ne 2) {{ exit 18 }}
    if ($_.Exception.Message -notmatch "failed to delete 2 resource") {{ exit 19 }}
}}
exit 0
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_selective_build_covers_cross_domain_worker_import_edges() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    detect = _job_block(workflow, "detect-changes")

    def patterns(flag: str) -> list[str]:
        return re.findall(rf'changed_matches "([^"]+)" && {flag}=true', detect)

    expected = {
        "VIDEO": (
            "apps/domains/messaging/selectors.py",
            "apps/domains/messaging/scheduled.py",
            "apps/domains/messaging/services/notification_dispatch.py",
            "apps/domains/messaging/alimtalk_content_builders.py",
            "apps/domains/messaging/policy.py",
        ),
        "MSG": ("apps/domains/video/redis_status_cache.py",),
        "TOOLS": (
            "apps/domains/ai/callbacks.py",
            "apps/domains/ai/job_types.py",
            "apps/domains/ai/gateway.py",
            "apps/domains/results/services/wrong_note_pdf_service.py",
            "apps/domains/results/services/wrong_note_pdf_worker.py",
            "apps/domains/results/services/wrong_note_service.py",
            "apps/domains/results/services/answer_matching.py",
            "apps/domains/assets/omr/renderer/fonts/NotoSansKR-Regular.ttf",
            "apps/domains/submissions/services/ai_omr_result_mapper.py",
            "apps/domains/submissions/services/lifecycle.py",
            "apps/support/ai/callback_dependencies.py",
            "academy/adapters/cache/redis_progress_adapter.py",
        ),
    }
    for flag, paths in expected.items():
        detector_patterns = patterns(flag)
        for path in paths:
            assert any(re.search(pattern, path) for pattern in detector_patterns), (
                flag,
                path,
            )


def test_ecr_repositories_are_latest_only_mutable_and_verified() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    prepare = _job_block(workflow, "prepare-build")
    ecr = ECR_RESOURCE.read_text(encoding="utf-8-sig")

    for source in (prepare, ecr):
        assert "IMMUTABLE_WITH_EXCLUSION" in source
        assert "filterType=WILDCARD,filter=latest" in source
        assert "put-image-tag-mutability" in source
        assert "imageTagMutabilityExclusionFilters" in source
    assert "RELEASE_IMAGE_TAG: sha-${{ github.sha }}-run-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "academy-video-worker@${IMAGE_DIGEST}" in _job_block(
        workflow, "deploy-video"
    )


def test_release_manifest_is_complete_exact_and_promoted_only_after_runtime_gates() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    build = _job_block(workflow, "build-and-push")
    verify = _job_block(workflow, "verify-deployment")
    userdata = WORKER_USERDATA.read_text(encoding="utf-8-sig")

    assert "imageTag=latest" not in build.split(
        "Collect exact release digests", maxsplit=1
    )[1]
    assert "prior-success" in build
    assert "Release candidate is incomplete" in build
    assert 'status:"candidate",complete:false' in build
    assert "Verify actual ASG container digests" in verify
    assert "Verify required Video Batch runtimes" in verify
    assert "Promote verified complete release manifest" in verify
    assert verify.index("Verify actual ASG container digests") < verify.index(
        "Promote verified complete release manifest"
    )
    assert '.status="successful" | .complete=true' in verify
    assert "release-manifest.latest.json" in userdata
    assert "complete verified six-image release" in userdata
    assert "newest sha" not in userdata.lower()


def test_runtime_digest_gate_waits_for_fresh_autoscale_instances() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    verify = _job_block(workflow, "verify-deployment")

    assert "RUNTIME_DIGEST_MAX_ATTEMPTS=18" in verify
    assert "runtime readiness attempt" in verify
    assert 'sleep 10' in verify
    assert "did not expose the expected immutable runtime digest" in verify
    assert verify.index("RUNTIME_DIGEST_MAX_ATTEMPTS=18") < verify.index(
        "Promote verified complete release manifest"
    )


def test_video_and_messaging_deploys_fail_closed_on_runtime_preconditions() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    video = _job_block(workflow, "deploy-video")
    messaging = _job_block(workflow, "deploy-messaging")

    assert "Required job definition $JOBDEF_NAME not found" in video
    assert '"$SUCCESS_COUNT" -eq 0' in video
    assert '"$SUCCESS_COUNT" -ne "$EXPECTED_COUNT"' in video
    assert "runtime image mismatch" in video
    assert "Required compute environment $CE" in video
    assert video.index("before Video rollout") < video.index("register-job-definition")
    assert "compensate_registered" in video
    assert "all successful revisions were compensated" in video
    for preserved_option in (
        "--retry-strategy",
        "--timeout",
        "--parameters",
        "--tags",
        "--scheduling-priority",
    ):
        assert preserved_option in video
    assert "::warning::$CE" not in video
    assert '"MinHealthyPercentage":100' in messaging
    assert '"MaxHealthyPercentage":200' in messaging


def test_deploy_verifier_waits_terminal_and_checks_exact_running_and_batch_uris() -> None:
    source = DEPLOY_AND_VERIFY.read_text(encoding="utf-8-sig")

    assert "Wait-InstanceRefreshTerminal" in source
    assert 'if ([string]$state.Status -eq "Successful")' in source
    assert '"Failed", "Cancelled", "RollbackFailed"' in source
    assert "Instance refresh timed out" in source
    assert "Assert-AsgRunningContainerDigests" in source
    assert "docker image inspect" in source
    assert "healthy InService count does not equal desired capacity" in source
    assert "actual runtime does not" not in source
    assert "exact account/region/repository digest URI" in source
    assert "must report exactly one account/region/repository digest URI" in source
    assert ".dockerignore$" in source


def test_asg_pin_retries_only_the_expected_missing_runtime_container() -> None:
    source = PIN_ASG_IMAGE.read_text(encoding="utf-8-sig")
    retry_block = source.split(
        "for ($startupElapsed = 0; $startupElapsed -le 300; $startupElapsed += 15)",
        maxsplit=1,
    )[1].split("$actual =", maxsplit=1)[0]

    assert "Start-Sleep -Seconds 15" in retry_block
    assert '$isMissingExpectedContainer' in retry_block
    assert '[string]$result.Status -eq "Failed"' in retry_block
    assert "[string]::IsNullOrWhiteSpace($stdout)" in retry_block
    assert "No such (?:object|container)" in retry_block
    assert "[Regex]::Escape($container)" in source
    assert "Runtime container did not become inspectable within 300s" in retry_block
    assert "Runtime verification command timed out" in retry_block
    digest_check = source.split("$actual =", maxsplit=1)[1]
    assert "Runtime digest mismatch" in digest_check


def test_iam_managed_write_statements_converge_exactly_and_read_back() -> None:
    source = IAM_RESOURCE.read_text(encoding="utf-8-sig")

    assert "Expected exactly four SSOT ASGs" in source
    assert "$asgStatement.Resource = $requiredAsgArns" in source
    assert "$writeStatement.Resource = $ltArns" in source
    assert "Expected exactly six SSOT ECR repositories" in source
    assert '"ecr:PutImageTagMutability"' in source
    assert "$duplicates.Count -gt 1" in source
    assert "$statements.Remove($duplicate)" in source
    assert "IAM readback mismatch for managed statement" in source
    assert "currentAsgArns +" not in source
    assert "currentLtArns +" not in source


def test_exact_workflow_iam_covers_full_contract_without_broad_ssm() -> None:
    source = IAM_RESOURCE.read_text(encoding="utf-8-sig")
    static = STATIC_GHA_IAM.read_text(encoding="utf-8")
    required_sids = (
        "EcrAuth", "EcrPushPull", "EcrRepoManage", "AsgInstanceRefresh",
        "AsgDescribe", "LaunchTemplateImagePinRead", "LaunchTemplateImagePinWrite",
        "LaunchTemplateInstanceUse", "LaunchTemplateInstanceTag",
        "LaunchTemplatePassRole", "RuntimeScalePolicyReadback",
        "RuntimeLogSinkReadback", "ProductUsagePilotLogQuery",
        "ApiCanaryInstanceRead", "ApiCanaryInstanceCleanup",
        "ApiCanaryProfileRead", "ApiCanarySsmRead",
        "SsmSendDocument", "SsmSendInstances", "SsmSendApiCanary",
        "SsmCommandRead", "ApiPreprodEnvSourceRead", "ApiPreprodEnvPublish",
        "ProductUsagePilotEnvPublish",
        "DevAlertsAlarmRead", "DevAlertsParameterRead",
        "DevAlertsTransitionWrite", "BatchRead",
        "BatchJobDefinitionRegister", "BatchJobDefinitionRevisionWrite",
        "BatchPassRoles", "ElbRead",
        "SnsFailureNotify", "StsIdentity", "DeploymentControlLock",
    )
    exact_function = source.split("function Ensure-GitHubActionsDeployIAM {", maxsplit=1)[1].split(
        "function Ensure-BatchIAM", maxsplit=1
    )[0]
    for sid in required_sids:
        assert f'Sid="{sid}"' in exact_function
        assert f'"Sid":"{sid}"' in static
    for action in (
        "autoscaling:CancelInstanceRefresh", "ec2:RunInstances", "ec2:CreateTags",
        "batch:RegisterJobDefinition",
        "batch:DeregisterJobDefinition", "batch:TagResource", "iam:PassRole",
        "iam:GetRolePolicy",
        "logs:DescribeLogGroups",
        "elasticloadbalancing:DescribeTargetHealth", "sns:Publish",
        "dynamodb:UpdateItem",
    ):
        assert action in exact_function
        assert action in static
    assert "SsmMigration" not in static
    assert "put-role-policy" in exact_function
    assert "full-policy readback" in exact_function

    policy = json.loads(static)
    by_sid = {statement["Sid"]: statement for statement in policy["Statement"]}
    register_resources = by_sid["BatchJobDefinitionRegister"]["Resource"]
    revision_resources = by_sid["BatchJobDefinitionRevisionWrite"]["Resource"]
    assert len(register_resources) == 8
    assert len(revision_resources) == 8
    assert all(resource.count(":job-definition/") == 1 for resource in register_resources)
    assert all(not resource.endswith(":*") for resource in register_resources)
    assert all(resource.endswith(":*") for resource in revision_resources)
    launch_use = by_sid["LaunchTemplateInstanceUse"]
    assert launch_use["Action"] == "ec2:RunInstances"
    assert len([resource for resource in launch_use["Resource"] if ":launch-template/lt-" in resource]) == 4
    assert "*" not in launch_use["Resource"]
    assert set(by_sid["LaunchTemplatePassRole"]["Resource"]) == {
        "arn:aws:iam::809466760795:role/academy-api-preprod-canary-role",
        "arn:aws:iam::809466760795:role/academy-ec2-role",
    }
    assert by_sid["LaunchTemplatePassRole"]["Condition"]["StringEquals"]["iam:PassedToService"] == "ec2.amazonaws.com"
    assert by_sid["RuntimeScalePolicyReadback"]["Action"] == "iam:GetRolePolicy"
    assert by_sid["RuntimeScalePolicyReadback"]["Resource"] == (
        "arn:aws:iam::809466760795:role/academy-ec2-role"
    )
    assert by_sid["RuntimeLogSinkReadback"] == {
        "Sid": "RuntimeLogSinkReadback",
        "Effect": "Allow",
        "Action": "logs:DescribeLogGroups",
        "Resource": "*",
    }
    assert by_sid["ProductUsagePilotLogQuery"] == {
        "Sid": "ProductUsagePilotLogQuery",
        "Effect": "Allow",
        "Action": ["logs:StartQuery", "logs:GetQueryResults"],
        "Resource": "arn:aws:logs:ap-northeast-2:809466760795:log-group:/academy/api:*",
    }
    assert by_sid["ApiCanaryInstanceCleanup"]["Condition"]["StringEquals"] == {
        "ec2:ResourceTag/Name": "academy-v1-api-preprod-canary",
        "ec2:ResourceTag/Project": "academy",
        "ec2:ResourceTag/ManagedBy": "academy-deploy-canary",
    }
    assert by_sid["SsmSendApiCanary"]["Condition"]["StringEquals"] == {
        "ssm:resourceTag/Name": "academy-v1-api-preprod-canary",
        "ssm:resourceTag/Project": "academy",
        "ssm:resourceTag/ManagedBy": "academy-deploy-canary",
    }
    assert by_sid["ApiPreprodEnvSourceRead"]["Action"] == "ssm:GetParameter"
    assert {
        resource.rsplit(":parameter/", maxsplit=1)[-1]
        for resource in by_sid["ApiPreprodEnvSourceRead"]["Resource"]
    } == {
        "academy/api/env",
        "academy/api/preprod/db-credentials",
        "academy/api/preprod/env",
    }
    assert by_sid["ApiPreprodEnvPublish"]["Action"] == "ssm:PutParameter"
    assert by_sid["ApiPreprodEnvPublish"]["Resource"].endswith(
        ":parameter/academy/api/preprod/env"
    )
    assert by_sid["ProductUsagePilotEnvPublish"] == {
        "Sid": "ProductUsagePilotEnvPublish",
        "Effect": "Allow",
        "Action": "ssm:PutParameter",
        "Resource": "arn:aws:ssm:ap-northeast-2:809466760795:parameter/academy/api/env",
    }
    assert "iam:PutRolePolicy" not in static
    assert set(by_sid["BatchPassRoles"]["Condition"]["StringEquals"]["iam:PassedToService"]) == {
        "batch.amazonaws.com", "ecs-tasks.amazonaws.com",
    }


def test_cleanup_and_rollback_preserve_all_durable_runtime_contracts() -> None:
    cleanup = ECR_CLEANUP.read_text(encoding="utf-8")
    rollback = ROLLBACK_VIDEO.read_text(encoding="utf-8-sig")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for name in (
        "academy-v1-video-batch-jobdef", "academy-v1-video-ops-scanstuck",
        "academy-v1-video-ops-reconcile", "academy-v1-video-ops-netprobe",
        "academy-v1-video-ops-enqueue-uploaded", "academy-v1-video-ops-purge-raw",
        "academy-v1-video-ops-detect-stuck", "academy-v1-video-ops-recover-dead",
    ):
        assert name in cleanup
    assert "collect_release_manifest_digests" in cleanup
    assert "required Video job definitions disagree on latest digest" in cleanup
    assert 'manifest.get("status") != "successful"' in cleanup
    assert "set(images) != set(REPOS)" in cleanup
    assert "sys.exit(2)" in cleanup.split("if total_failed > 0:", maxsplit=1)[1]
    for option in (
        "parameters", "tags", "propagateTags", "schedulingPriority",
        "consumableResourceProperties",
    ):
        assert option in rollback
    assert "Acquire-DeployLock" in rollback and "Release-DeployLock" in rollback
    assert ".env*" in dockerignore
    assert "!.env.example" in dockerignore
    assert "!.env.*.example" in dockerignore


def test_first_release_prerequisite_bridge_does_not_mutate_runtime() -> None:
    source = RELEASE_PREREQUISITES.read_text(encoding="utf-8-sig")

    assert "Ensure-ECRRepos" in source
    assert "Ensure-GitHubActionsDeployIAM" in source
    assert "Ensure-DynamoLockTable" in source
    assert source.index("Ensure-DynamoLockTable") < source.index("Ensure-GitHubActionsDeployIAM")
    assert "Acquire-DeployLock" in source
    assert "Release-DeployLock" in source
    assert "foundLaunchTemplates.Count -ne 4" in source
    for forbidden in (
        "start-instance-refresh",
        "create-launch-template-version",
        "register-job-definition",
        "Ensure-API",
        "Ensure-ASG",
    ):
        assert forbidden not in source


def test_fresh_bootstrap_creates_and_validates_lock_before_acquire() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8-sig")
    dynamodb = DYNAMODB_RESOURCE.read_text(encoding="utf-8-sig")
    bootstrap = deploy.split("Assert-NoLegacyScripts", maxsplit=1)[1].split(
        "Invoke-PreflightCheck", maxsplit=1
    )[0]
    assert bootstrap.index("Ensure-DynamoLockTable") < bootstrap.index("Acquire-DeployLock")
    assert "videoId HASH only" in dynamodb
    assert "videoId String only" in dynamodb
    assert 'BillingModeSummary.BillingMode -ne "PAY_PER_REQUEST"' in dynamodb


def test_default_deploy_never_reports_success_after_failed_verification() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8-sig")
    assert "DEPLOY_SUCCESS (verification warnings" not in deploy
    assert "Post-deploy verification failed. Refusing to report deployment success" in deploy
    assert 'elseif ($RelaxedValidation)' in deploy
    assert "DEPLOY_COMPLETED_WITH_VERIFICATION_WARNINGS" in deploy
    assert 'if ($netStatus -eq "failed") { $verifyOk = $false }' in deploy
    assert "Required ASG missing" in deploy
    assert "Required API target group missing" in deploy
    assert "Required Video compute environment missing" in deploy
    assert "Required Video job queue missing" in deploy


def _load_ecr_cleanup_module():
    spec = importlib.util.spec_from_file_location("ecr_cleanup_contract", ECR_CLEANUP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_deployment_lock_module():
    spec = importlib.util.spec_from_file_location("deployment_lock_contract", DEPLOYMENT_LOCK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_release_freshness_module():
    spec = importlib.util.spec_from_file_location(
        "release_freshness_contract",
        RELEASE_FRESHNESS,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_lock_uses_conditional_put_renew_and_owner_release(monkeypatch) -> None:
    lock = _load_deployment_lock_module()
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(lock.subprocess, "run", fake_run)
    monkeypatch.setattr(lock.time, "time", lambda: 1_000)
    lock.acquire(lock.DEFAULT_TABLE, "owner-1", 600)
    lock.renew(lock.DEFAULT_TABLE, "owner-1", 600)
    lock.release(lock.DEFAULT_TABLE, "owner-1")
    lock.seal_legacy(lock.DEFAULT_TABLE, "cutover")

    joined = [" ".join(command) for command in commands]
    assert "attribute_not_exists(videoId) OR #ttl < :now" in joined[0]
    assert "#owner = :owner AND #ttl >= :now" in joined[1]
    assert "#owner = :owner" in joined[2]
    assert lock.LOCK_KEY == "__deployment_control_v2__"
    assert lock.LEGACY_LOCK_KEY == "__deployment_control__"
    assert "begins_with(#owner, :retired)" in joined[3]
    assert "retired:cutover" in joined[3]
    assert all(lock.DEFAULT_TABLE in command for command in joined)
    assert all(lock.DEFAULT_REGION in command for command in joined)


def test_release_freshness_allows_forward_only_and_rejects_stale_or_divergent(
    tmp_path: Path,
) -> None:
    freshness = _load_release_freshness_module()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "contract@example.com")
    git("config", "user.name", "Contract Test")
    marker = repo / "marker.txt"
    marker.write_text("a", encoding="utf-8")
    git("add", "marker.txt")
    git("commit", "-m", "A")
    sha_a = git("rev-parse", "HEAD")
    marker.write_text("b", encoding="utf-8")
    git("commit", "-am", "B")
    sha_b = git("rev-parse", "HEAD")
    git("checkout", "-b", "divergent", sha_a)
    marker.write_text("c", encoding="utf-8")
    git("commit", "-am", "C")
    sha_c = git("rev-parse", "HEAD")

    assert freshness.classify_candidate(repo, sha_a, sha_a) == "same"
    assert freshness.classify_candidate(repo, sha_b, sha_a) == "forward"
    assert freshness.classify_candidate(repo, sha_a, sha_b) == "stale"
    assert freshness.classify_candidate(repo, sha_c, sha_b) == "divergent"
    git("checkout", "main")
    evidence = repo / "docs" / "reports" / "ci-build.latest.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("evidence", encoding="utf-8")
    git("add", "docs/reports/ci-build.latest.md")
    git("commit", "-m", "evidence")
    sha_evidence = git("rev-parse", "HEAD")
    assert (
        freshness.classify_candidate(repo, sha_b, sha_a, sha_evidence)
        == "forward"
    )
    git("checkout", "-b", "off-main", sha_b)
    marker.write_text("off-main", encoding="utf-8")
    git("commit", "-am", "off-main")
    sha_off_main = git("rev-parse", "HEAD")
    assert (
        freshness.classify_candidate(repo, sha_off_main, sha_a, sha_evidence)
        == "off-main"
    )


def test_workflow_checks_release_freshness_under_lock_and_always_releases() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    acquire_block = _job_block(workflow, "acquire-production-lock")
    freshness_block = _job_block(workflow, "verify-release-freshness")
    iam_block = _job_block(workflow, "verify-runtime-iam")
    prepare_block = _job_block(workflow, "prepare-build")
    release_block = _job_block(workflow, "release-production-lock")

    assert "'docs/reports/**'" in workflow
    assert "'docs/ssot/runtime-current.md'" in workflow
    assert "'scripts/codex/**'" in workflow
    assert "runtime_changes() { grep -vE '^scripts/codex/'" in workflow
    assert 'CHANGED="$(runtime_changes "$CHANGED_API")"' in workflow
    assert 'CHANGED="$(runtime_changes "$CHANGED_AI")"' in workflow
    for output in (
        "build_base",
        "build_api",
        "build_video",
        "build_messaging",
        "build_ai",
        "build_tools",
        "force_full",
    ):
        assert f"needs.detect-changes.outputs.{output} == 'true'" in acquire_block
    assert "deployment_lock.py seal-legacy" in acquire_block
    assert acquire_block.index("deployment_lock.py seal-legacy") < acquire_block.index(
        "deployment_lock.py acquire"
    )
    assert "needs: [acquire-production-lock]" in freshness_block
    assert "fetch-depth: 0" in freshness_block
    assert "scripts/v1/release_freshness.py" in freshness_block
    assert "git fetch --no-tags origin main" in freshness_block
    assert "release-manifest.latest.json" in freshness_block
    assert '--main-sha "$MAIN_SHA"' in freshness_block
    assert "needs: [acquire-production-lock, verify-release-freshness]" in iam_block
    assert "iam get-role-policy" in iam_block
    assert "iam put-role-policy" not in iam_block
    assert "runtime worker-scale IAM readback mismatch" in iam_block
    assert "policy_workers_sqs.json" in iam_block
    assert "academy-workers-sqs" in iam_block
    assert "runtime worker SQS IAM readback mismatch" in iam_block
    assert "policy_ec2_cloudwatch_logs.json" in iam_block
    assert "academy-ec2-cloudwatch-logs" in iam_block
    assert "runtime logging IAM readback mismatch" in iam_block
    assert "logs describe-log-groups" in iam_block
    assert "/academy/api /academy/ai-worker /academy/tools-worker" in iam_block
    assert "runtime log group readback mismatch" in iam_block
    assert '[ "$RETENTION" != "30" ]' in iam_block
    assert "verify-runtime-iam" in prepare_block
    assert "verify-runtime-iam" in release_block
    assert "verify-release-freshness" in release_block
    assert "if: always() && needs.acquire-production-lock.result == 'success'" in release_block


def test_runtime_worker_sqs_policy_grants_only_exact_dlq_readback() -> None:
    policy = json.loads(WORKERS_SQS_POLICY.read_text(encoding="utf-8"))
    by_sid = {statement["Sid"]: statement for statement in policy["Statement"]}

    assert set(by_sid) == {"SQSQueueAccess", "MessagingDlqReadback"}
    assert set(by_sid["SQSQueueAccess"]["Resource"]) == {
        "arn:aws:sqs:ap-northeast-2:809466760795:academy-v1-messaging-queue",
        "arn:aws:sqs:ap-northeast-2:809466760795:academy-v1-ai-queue",
        "arn:aws:sqs:ap-northeast-2:809466760795:academy-v1-tools-queue",
    }
    assert set(by_sid["SQSQueueAccess"]["Action"]) == {
        "sqs:SendMessage",
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
    }
    assert all(not arn.endswith("-dlq") for arn in by_sid["SQSQueueAccess"]["Resource"])
    assert by_sid["MessagingDlqReadback"] == {
        "Sid": "MessagingDlqReadback",
        "Effect": "Allow",
        "Action": ["sqs:GetQueueAttributes", "sqs:GetQueueUrl"],
        "Resource": (
            "arn:aws:sqs:ap-northeast-2:809466760795:"
            "academy-v1-messaging-queue-dlq"
        ),
    }
    iam_source = IAM_RESOURCE.read_text(encoding="utf-8-sig")
    assert 'Join-Path $TemplatesPath "policy_workers_sqs.json"' in iam_source
    assert '$inlineName = "academy-workers-sqs"' in iam_source
    assert '"put-role-policy"' in iam_source


def test_runtime_worker_sqs_iam_converger_is_locked_and_readback_only_beyond_policy() -> None:
    source = CONVERGE_WORKERS_SQS_IAM.read_text(encoding="utf-8-sig")

    assert "assert-production-source-freshness.ps1" in source
    assert "Acquire-DeployLock" in source
    assert "Assert-DeployLockAcquired" in source
    assert "Release-DeployLock" in source
    assert '"put-role-policy"' in source
    assert source.count('"put-role-policy"') == 1
    assert '"get-role-policy"' in source
    assert source.count("Get-WorkerSqsPolicyReadback") >= 4
    current_read = source.index("$current = Get-WorkerSqsPolicyReadback")
    mismatch_branch = source.index("if ($currentJson -ne $expectedJson)", current_read)
    pre_write_assert = source.index("Assert-DeployLockAcquired", mismatch_branch)
    policy_write = source.index('"put-role-policy"', pre_write_assert)
    post_write_assert = source.index("Assert-DeployLockAcquired", policy_write)
    readback = source.index("$readback = Get-WorkerSqsPolicyReadback", post_write_assert)
    assert current_read < pre_write_assert < policy_write < post_write_assert < readback
    assert "RUNTIME_WORKER_SQS_IAM_RECONCILED" in source
    assert "start-instance-refresh" not in source
    assert "update-auto-scaling-group" not in source
    assert "register-job-definition" not in source


def test_dev_alerts_cron_reconciles_failed_wrong_note_pdf_objects_safely() -> None:
    workflow = DEV_ALERTS_WORKFLOW.read_text(encoding="utf-8")

    assert "python manage.py help cleanup_failed_wrong_note_pdfs" in workflow
    assert "python manage.py cleanup_failed_wrong_note_pdfs --silent --limit 50" in workflow
    assert 'if [ "$DRY_RUN" = "true" ]; then' in workflow
    assert "TEST_SMS" not in workflow
    assert 'CLEANUP_COMMAND=""' in workflow
    assert "${CLEANUP_COMMAND}${PUSH_COMMAND}python manage.py check_dev_alerts" in workflow


def test_successful_release_manifest_protects_exactly_all_six_images(
    monkeypatch, tmp_path: Path
) -> None:
    cleanup = _load_ecr_cleanup_module()
    manifest = tmp_path / "release-manifest.latest.json"
    images = {
        repo: {"digest": "sha256:" + format(index, "064x")}
        for index, repo in enumerate(cleanup.REPOS, start=1)
    }
    manifest.write_text(
        json.dumps(
            {"schemaVersion": 1, "status": "successful", "complete": True, "images": images}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cleanup, "RELEASE_MANIFEST", manifest)
    assert cleanup.collect_release_manifest_digests() == {
        repo: entry["digest"] for repo, entry in images.items()
    }


def test_ecr_partial_delete_failure_is_counted(monkeypatch) -> None:
    cleanup = _load_ecr_cleanup_module()
    monkeypatch.setattr(
        cleanup,
        "aws_ecr",
        lambda *_args: '{"imageIds":[],"failures":[{"failureCode":"ImageReferencedByManifestList"}]}',
    )
    deleted, failed = cleanup.batch_delete(
        "academy-api", ["sha256:" + "a" * 64], "manifest"
    )
    assert deleted == 0
    assert failed == 1


def test_ecr_cleanup_never_deletes_old_active_runtime_digest(monkeypatch) -> None:
    cleanup = _load_ecr_cleanup_module()
    images = [
        {
            "imageDigest": f"sha256:{index:064x}",
            "imageTags": [f"sha-{index:08x}"],
            "imagePushedAt": f"2026-07-{index + 1:02d}T00:00:00Z",
            "imageManifestMediaType": cleanup.TYPE_MANIFEST,
        }
        for index in range(12)
    ]
    runtime_digest = images[0]["imageDigest"]
    protected, old_tagged = cleanup.identify_protected_set(
        images,
        keep=10,
        repo="academy-api",
        runtime_digests={runtime_digest},
    )
    indexes, manifests = cleanup.classify_deletable(images, protected, old_tagged)

    assert runtime_digest in protected
    assert runtime_digest not in indexes
    assert runtime_digest not in manifests


def test_ecr_cleanup_retries_bootstrapping_runtime_until_digest_ready(
    monkeypatch,
) -> None:
    cleanup = _load_ecr_cleanup_module()
    digest = "sha256:" + "7" * 64
    registry = f"{cleanup.ACCOUNT_ID}.dkr.ecr.{cleanup.REGION}.amazonaws.com"
    invocations = 0
    sleeps: list[int] = []
    membership_phases: list[str] = []

    def fake_service(service: str, *args):
        nonlocal invocations
        assert service == "ssm"
        if args[0] == "send-command":
            return {"Command": {"CommandId": f"cmd-{invocations + 1}"}}
        if args[0] == "get-command-invocation":
            invocations += 1
            if invocations == 1:
                return {
                    "Status": "Failed",
                    "StandardErrorContent": "docker: command not found",
                }
            return {
                "Status": "Success",
                "StandardOutputContent": f"{registry}/academy-ai-worker-cpu@{digest}\n",
            }
        raise AssertionError((service, args))

    monkeypatch.setattr(cleanup, "aws_service", fake_service)
    monkeypatch.setattr(cleanup, "wait_for_ssm_command", lambda *_: None)
    monkeypatch.setattr(
        cleanup,
        "assert_runtime_asg_membership",
        lambda _asg, _instances, *, phase: membership_phases.append(phase),
    )
    monkeypatch.setattr(cleanup, "RUNTIME_DIGEST_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(cleanup.time, "sleep", sleeps.append)

    actual = cleanup.collect_actual_instance_runtime_digest(
        "academy-v1-ai-asg",
        frozenset({"i-bootstrapping"}),
        "i-bootstrapping",
        "academy-ai-worker-cpu",
        "academy-ai-worker-cpu",
    )

    assert actual == digest
    assert invocations == 2
    assert sleeps == [cleanup.RUNTIME_DIGEST_RETRY_SECONDS]
    assert membership_phases == [
        "attempt-1-before-ssm",
        "attempt-1-after-ssm",
        "attempt-2-before-ssm",
        "attempt-2-after-ssm",
    ]


def test_ecr_cleanup_still_fails_closed_after_runtime_retry_budget(
    monkeypatch,
) -> None:
    cleanup = _load_ecr_cleanup_module()
    invocations = 0
    sleeps: list[int] = []
    membership_phases: list[str] = []

    def fake_service(service: str, *args):
        nonlocal invocations
        assert service == "ssm"
        if args[0] == "send-command":
            return {"Command": {"CommandId": f"cmd-{invocations + 1}"}}
        if args[0] == "get-command-invocation":
            invocations += 1
            return {
                "Status": "Failed",
                "StandardErrorContent": "docker: command not found",
            }
        raise AssertionError((service, args))

    monkeypatch.setattr(cleanup, "aws_service", fake_service)
    monkeypatch.setattr(cleanup, "wait_for_ssm_command", lambda *_: None)
    monkeypatch.setattr(
        cleanup,
        "assert_runtime_asg_membership",
        lambda _asg, _instances, *, phase: membership_phases.append(phase),
    )
    monkeypatch.setattr(cleanup, "RUNTIME_DIGEST_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(cleanup.time, "sleep", sleeps.append)

    with pytest.raises(SystemExit) as exc:
        cleanup.collect_actual_instance_runtime_digest(
            "academy-v1-ai-asg",
            frozenset({"i-never-ready"}),
            "i-never-ready",
            "academy-ai-worker-cpu",
            "academy-ai-worker-cpu",
        )

    assert exc.value.code == 2
    assert invocations == 2
    assert sleeps == [cleanup.RUNTIME_DIGEST_RETRY_SECONDS]
    assert membership_phases == [
        "attempt-1-before-ssm",
        "attempt-1-after-ssm",
        "attempt-2-before-ssm",
        "attempt-2-after-ssm",
    ]


def test_ecr_cleanup_fails_closed_when_runtime_membership_drifts(
    monkeypatch,
    capsys,
) -> None:
    cleanup = _load_ecr_cleanup_module()

    def fake_service(service: str, *args):
        assert service == "autoscaling"
        assert args == (
            "describe-auto-scaling-groups",
            "--auto-scaling-group-names",
            "academy-v1-api-asg",
        )
        return {
            "AutoScalingGroups": [
                {
                    "AutoScalingGroupName": "academy-v1-api-asg",
                    "DesiredCapacity": 1,
                    "Instances": [
                        {
                            "InstanceId": "i-replacement",
                            "LifecycleState": "InService",
                            "HealthStatus": "Healthy",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(cleanup, "aws_service", fake_service)

    with pytest.raises(SystemExit) as exc:
        cleanup.assert_runtime_asg_membership(
            "academy-v1-api-asg",
            frozenset({"i-original"}),
            phase="attempt-1-after-ssm",
        )

    assert exc.value.code == 2
    assert "runtime ASG membership drifted" in capsys.readouterr().err


def test_ecr_cleanup_inventories_asg_current_and_running_lt_and_batch(monkeypatch) -> None:
    cleanup = _load_ecr_cleanup_module()
    api_digest = "sha256:" + "a" * 64
    old_api_digest = "sha256:" + "b" * 64
    actual_api_digest = "sha256:" + "9" * 64
    video_digest = "sha256:" + "c" * 64
    registry = f"{cleanup.ACCOUNT_ID}.dkr.ecr.{cleanup.REGION}.amazonaws.com"
    asg_repos = cleanup.ASG_REPOSITORIES
    current_by_repo = {
        "academy-api": api_digest,
        "academy-messaging-worker": "sha256:" + "d" * 64,
        "academy-ai-worker-cpu": "sha256:" + "e" * 64,
        "academy-tools-worker": "sha256:" + "f" * 64,
    }

    def fake_service(service: str, *args):
        if service == "autoscaling":
            groups = [
                    {
                        "AutoScalingGroupName": asg_name,
                        "LaunchTemplate": {
                            "LaunchTemplateId": f"lt-{repo}",
                            "Version": "$Latest",
                        },
                        "Instances": (
                            [
                                {
                                    "InstanceId": "i-api-runtime",
                                    "LifecycleState": "InService",
                                    "HealthStatus": "Healthy",
                                    "LaunchTemplate": {
                                        "LaunchTemplateId": f"lt-{repo}",
                                        "Version": "4",
                                    }
                                }
                            ]
                            if repo == "academy-api"
                            else []
                        ),
                        "DesiredCapacity": 1 if repo == "academy-api" else 0,
                    }
                    for asg_name, repo in asg_repos.items()
                ]
            if "--auto-scaling-group-names" in args:
                expected_name = args[args.index("--auto-scaling-group-names") + 1]
                groups = [
                    group
                    for group in groups
                    if group["AutoScalingGroupName"] == expected_name
                ]
            return {"AutoScalingGroups": groups}
        if service == "ec2":
            version = args[args.index("--versions") + 1]
            template_id = args[args.index("--launch-template-id") + 1]
            repo = template_id.removeprefix("lt-")
            digest = (
                old_api_digest
                if repo == "academy-api" and version == "4"
                else current_by_repo[repo]
            )
            image = f"{registry}/{repo}@{digest}"
            # Real launch-template user data repeats the same immutable URI in
            # pull, run, and error logging. Repetition must not be interpreted
            # as multiple distinct runtime image references.
            userdata = base64.b64encode(
                f"docker pull {image}\ndocker run {image}\necho {image}".encode()
            ).decode()
            return {
                "LaunchTemplateVersions": [
                    {"LaunchTemplateData": {"UserData": userdata}}
                ]
            }
        if service == "batch":
            return {
                "jobDefinitions": [
                    {
                        "jobDefinitionName": name,
                        "revision": 1,
                        "containerProperties": {
                            "image": f"{registry}/academy-video-worker@{video_digest}"
                        }
                    }
                    for name in cleanup.REQUIRED_VIDEO_JOB_DEFINITIONS
                ]
            }
        if service == "ssm" and args[0] == "send-command":
            return {"Command": {"CommandId": "cmd-runtime"}}
        if service == "ssm" and args[0] == "get-command-invocation":
            return {
                "Status": "Success",
                "StandardOutputContent": (
                    f"{registry}/academy-api@{actual_api_digest}\n"
                ),
            }
        raise AssertionError((service, args))

    monkeypatch.setattr(cleanup, "aws_service", fake_service)
    monkeypatch.setattr(cleanup, "wait_for_ssm_command", lambda *_: None)
    monkeypatch.setattr(
        cleanup,
        "collect_release_manifest_digests",
        lambda: {repo: current_by_repo.get(repo, video_digest) for repo in cleanup.REPOS},
    )
    protected = cleanup.collect_runtime_protected_digests()

    assert protected["academy-api"] == {
        api_digest,
        old_api_digest,
        actual_api_digest,
    }
    assert protected["academy-video-worker"] == {video_digest}
