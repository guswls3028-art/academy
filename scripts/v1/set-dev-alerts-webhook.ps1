# Set DEV_ALERTS_WEBHOOK_URL and its required-state in /academy/api/env (SSM SecureString).
# 사용:
#   pwsh scripts/v1/set-dev-alerts-webhook.ps1 -Url "https://hooks.slack.com/services/..."
#   pwsh scripts/v1/set-dev-alerts-webhook.ps1 -Url ""  # 비활성화 (제거)
#
# 인프라:
#   /academy/api/env (SecureString JSON) → /opt/api.env on each API instance via UserData/refresh.
#   academy-api 컨테이너는 /opt/api.env --env-file로 마운트.
#   변경 후 ASG instance refresh 또는 본 스크립트의 -RefreshContainers 옵션 사용.

param(
    [Parameter(Mandatory=$true)] [AllowEmptyString()] [string]$Url,
    [switch]$RefreshContainers,
    [string]$Region = "ap-northeast-2",
    [string]$SsmApiEnv = "/academy/api/env",
    [string]$ApiAsgName = "academy-v1-api-asg"
)

$ErrorActionPreference = "Stop"

if ($Url -ne "" -and -not ($Url -match '^https?://')) {
    Write-Host "ERROR: -Url must start with http(s)://" -ForegroundColor Red
    exit 1
}

. (Join-Path $PSScriptRoot "core\runtime-env-lock.ps1")
Enter-AcademyRuntimeEnvMutationLock `
    -Region $Region `
    -OwnerPrefix "dev-alerts-webhook"
try {
# 1) Read current SSM parameter
Write-Host "[1/3] Reading SSM $SsmApiEnv..."
$existing = aws ssm get-parameter --name $SsmApiEnv --with-decryption --region $Region --output json 2>&1 | ConvertFrom-Json
if (-not $existing.Parameter -or -not $existing.Parameter.Value) {
    Write-Host "ERROR: $SsmApiEnv not found or empty." -ForegroundColor Red
    exit 1
}

$valueRaw = $existing.Parameter.Value
$isBase64 = ($valueRaw -match '^[A-Za-z0-9+/]+=*$') -and ($valueRaw.Length -gt 100)
$jsonStr = $valueRaw
if ($isBase64) {
    $jsonStr = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($valueRaw))
}
$obj = $jsonStr | ConvertFrom-Json

# 2) Update DEV_ALERTS_WEBHOOK_URL and the matching required-state atomically.
$prev = $obj.PSObject.Properties["DEV_ALERTS_WEBHOOK_URL"].Value
if ($Url -eq "") {
    if ($obj.PSObject.Properties["DEV_ALERTS_WEBHOOK_URL"]) {
        $obj.PSObject.Properties.Remove("DEV_ALERTS_WEBHOOK_URL")
        Write-Host "[2/3] Removed DEV_ALERTS_WEBHOOK_URL (was: $(if ($prev) { '<set>' } else { '<unset>' }))"
    } else {
        Write-Host "[2/3] DEV_ALERTS_WEBHOOK_URL already unset, no change"
    }
    $obj | Add-Member -NotePropertyName "DEV_ALERTS_WEBHOOK_REQUIRED" -NotePropertyValue $false -Force
} else {
    $obj | Add-Member -NotePropertyName "DEV_ALERTS_WEBHOOK_URL" -NotePropertyValue $Url -Force
    $obj | Add-Member -NotePropertyName "DEV_ALERTS_WEBHOOK_REQUIRED" -NotePropertyValue $true -Force
    Write-Host "[2/3] Set DEV_ALERTS_WEBHOOK_URL=<configured> (was: $(if ($prev) { '<set>' } else { '<unset>' }))"
}

# 3) Write back
$newJson = $obj | ConvertTo-Json -Compress -Depth 10
$newValue = $newJson
if ($isBase64) {
    $newValue = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($newJson))
}
Assert-AcademyRuntimeEnvMutationLock -Region $Region
aws ssm put-parameter --name $SsmApiEnv --type SecureString --value $newValue --overwrite --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "SSM update failed."
}
Write-Host "[3/3] SSM updated."

# Optional: guarded API ASG refresh with terminal and public-health readback.
if ($RefreshContainers) {
    Write-Host "`n[refresh] Starting guarded API ASG instance refresh..."
    $refreshId = Start-AcademyInstanceRefresh `
        -AutoScalingGroupName $ApiAsgName `
        -Region $Region
    Wait-AcademyInstanceRefresh `
        -AutoScalingGroupName $ApiAsgName `
        -InstanceRefreshId $refreshId `
        -Region $Region
    Assert-AcademyPublicApiHealth
    Complete-AcademyRuntimeRefreshBoundary -Region $Region
    Write-Host "API refresh and public health readback passed." -ForegroundColor Green
} else {
    Write-Host "`nNote: /opt/api.env on running instances NOT refreshed yet."
    Write-Host "  Either run with -RefreshContainers, or wait for next ASG refresh / deploy."
}
} finally {
    Exit-AcademyRuntimeEnvMutationLock -Region $Region
}
