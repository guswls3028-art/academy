$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workflowRoot = Join-Path $repoRoot ".github\workflows"
$failures = @()

foreach ($file in Get-ChildItem -LiteralPath $workflowRoot -File) {
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $file.FullName) {
        $lineNumber++
        if ($line -match 'uses:\s+([^\s#]+)') {
            $action = $matches[1]
            if ($action -notmatch '^\./' -and $action -notmatch '@[0-9a-f]{40}$') {
                $failures += "$($file.Name):$lineNumber action is not commit-pinned: $action"
            }
        }
    }
}

$productionWorkflow = Get-Content -LiteralPath (
    Join-Path $workflowRoot "v1-build-and-push-latest.yml"
) -Raw
$qualityWorkflow = Get-Content -LiteralPath (
    Join-Path $workflowRoot "quality-gate.yml"
) -Raw
$governanceScript = Get-Content -LiteralPath (
    Join-Path $PSScriptRoot "converge-github-governance.ps1"
) -Raw
$deployIam = Get-Content -LiteralPath (
    Join-Path $PSScriptRoot "resources\iam.ps1"
) -Raw
$developmentOidc = Get-Content -LiteralPath (
    Join-Path $PSScriptRoot "converge-api-development-oidc.ps1"
) -Raw

$requiredProductionMarkers = @(
    "environment: production",
    "prepare-build:",
    "tests/test_critical_academy_journey.py",
    "build-runtime-images:",
    "needs: [detect-changes, prepare-build, build-runtime-images]",
    "fail-fast: false",
    "Gate all candidate images on completed ECR critical scan",
    "scripts/v1/ecr-critical-scan-gate.py",
    "docs/ssot/ecr-critical-risk-acceptance.json",
    ".imageScanningConfiguration.scanOnPush == true",
    "needs.build-and-push.result == 'success'",
    "contents: read",
    'ssh-key: ${{ secrets.ACADEMY_RELEASE_DEPLOY_KEY }}'
)
foreach ($marker in $requiredProductionMarkers) {
    if (-not $productionWorkflow.Contains($marker)) {
        $failures += "Production workflow is missing governance marker: $marker"
    }
}
foreach ($marker in @(
    "Backend static and migration contract",
    "Backend Django smoke and deployment contracts",
    "scripts/lint/check_safe_method_writes.py",
    "--allow-contract-review",
    "permissions:",
    "contents: read"
)) {
    if (-not $qualityWorkflow.Contains($marker)) {
        $failures += "Backend quality workflow is missing marker: $marker"
    }
}
if (-not $productionWorkflow.Contains("scripts/lint/check_safe_method_writes.py")) {
    $failures += "Production workflow is missing the safe-method write boundary."
}
if (-not $productionWorkflow.Contains('ARGS+=(--allow-contract)')) {
    $failures += "Production workflow is missing the explicit contract execution marker."
}
foreach ($marker in @(
    "repo:guswls3028-art/academy-backend:ref:refs/heads/main",
    "repo:guswls3028-art/academy-backend:environment:production",
    "update-assume-role-policy",
    "GitHub Actions OIDC trust readback mismatch",
    "ecr:StartImageScan",
    'Sid="ProductUsagePilotEnvPublish"',
    'Action="ssm:PutParameter"',
    'Sid="ProductUsagePilotLogQuery"',
    '"logs:StartQuery","logs:GetQueryResults"',
    'log-group:/academy/api:*'
)) {
    if (-not $deployIam.Contains($marker)) {
        $failures += "Deploy IAM is missing OIDC trust marker: $marker"
    }
}
foreach ($marker in @(
    "repo:guswls3028-art/academy-backend:ref:refs/heads/main",
    "repo:guswls3028-art/academy-backend:environment:production",
    "main-ref and production-environment only"
)) {
    if (-not $developmentOidc.Contains($marker)) {
        $failures += "Development OIDC audit is missing trust marker: $marker"
    }
}
foreach ($marker in @(
    "sha_pinning_required = `$true",
    "academy-main-governance",
    "production-rollback",
    "Get-RequiredApprovingReviewCount",
    "required_approving_review_count = `$RequiredApprovingReviewCount",
    "automated-security-fixes",
    "vulnerability-alerts",
    "Ensure-ReleaseDeployKey",
    'actor_type = "DeployKey"',
    'actor_id = $null',
    "required_status_checks",
    "protection_rules",
    "rollbackReviewRules",
    "allowed_actions"
)) {
    if (-not $governanceScript.Contains($marker)) {
        $failures += "GitHub governance script is missing marker: $marker"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot ".github\dependabot.yml"))) {
    $failures += "Backend Dependabot configuration is missing."
}
foreach ($relativePath in @(
    "scripts\v1\ecr-critical-scan-gate.py",
    "docs\ssot\ecr-critical-risk-acceptance.json",
    "docs\operations\container-image-security.md"
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $relativePath))) {
        $failures += "Container image security contract is missing: $relativePath"
    }
}

if ($failures.Count -gt 0) {
    throw ($failures -join [Environment]::NewLine)
}
Write-Host "WORKFLOW_GOVERNANCE_CONTRACT_PASS" -ForegroundColor Green
