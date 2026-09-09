# Usage: pwsh scripts/v1/deploy.ps1 [-Env prod] [-Plan] [-Bootstrap] ...
# 원테이크: Bootstrap 기본 ON. SSM/SQS/RDS engineVersion/ECR 자동 준비 후 Ensure. 사용자 사전 작업 없음.
# 인프라 Ensure 후 API env(/academy/api/env) 및 Workers env(/academy/workers/env)를 SSOT와 Redis discovery 기준으로
# 자동 동기화(Invoke-SyncEnvFromSSOT)하여, 여러 번 실행해도 동일한 인프라·env 상태가 되도록 idempotent 함.
# 배포 원칙: 빌드 서버는 사용하지 않는다(0대). 이미지 빌드·ECR 푸시는 GitHub Actions(OIDC)만 사용한다.
# deploy.ps1는 이미 ECR에 올라간 이미지를 pull하여 배포/refresh만 수행한다. (즉 -SkipBuild가 기본 흐름)
# 전체 실행 시간: API health 대기(최대 300s) + Netprobe( cold start 시 최대 600s) + Evidence(수십 초) 등으로 20~25분 넘을 수 있음. CI/터미널 타임아웃은 30분 이상 권장 (docs/reports/DEPLOY-TIMING-CHECKLIST.md 참고).
# Cursor 등 실행 시: AWS 프로필은 반드시 default. -AwsProfile default (문서·Cursor 룰 07_aws_profile_default.mdc).
# ==============================================================================
[CmdletBinding()]
param(
    [ValidateSet("prod","staging","dev")]
    [string]$Env = "prod",
    [switch]$Plan = $false,
    [switch]$Bootstrap = $true,
    [switch]$StrictValidation = $true,
    [switch]$SkipBuild = $true,
    [switch]$SkipSqs = $false,
    [switch]$SkipRds = $false,
    [switch]$SkipRedis = $false,
    [switch]$PruneLegacy = $false,
    [switch]$PurgeAndRecreate = $false,
    [switch]$DryRun = $false,
    [switch]$ForceRecreateAll = $false,
    [switch]$SkipNetprobe = $false,
    [switch]$MinimalDeploy = $false,
    [switch]$SkipApiSSMWait = $false,
    [switch]$Ci = $false,
    [switch]$RelaxedValidation = $false,
    [string]$EcrRepoUri = "",
    [string]$AwsProfile = "default"
)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch {}
$ScriptRoot = $PSScriptRoot

# 인증: AWS·Cloudflare(클플) 인증은 Cursor 룰(.cursor/rules)에 의거 .env 직접 열람 후 키를 환경변수로 넣어 배포·검증·인증을 진행한다. 스크립트는 .env를 로드하지 않음.
# Cursor(에이전트)가 루트 .env를 열람해 AWS/Cloudflare 등을 환경변수로 설정한 뒤 본 스크립트를 실행한다.
. (Join-Path $ScriptRoot "core\env.ps1")
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot "..\..")).Path

# -AwsProfile 기본값 default (문서·룰: 반드시 default 사용, 프로필 묻지 말 것)
# CI mode (-Ci): OIDC uses env vars (AWS_ACCESS_KEY_ID/AWS_SESSION_TOKEN), not named profiles.
if ($Ci) {
    Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue
    if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = "ap-northeast-2" }
    Write-Host "CI mode: using env-var credentials (AWS_PROFILE removed)" -ForegroundColor Cyan
} elseif ($AwsProfile -and $AwsProfile.Trim() -ne "") {
    $env:AWS_PROFILE = $AwsProfile.Trim()
    if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = "ap-northeast-2" }
    Write-Host "Using AWS_PROFILE: $($env:AWS_PROFILE) (region: $($env:AWS_DEFAULT_REGION))" -ForegroundColor Cyan
}

$script:PlanMode = $Plan
$script:AllowRebuild = -not $Plan -and (-not $ForceRecreateAll -or $true)
$script:ChangesMade = $false
$script:DeployLockAcquired = $false
$script:SqsScalingNotEnforced = $false
switch ($true) { { $EcrRepoUri } { $script:EcrRepoUri = $EcrRepoUri } default { $script:EcrRepoUri = "" } }

# Core (order: ssot first so Load-SSOT sets vars)
. (Join-Path $ScriptRoot "core\ssot.ps1")
. (Join-Path $ScriptRoot "core\logging.ps1")
. (Join-Path $ScriptRoot "core\aws.ps1")
. (Join-Path $ScriptRoot "core\wait.ps1")
. (Join-Path $ScriptRoot "core\diff.ps1")
. (Join-Path $ScriptRoot "core\evidence.ps1")
. (Join-Path $ScriptRoot "core\prune.ps1")
. (Join-Path $ScriptRoot "core\guard.ps1")
. (Join-Path $ScriptRoot "core\preflight.ps1")
. (Join-Path $ScriptRoot "core\reports.ps1")
. (Join-Path $ScriptRoot "core\bootstrap.ps1")
. (Join-Path $ScriptRoot "core\sync_env.ps1")

# Resources
. (Join-Path $ScriptRoot "resources\network.ps1")
. (Join-Path $ScriptRoot "resources\iam.ps1")
. (Join-Path $ScriptRoot "resources\ssm.ps1")
. (Join-Path $ScriptRoot "resources\ecr.ps1")
. (Join-Path $ScriptRoot "resources\alb.ps1")
. (Join-Path $ScriptRoot "resources\api.ps1")
. (Join-Path $ScriptRoot "resources\worker_userdata.ps1")
. (Join-Path $ScriptRoot "resources\rds.ps1")
. (Join-Path $ScriptRoot "resources\redis.ps1")
. (Join-Path $ScriptRoot "resources\asg_ai.ps1")
. (Join-Path $ScriptRoot "resources\asg_messaging.ps1")
. (Join-Path $ScriptRoot "resources\asg_tools.ps1")
. (Join-Path $ScriptRoot "resources\batch.ps1")
. (Join-Path $ScriptRoot "resources\jobdef.ps1")
. (Join-Path $ScriptRoot "resources\eventbridge.ps1")
. (Join-Path $ScriptRoot "resources\dynamodb.ps1")
. (Join-Path $ScriptRoot "resources\netprobe.ps1")
. (Join-Path $ScriptRoot "resources\cloudwatch.ps1")
. (Join-Path $ScriptRoot "resources\cost_tags.ps1")

$null = Load-SSOT -Env $Env
$script:RelaxedValidation = $RelaxedValidation
$script:MinimalDeploy = $MinimalDeploy
if ($MinimalDeploy) {
    $script:SkipEcrVpcEndpoints = $true
    $script:OpsCEName = $null
    $script:OpsQueueName = $null
}
$script:SkipApiSSMWait = if ($MinimalDeploy -and -not $PSBoundParameters.ContainsKey("SkipApiSSMWait")) { $true } else { $SkipApiSSMWait }
if ($EcrRepoUri) { $script:EcrRepoUri = $EcrRepoUri } else { $script:EcrRepoUri = "" }

# Bootstrap는 Ensure-Network 이후 try 블록 내에서 실행 (빌드 서버 Ensure 시 서브넷 필요).

Write-Host "`n=== DEPLOY v1 ($Env) ===" -ForegroundColor Cyan
if ($Plan) { Write-Host "MODE: Plan (no AWS changes)" -ForegroundColor Yellow }
if ($Bootstrap) { Write-Host "MODE: Bootstrap ON (one-take)" -ForegroundColor Cyan }
if ($StrictValidation) { Write-Host "MODE: StrictValidation ON" -ForegroundColor Cyan }
if ($RelaxedValidation) { Write-Host "MODE: RelaxedValidation (SQS scaling failure non-fatal)" -ForegroundColor Yellow }
if ($PruneLegacy -and -not $Plan) { Write-Host "MODE: PruneLegacy" -ForegroundColor Yellow }
if ($PurgeAndRecreate) { Write-Host "MODE: PurgeAndRecreate" -ForegroundColor Yellow }
if ($MinimalDeploy) { Write-Host "MODE: MinimalDeploy (Video Long, Ops, EventBridge skipped)" -ForegroundColor Cyan }
if ($DryRun) { Write-Host "MODE: DryRun (no changes)" -ForegroundColor Yellow }

function Invoke-ManualApiDevelopmentGate {
    param(
        [Parameter(Mandatory = $true)][string]$ApiImageUri,
        [Parameter(Mandatory = $true)][string]$ToolsImageUri,
        [Parameter(Mandatory = $true)][string]$AiImageUri,
        [Parameter(Mandatory = $true)][string]$MessagingImageUri
    )
    if ($ApiImageUri -notmatch '@sha256:(?<digest>[0-9a-f]{64})$') {
        throw "API development candidate image is not digest-pinned."
    }
    $apiDigest = $matches["digest"]
    Assert-ImmutableEcrImageUri -ImageUri $ToolsImageUri
    Assert-ImmutableEcrImageUri -ImageUri $AiImageUri
    Assert-ImmutableEcrImageUri -ImageUri $MessagingImageUri
    $releaseId = "manual-sha256-$apiDigest"
    $outputPath = [IO.Path]::GetTempFileName()
    try {
        & (Join-Path $ScriptRoot "publish-api-development-env.ps1") `
            -ReleaseId $releaseId `
            -GithubOutputPath $outputPath `
            -AwsProfile $AwsProfile
        $outputs = @{}
        foreach ($line in Get-Content -LiteralPath $outputPath) {
            $parts = [string]$line -split "=", 2
            if ($parts.Count -eq 2 -and $parts[0]) {
                $outputs[$parts[0]] = $parts[1]
            }
        }
        foreach ($required in @(
            "parameter_version",
            "workers_parameter_version",
            "release_id",
            "production_database_name"
        )) {
            if (-not $outputs[$required]) {
                throw "Development env publisher returned no '$required' output."
            }
        }
        & (Join-Path $ScriptRoot "deploy-api-development.ps1") `
            -ApiImageUri $ApiImageUri `
            -ToolsImageUri $ToolsImageUri `
            -AiImageUri $AiImageUri `
            -MessagingImageUri $MessagingImageUri `
            -ExpectedEnvVersion ([int]$outputs["parameter_version"]) `
            -ExpectedWorkersEnvVersion ([int]$outputs["workers_parameter_version"]) `
            -ExpectedReleaseId $outputs["release_id"] `
            -ExpectedProductionDatabaseName $outputs["production_database_name"] `
            -AwsProfile $AwsProfile
    } finally {
        Remove-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
    }
}

try {
    Assert-NoLegacyScripts -Ci:$Ci
    if ($Env -eq "prod" -and -not $Plan -and -not $DryRun) {
        & (Join-Path $ScriptRoot "assert-production-source-freshness.ps1") `
            -RepoRoot $RepoRoot
    }
    # The lock table is the only production mutation allowed before acquiring
    # the lock itself. Its create is conditional/idempotent and is required to
    # make a fresh bootstrap reachable; all other mutations stay behind it.
    Ensure-DynamoLockTable
    Acquire-DeployLock -Reg $script:Region
    Invoke-PreflightCheck
    $driftRows = Get-StructuralDrift
    Show-DriftTable -Rows $driftRows
    Save-DriftReport -Rows $driftRows

    if ($PurgeAndRecreate -and $DryRun) {
        $purgePlan = Get-PurgePlan
        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.AppendLine("# Purge plan (DryRun)")
        [void]$sb.AppendLine("**Generated:** $(Get-Date -Format 'o')")
        [void]$sb.AppendLine("")
        foreach ($key in $purgePlan.Keys) {
            [void]$sb.AppendLine("## $key")
            foreach ($id in $purgePlan[$key]) { [void]$sb.AppendLine("- $id") }
            [void]$sb.AppendLine("")
        }
        Save-EvidenceReport -MarkdownContent $sb.ToString()
        Write-Host "`n=== PURGE DRY RUN COMPLETE (no changes) ===`n" -ForegroundColor Green
        Release-DeployLock -Reg $script:Region
        exit 0
    }

    if ($PruneLegacy) {
        $all = Get-AllAwsResourcesForPrune
        $candidates = Get-DeleteCandidates -All $all
        $count = Show-DeleteCandidateTable -Candidates $candidates
        if (-not $Plan -and $count -gt 0) {
            Write-Host "PruneLegacy: running deletes..." -ForegroundColor Yellow
            Invoke-PruneLegacyDeletes -Candidates $candidates
        }
        if ($Plan) {
            Write-Host "`n=== PLAN COMPLETE (no changes) ===`n" -ForegroundColor Green
            exit 0
        }
    }

    if ($PurgeAndRecreate -and -not $Plan) {
        Invoke-PurgeAndRecreate -IncludePruneLegacy:$false
    }

    if ($Plan) {
        $ev = Show-Evidence -NetprobeJobId "" -NetprobeStatus "skipped"
        if ($ev) { Save-EvidenceReport -MarkdownContent (Convert-EvidenceToMarkdown -Ev $ev) }
        Write-Host "`n=== PLAN COMPLETE ===`n" -ForegroundColor Green
        Release-DeployLock -Reg $script:Region
        exit 0
    }

    $script:BatchIam = Ensure-BatchIAM
    Ensure-EC2InstanceProfileSSM
    if ($Env -eq "prod") { Ensure-ApiPreprodCanaryIAM }
    Ensure-Network
    Ensure-NetworkVpc
    Confirm-SubnetsMatchSSOT

    # Bootstrap (원테이크): Ensure-Network 이후 실행하여 빌드 서버 Ensure 시 서브넷 사용 가능.
    if ($Bootstrap -and -not $Plan) {
        Invoke-Bootstrap -Bootstrap:$true -SkipSqs:$SkipSqs -SkipRds:$SkipRds -SkipRedis:$SkipRedis -SkipBuild:$SkipBuild
    }
    if ($script:EcrRepoUriResolved) { $script:EcrRepoUri = $script:EcrRepoUriResolved }

    # Strict Gate (Bootstrap 이후 평가)
    $strictCheck = -not $Plan -and $StrictValidation -and -not $script:RelaxedValidation
    switch ($true) {
        { $strictCheck -and $script:EcrImmutableTagRequired -and (-not $script:EcrRepoUri -or $script:EcrRepoUri.Trim() -eq "") } {
            Write-Fail "Strict: EcrRepoUri not set. Bootstrap could not resolve image. Pass -EcrRepoUri or ensure build/ECR available."
            throw "Strict: EcrRepoUri required."
        }
        { $script:EcrRepoUri -and ($script:EcrRepoUri -match ':latest\s*$') -and -not $script:EcrUseLatestTag } {
            Write-Fail ":latest tag is prohibited. Use an immutable tag or set ecr.useLatestTag in SSOT."
            throw "EcrRepoUri must not contain :latest when useLatestTag is false."
        }
        { $strictCheck -and $script:EcrImmutableTagRequired -and $script:EcrRepoUri -notmatch '(:sha-(?:[0-9a-f]{8,40}|[0-9a-f]{40}-run-[0-9]+-[0-9]+)|@sha256:[0-9a-f]{64})$' } {
            Write-Fail "Strict: EcrRepoUri must use a CI sha-* tag or an ECR digest."
            throw "EcrRepoUri is not immutable."
        }
        { $strictCheck -and [string]::IsNullOrWhiteSpace($script:MessagingSqsQueueUrl) -and [string]::IsNullOrWhiteSpace($script:MessagingSqsQueueName) } {
            Write-Fail "Strict: messagingWorker SQS not set. Bootstrap could not create/find queue."
            throw "Strict: set messagingWorker.sqsQueueUrl or sqsQueueName, or allow Bootstrap to create."
        }
        { $strictCheck -and [string]::IsNullOrWhiteSpace($script:AiSqsQueueUrl) -and [string]::IsNullOrWhiteSpace($script:AiSqsQueueName) } {
            Write-Fail "Strict: aiWorker SQS not set. Bootstrap could not create/find queue."
            throw "Strict: set aiWorker.sqsQueueUrl or sqsQueueName, or allow Bootstrap to create."
        }
        { $strictCheck -and $script:RdsDbIdentifier -and [string]::IsNullOrWhiteSpace($script:RdsMasterPasswordSsmParam) } {
            Write-Fail "Strict: rds.masterPasswordSsmParam not set. Bootstrap could not create SSM password."
            throw "Strict: set rds.masterPasswordSsmParam in params.yaml."
        }
        { $strictCheck -and $script:RdsDbIdentifier -and $script:RdsMasterPasswordSsmParam } {
            $rdsParam = Invoke-AwsJson @("ssm", "get-parameter", "--name", $script:RdsMasterPasswordSsmParam, "--with-decryption", "--region", $script:Region, "--output", "json")
            $badParam = -not $rdsParam -or -not $rdsParam.Parameter -or -not $rdsParam.Parameter.Value
            switch ($badParam) {
                $true {
                    Write-Fail "Strict: RDS master password SSM parameter not found: $($script:RdsMasterPasswordSsmParam)"
                    throw "Strict: create SSM SecureString or run with Bootstrap."
                }
            }
        }
    }

    Confirm-RDSState
    Confirm-RedisState
    Confirm-SSMEnv
    Ensure-ECRRepos
    Ensure-DynamoLockTable
    Ensure-DynamoUploadCheckpointTable

    # Prepare candidate values in memory. Live API/worker parameters remain
    # unchanged until the isolated server passes.
    Invoke-SyncEnvFromSSOT -PrepareOnly

    # Production mutation is gated by one isolated instance using a dedicated
    # SSM parameter, instance profile, and non-production database.
    if ($Env -eq "prod") {
        $apiCanaryImage = Get-LatestApiImageUri
        if (-not $apiCanaryImage) { throw "API pre-production canary could not resolve an immutable API image." }
        if ($apiCanaryImage -notmatch '@sha256:(?<digest>[0-9a-f]{64})$') {
            throw "API pre-production canary image is not digest-pinned."
        }
        $apiCanaryReleaseId = "manual-sha256-$($matches['digest'])"
        $toolsCanaryImage = Get-LatestWorkerImageUri -RepoName $script:EcrToolsRepo
        if (-not $toolsCanaryImage) {
            throw "Tools development candidate could not resolve an immutable image."
        }
        $aiCanaryImage = Get-LatestWorkerImageUri -RepoName $script:EcrAiRepo
        if (-not $aiCanaryImage) {
            throw "AI development candidate could not resolve an immutable image."
        }
        $messagingCanaryImage = Get-LatestWorkerImageUri -RepoName $script:EcrMessagingRepo
        if (-not $messagingCanaryImage) {
            throw "Messaging development candidate could not resolve an immutable image."
        }
        Invoke-ManualApiDevelopmentGate `
            -ApiImageUri $apiCanaryImage `
            -ToolsImageUri $toolsCanaryImage `
            -AiImageUri $aiCanaryImage `
            -MessagingImageUri $messagingCanaryImage
        $apiCanaryEnv = Publish-ApiPreprodEnvCandidate -ReleaseId $apiCanaryReleaseId
        & (Join-Path $ScriptRoot "run-api-preprod-canary.ps1") `
            -ImageUri $apiCanaryImage `
            -SsmApiEnvParameter $apiCanaryEnv.ParameterName `
            -ExpectedEnvVersion $apiCanaryEnv.ParameterVersion `
            -ExpectedReleaseId $apiCanaryEnv.ReleaseId `
            -ExpectedDatabaseName $apiCanaryEnv.DatabaseName `
            -ExpectedDatabaseUser $apiCanaryEnv.DatabaseUser `
            -ExpectedProductionDatabaseName $apiCanaryEnv.ProductionDatabaseName `
            -AwsProfile $AwsProfile
    }

    # Only a passing canary may promote live runtime parameters.
    Publish-RuntimeEnvCandidates

    # Only promoted, canary-verified values may open any production runtime
    # mutation path. This includes API, workers, Batch, EventBridge, and ALB.
    Ensure-RuntimeLogRetention
    Ensure-ASGAi
    Ensure-ASGMessaging
    Ensure-ASGTools
    # long path 폐기 (2026-05-10): VideoLongCE/Queue/JobDef ensure 제거.
    Ensure-VideoCE
    if (-not $script:MinimalDeploy) { Ensure-OpsCE }
    Ensure-VideoQueue
    if (-not $script:MinimalDeploy) { Ensure-OpsQueue }
    Ensure-VideoJobDef
    if (-not $script:MinimalDeploy) {
        Ensure-OpsJobDefReconcile
        Ensure-OpsJobDefScanStuck
        Ensure-OpsJobDefNetprobe
        Ensure-OpsJobDefEnqueueUploaded
        # 2026-05-11 IaC 보강: cron 7종 SSOT 중 Batch 패턴 3종 jobdef 추가.
        # cleanup-orphan 은 SSM RunShellScript 운영이라 Batch jobdef 없음 (eventbridge.ps1 도 그 rule 건드리지 않음).
        Ensure-OpsJobDefDetectStuck
        Ensure-OpsJobDefRecoverDead
        Ensure-OpsJobDefPurgeRaw
        Ensure-EventBridgeRules
    }
    Ensure-VideoCloudWatchAlarms
    Ensure-RdsCloudWatchAlarms
    Ensure-ALBStack
    Ensure-ApiCloudWatchAlarms
    Ensure-ProjectCostAllocationTags
    Ensure-API
    # Converge the CI role only after all four SSOT Launch Templates exist so
    # its write resources can be exact and read back in the same run.
    Ensure-GitHubActionsDeployIAM

    if (-not $SkipBuild) {
        Write-Warn "Build step is deprecated in v1 (GitHub Actions OIDC only). Skipping build on this machine."
    } else {
        Write-Ok "Build skipped (GitHub Actions OIDC only)"
    }

    $netJobId = ""
    $netStatus = ""
    if (-not $SkipNetprobe) {
        try {
            # RunnableFailSec 600: Ops CE cold start (minvCpus=0) can take 5+ min to schedule job.
            $net = Invoke-Netprobe -TimeoutSec 1200 -RunnableFailSec 600
            $netJobId = $net.jobId
            $netStatus = $net.status
        } catch {
            Write-Warn "Netprobe failed (deploy continues): $_"
            if ($_.Exception.Message -match "jobId=([a-f0-9-]+)") { $netJobId = $matches[1] }
            $netStatus = "failed"
        }
    } else {
        Write-Warn "Netprobe skipped (-SkipNetprobe)"
    }

    $ev = Show-Evidence -NetprobeJobId $netJobId -NetprobeStatus $netStatus
    if ($ev) { Save-EvidenceReport -MarkdownContent (Convert-EvidenceToMarkdown -Ev $ev) }

    # After-deploy verification (MinimalDeploy: ALB targets, Workers, Batch CE/Queue)
    if (-not $Plan) {
        $verifyOk = $true
        if ($netStatus -eq "failed") { $verifyOk = $false }
        $R = $script:Region
        Write-Host "`n=== After-Deploy Verification ===" -ForegroundColor Cyan
        try {
            $asgAll = Invoke-AwsJson @("autoscaling", "describe-auto-scaling-groups", "--region", $R, "--output", "json")
            $asgNames = @($script:AiASGName, $script:ToolsASGName, $script:MessagingASGName, $script:ApiASGName) | Where-Object { $_ }
            foreach ($name in $asgNames) {
                $a = $asgAll.AutoScalingGroups | Where-Object { $_.AutoScalingGroupName -eq $name } | Select-Object -First 1
                if ($a) {
                    $desired = $a.DesiredCapacity; $running = ($a.Instances | Where-Object { $_.LifecycleState -eq "InService" -and $_.HealthStatus -eq "Healthy" }).Count
                    Write-Host "  ASG $name : desired=$desired inService=$running" -ForegroundColor $(if ($running -ge 1) { "Green" } else { "Yellow" })
                    if ($running -ne $desired) { $verifyOk = $false }
                } else { Write-Warn "Required ASG missing: $name"; $verifyOk = $false }
            }
            $tg = Invoke-AwsJson @("elbv2", "describe-target-groups", "--names", $script:ApiTargetGroupName, "--region", $R, "--output", "json") 2>$null
            if ($tg -and $tg.TargetGroups -and $tg.TargetGroups.Count -gt 0) {
                $th = Invoke-AwsJson @("elbv2", "describe-target-health", "--target-group-arn", $tg.TargetGroups[0].TargetGroupArn, "--region", $R, "--output", "json") 2>$null
                $healthy = @($th.TargetHealthDescriptions | Where-Object { $_.TargetHealth.State -eq "healthy" }).Count
                $total = if ($th.TargetHealthDescriptions) { $th.TargetHealthDescriptions.Count } else { 0 }
                Write-Host "  ALB target health : $healthy / $total healthy" -ForegroundColor $(if ($healthy -ge 1) { "Green" } else { "Yellow" })
                if ($healthy -lt 1) { $verifyOk = $false }
            } else { Write-Warn "Required API target group missing: $($script:ApiTargetGroupName)"; $verifyOk = $false }
            $ce = Invoke-AwsJson @("batch", "describe-compute-environments", "--compute-environments", $script:VideoCEName, "--region", $R, "--output", "json") 2>$null
            if ($ce -and $ce.computeEnvironments -and $ce.computeEnvironments.Count -gt 0) {
                $ceStatus = $ce.computeEnvironments[0].status; $ceState = $ce.computeEnvironments[0].state
                Write-Host "  Batch Video CE : status=$ceStatus state=$ceState" -ForegroundColor $(if ($ceStatus -eq "VALID") { "Green" } else { "Yellow" })
                if ($ceStatus -ne "VALID" -or $ceState -ne "ENABLED") { $verifyOk = $false }
            } else { Write-Warn "Required Video compute environment missing: $($script:VideoCEName)"; $verifyOk = $false }
            $q = Invoke-AwsJson @("batch", "describe-job-queues", "--job-queues", $script:VideoQueueName, "--region", $R, "--output", "json") 2>$null
            if ($q -and $q.jobQueues -and $q.jobQueues.Count -gt 0) {
                $qState = $q.jobQueues[0].state; $qStatus = $q.jobQueues[0].status
                Write-Host "  Batch Video Queue : status=$qStatus state=$qState" -ForegroundColor $(if ($qState -eq "ENABLED" -and $qStatus -eq "VALID") { "Green" } else { "Yellow" })
                if ($qState -ne "ENABLED" -or $qStatus -ne "VALID") { $verifyOk = $false }
            } else { Write-Warn "Required Video job queue missing: $($script:VideoQueueName)"; $verifyOk = $false }
        } catch {
            Write-Warn "Verification error: $_"
            $verifyOk = $false
        }
        if ($verifyOk) {
            Write-Host "`nDEPLOY_SUCCESS" -ForegroundColor Green
        } elseif ($RelaxedValidation) {
            Write-Warn "DEPLOY_COMPLETED_WITH_VERIFICATION_WARNINGS (explicit -RelaxedValidation)"
        } else {
            throw "Post-deploy verification failed. Refusing to report deployment success; inspect ASG/ALB/Batch output above."
        }
    }
}
catch {
    Write-Fail $_.Exception.Message
    throw
}
finally {
    Release-DeployLock -Reg $script:Region
}

if (-not $script:ChangesMade) {
    Write-Host "Idempotent: No changes required." -ForegroundColor Green
}
Write-Host "=== DEPLOY v1 COMPLETE ===`n" -ForegroundColor Green
