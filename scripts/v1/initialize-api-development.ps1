# One-command bootstrap of the persistent development runtime from the last
# fully verified production release. Requires a non-root least-privilege profile.
[CmdletBinding()]
param(
    [ValidateRange(300, 1800)]
    [int]$TimeoutSec = 900,
    [string]$AwsProfile = "default"
)

$ErrorActionPreference = "Stop"
$ScriptRoot = $PSScriptRoot
if ($AwsProfile -and $AwsProfile.Trim()) {
    $env:AWS_PROFILE = $AwsProfile.Trim()
}
if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = "ap-northeast-2" }

$script:PlanMode = $false
. (Join-Path $ScriptRoot "core\env.ps1")
. (Join-Path $ScriptRoot "core\ssot.ps1")
. (Join-Path $ScriptRoot "core\aws.ps1")
. (Join-Path $ScriptRoot "resources\worker_userdata.ps1")
Assert-AwsMutationIdentity | Out-Null
Load-SSOT -Env prod | Out-Null

& (Join-Path $ScriptRoot "converge-api-development-prerequisites.ps1") `
    -AwsProfile $AwsProfile

$apiRelease = Get-ReleaseManifestImage -RepoName $script:EcrApiRepo
$toolsRelease = Get-ReleaseManifestImage -RepoName $script:EcrToolsRepo
$aiRelease = Get-ReleaseManifestImage -RepoName $script:EcrAiRepo
$messagingRelease = Get-ReleaseManifestImage -RepoName $script:EcrMessagingRepo
if (
    [string]$apiRelease.GitSha -notmatch '^[0-9a-fA-F]{40}$' -or
    [string]$toolsRelease.GitSha -ne [string]$apiRelease.GitSha -or
    [string]$aiRelease.GitSha -ne [string]$apiRelease.GitSha -or
    [string]$messagingRelease.GitSha -ne [string]$apiRelease.GitSha
) {
    throw "Verified API, Tools, AI, and Messaging release images must share one full Git SHA."
}
$releaseId = "sha-$([string]$apiRelease.GitSha)-run-0-0"
$apiImageUri = (
    "$($script:AccountId).dkr.ecr.$($script:Region).amazonaws.com/" +
    "$($script:EcrApiRepo)@$([string]$apiRelease.Digest)"
)
$toolsImageUri = (
    "$($script:AccountId).dkr.ecr.$($script:Region).amazonaws.com/" +
    "$($script:EcrToolsRepo)@$([string]$toolsRelease.Digest)"
)
$aiImageUri = (
    "$($script:AccountId).dkr.ecr.$($script:Region).amazonaws.com/" +
    "$($script:EcrAiRepo)@$([string]$aiRelease.Digest)"
)
$messagingImageUri = (
    "$($script:AccountId).dkr.ecr.$($script:Region).amazonaws.com/" +
    "$($script:EcrMessagingRepo)@$([string]$messagingRelease.Digest)"
)

$outputPath = [System.IO.Path]::GetTempFileName()
try {
    & (Join-Path $ScriptRoot "publish-api-development-env.ps1") `
        -ReleaseId $releaseId `
        -GithubOutputPath $outputPath `
        -AwsProfile $AwsProfile
    $outputs = @{}
    foreach ($line in Get-Content -LiteralPath $outputPath) {
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) { $outputs[$parts[0]] = $parts[1] }
    }
    foreach ($required in @(
        "parameter_version",
        "workers_parameter_version",
        "production_database_name"
    )) {
        if (-not $outputs[$required]) {
            throw "Development environment publisher returned no $required."
        }
    }

    & (Join-Path $ScriptRoot "deploy-api-development.ps1") `
        -ApiImageUri $apiImageUri `
        -ToolsImageUri $toolsImageUri `
        -AiImageUri $aiImageUri `
        -MessagingImageUri $messagingImageUri `
        -ExpectedEnvVersion ([int]$outputs["parameter_version"]) `
        -ExpectedWorkersEnvVersion ([int]$outputs["workers_parameter_version"]) `
        -ExpectedReleaseId $releaseId `
        -ExpectedProductionDatabaseName $outputs["production_database_name"] `
        -TimeoutSec $TimeoutSec `
        -AwsProfile $AwsProfile
} finally {
    Remove-Item -LiteralPath $outputPath -ErrorAction SilentlyContinue
}
