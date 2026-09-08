# Publish a versioned, production-shaped API environment that is bound to the
# isolated development database. Secret values never leave process memory.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?:sha-[0-9a-fA-F]{40}-run-[0-9]+-[0-9]+|manual-sha256-[0-9a-fA-F]{64})$')]
    [string]$ReleaseId,
    [string]$GithubOutputPath = "",
    [switch]$Ci = $false,
    [string]$AwsProfile = "default"
)

$ErrorActionPreference = "Stop"
$ScriptRoot = $PSScriptRoot
if ($Ci) {
    Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue
} elseif ($AwsProfile -and $AwsProfile.Trim()) {
    $env:AWS_PROFILE = $AwsProfile.Trim()
}
if (-not $env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION = "ap-northeast-2" }

$script:PlanMode = $false
. (Join-Path $ScriptRoot "core\env.ps1")
. (Join-Path $ScriptRoot "core\ssot.ps1")
. (Join-Path $ScriptRoot "core\aws.ps1")
Assert-AwsMutationIdentity | Out-Null
Load-SSOT -Env prod | Out-Null

if (-not $script:ApiDevelopmentEnabled) {
    throw "Persistent API development environment is disabled in params.yaml."
}
if ($script:ApiDevelopmentAccessMode -ne "ssm-only") {
    throw "API development access must remain ssm-only."
}

function Get-RequiredSecureParameterValue {
    param([string]$Name)
    $result = Invoke-AwsJson @(
        "ssm", "get-parameter",
        "--name", $Name,
        "--with-decryption",
        "--region", $script:Region,
        "--output", "json"
    )
    if (-not $result -or -not $result.Parameter -or -not $result.Parameter.Value) {
        throw "Required secure parameter is missing or unreadable: $Name"
    }
    return [string]$result.Parameter.Value
}

$productionValue = Get-RequiredSecureParameterValue -Name $script:SsmApiEnv
$productionWorkersValue = Get-RequiredSecureParameterValue -Name $script:SsmWorkersEnv
$credentialValue = Get-RequiredSecureParameterValue -Name $script:ApiDevelopmentCredentialParameter
$r2CredentialValue = Get-RequiredSecureParameterValue -Name $script:ApiDevelopmentR2CredentialParameter
try {
    $production = $productionValue | ConvertFrom-Json
    $productionWorkersJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($productionWorkersValue)
    )
    $productionWorkers = $productionWorkersJson | ConvertFrom-Json
    $credential = $credentialValue | ConvertFrom-Json
    $r2Credential = $r2CredentialValue | ConvertFrom-Json
} catch {
    throw "API development source parameters must contain the expected JSON/base64(JSON) objects."
}

$productionDatabaseName = [string]$production.DB_NAME
$productionDatabaseUser = [string]$production.DB_USER
$developmentDatabaseName = [string]$script:ApiDevelopmentDatabaseName
$developmentDatabaseUser = [string]$script:ApiDevelopmentDatabaseUser
$credentialUser = [string]$credential.DB_USER
$credentialPassword = [string]$credential.DB_PASSWORD
$r2Endpoint = ([string]$r2Credential.R2_ENDPOINT).Trim().TrimEnd("/")
$r2Region = ([string]$r2Credential.R2_REGION).Trim()
$r2AccessKey = ([string]$r2Credential.R2_ACCESS_KEY).Trim()
$r2SecretKey = ([string]$r2Credential.R2_SECRET_KEY).Trim()
$r2Bucket = ([string]$r2Credential.R2_BUCKET).Trim()
if ([string]$production.DJANGO_SETTINGS_MODULE -ne "apps.api.config.settings.prod") {
    throw "Production API env does not select the production settings module."
}
if (-not $productionDatabaseName -or $productionDatabaseName -eq $developmentDatabaseName) {
    throw "Production and development database names must be distinct."
}
if (-not $productionDatabaseUser -or $productionDatabaseUser -eq $developmentDatabaseUser) {
    throw "Production and development database users must be distinct."
}
if ($credentialUser -ne $developmentDatabaseUser) {
    throw "Development credential parameter is not bound to the dedicated database role."
}
if (-not $credentialPassword -or $credentialPassword.Length -lt 32) {
    throw "Development database password is missing or too short."
}
if (
    $r2Endpoint -notmatch '^https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com$' -or
    $r2Region -ne "auto" -or
    $r2AccessKey.Length -lt 16 -or
    $r2SecretKey.Length -lt 32 -or
    $r2Bucket -ne [string]$script:ApiDevelopmentR2BucketName -or
    -not $r2Bucket.StartsWith("academy-development-")
) {
    throw (
        "Development R2 credential must contain a dedicated endpoint/key pair " +
        "and the exact development bucket from params.yaml."
    )
}
$productionBucketNames = @(
    "academy-ai",
    "academy-video",
    "academy-excel",
    "academy-storage",
    "academy-admin",
    [string]$production.R2_AI_BUCKET,
    [string]$production.R2_VIDEO_BUCKET,
    [string]$production.R2_EXCEL_BUCKET,
    [string]$production.R2_STORAGE_BUCKET,
    [string]$production.R2_ADMIN_BUCKET,
    [string]$productionWorkers.R2_AI_BUCKET,
    [string]$productionWorkers.R2_VIDEO_BUCKET,
    [string]$productionWorkers.R2_EXCEL_BUCKET,
    [string]$productionWorkers.R2_STORAGE_BUCKET,
    [string]$productionWorkers.R2_ADMIN_BUCKET
) | Where-Object { $_ } | Sort-Object -Unique
if ($r2Bucket -in $productionBucketNames) {
    throw "Development R2 bucket must not overlap any production R2 bucket."
}

$productionCdnSigningSecret = ([string]$production.CDN_HLS_SIGNING_SECRET).Trim()
if (
    ([string]$production.CDN_HLS_BASE_URL).Trim().TrimEnd("/") -ne "https://cdn.hakwonplus.com" -or
    $productionCdnSigningSecret.Length -lt 32 -or
    ([string]$production.CDN_HLS_SIGNING_KEY_ID).Trim() -ne "v1"
) {
    throw "Production source env does not contain the canonical signed video contract."
}
$cdnSigningSecretBytes = New-Object byte[] 32
try {
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($cdnSigningSecretBytes)
    $script:ApiDevelopmentCdnSigningSecret = [Convert]::ToBase64String(
        $cdnSigningSecretBytes
    )
} finally {
    [Array]::Clear($cdnSigningSecretBytes, 0, $cdnSigningSecretBytes.Length)
}
if (
    $script:ApiDevelopmentCdnSigningSecret.Length -lt 32 -or
    $script:ApiDevelopmentCdnSigningSecret -eq $productionCdnSigningSecret
) {
    throw "Development video signing secret generation did not preserve isolation."
}

function Set-IsolatedDevelopmentValues {
    param(
        [object]$Target,
        [string]$SettingsModule
    )
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $djangoSecret = [Convert]::ToBase64String(
            $sha256.ComputeHash(
                [Text.Encoding]::UTF8.GetBytes(
                    "academy-development-django:$credentialPassword"
                )
            )
        )
        $bindingSecret = [Convert]::ToBase64String(
            $sha256.ComputeHash(
                [Text.Encoding]::UTF8.GetBytes(
                    "academy-development-messaging:$credentialPassword"
                )
            )
        )
    } finally {
        $sha256.Dispose()
    }
    $secretPatterns = @(
        '^SOLAPI_',
        '^TOSS_',
        '^BILLING_',
        '^DEV_ALERTS_',
        '^CDN_HLS_SIGNING_',
        '^VAPID_PRIVATE_KEY$',
        '^OPENAI_',
        '^ANTHROPIC_',
        '^AWS_ACCESS_KEY_ID$',
        '^AWS_SECRET_ACCESS_KEY$',
        '^AWS_SESSION_TOKEN$',
        '^AWS_ROOT_'
    )
    foreach ($property in @($Target.PSObject.Properties)) {
        if ($secretPatterns | Where-Object { $property.Name -match $_ }) {
            $Target.PSObject.Properties.Remove($property.Name)
        }
    }
    $values = [ordered]@{
        DJANGO_SETTINGS_MODULE = $SettingsModule
        ACADEMY_RUNTIME_ENV = "development"
        ACADEMY_DEVELOPMENT_RELEASE_ID = $ReleaseId
        SECRET_KEY = $djangoSecret
        MESSAGING_TENANT_BINDING_KEY = $bindingSecret
        MESSAGING_TENANT_BINDING_FALLBACK_KEYS = ""
        CDN_HLS_BASE_URL = "https://cdn.hakwonplus.com"
        CDN_HLS_SIGNING_SECRET = $script:ApiDevelopmentCdnSigningSecret
        CDN_HLS_SIGNING_KEY_ID = "v1"
        DB_NAME = $developmentDatabaseName
        DB_USER = $credentialUser
        DB_PASSWORD = $credentialPassword
        AWS_DEFAULT_REGION = $script:Region
        R2_ENDPOINT = $r2Endpoint
        R2_REGION = $r2Region
        R2_ACCESS_KEY = $r2AccessKey
        R2_SECRET_KEY = $r2SecretKey
        R2_AI_BUCKET = $r2Bucket
        R2_VIDEO_BUCKET = $r2Bucket
        R2_STORAGE_BUCKET = $r2Bucket
        R2_EXCEL_BUCKET = $r2Bucket
        R2_ADMIN_BUCKET = $r2Bucket
        R2_PUBLIC_BASE_URL = ""
        R2_ADMIN_PUBLIC_BASE_URL = ""
        AI_SQS_QUEUE_NAME_LITE = $script:ApiDevelopmentAiQueueName
        AI_SQS_QUEUE_NAME_BASIC = $script:ApiDevelopmentAiQueueName
        AI_SQS_QUEUE_NAME_PREMIUM = $script:ApiDevelopmentAiQueueName
        TOOLS_SQS_QUEUE_NAME = $script:ApiDevelopmentToolsQueueName
        MESSAGING_SQS_QUEUE_NAME = $script:ApiDevelopmentMessagingQueueName
        VIDEO_BATCH_JOB_QUEUE = ""
        VIDEO_BATCH_JOB_DEFINITION = ""
        REDIS_HOST = "127.0.0.1"
        REDIS_PORT = "6379"
        SOLAPI_MOCK = "true"
        SOLAPI_API_KEY = ""
        SOLAPI_API_SECRET = ""
        SOLAPI_SENDER = ""
        SOLAPI_KAKAO_PF_ID = ""
        SOLAPI_KAKAO_TEMPLATE_ID = ""
        MESSAGING_DRY_RUN_TRIGGERS = "*"
        TOSS_AUTO_BILLING_ENABLED = "false"
        TOSS_PAYMENTS_CLIENT_KEY = ""
        TOSS_PAYMENTS_SECRET_KEY = ""
        BILLING_BANK_TRANSFER_ENABLED = "false"
        BILLING_KEY_ENCRYPTION_WRITE_ENABLED = "false"
        BILLING_KEY_ENCRYPTION_PRIMARY_KEY = ""
        BILLING_KEY_ENCRYPTION_FALLBACK_KEYS = ""
        SENTRY_ENVIRONMENT = "development"
    }
    foreach ($entry in $values.GetEnumerator()) {
        $Target | Add-Member -NotePropertyName $entry.Key -NotePropertyValue $entry.Value -Force
    }
    $Target.PSObject.Properties.Remove("ACADEMY_PREPROD_RELEASE_ID")
}

Set-IsolatedDevelopmentValues `
    -Target $production `
    -SettingsModule "apps.api.config.settings.development"
Set-IsolatedDevelopmentValues `
    -Target $productionWorkers `
    -SettingsModule "apps.api.config.settings.worker"
$value = $production | ConvertTo-Json -Compress -Depth 20
$workersJson = $productionWorkers | ConvertTo-Json -Compress -Depth 20
$workersValue = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($workersJson))

$put = Invoke-AwsJson @(
    "ssm", "put-parameter",
    "--name", $script:ApiDevelopmentEnvParameter,
    "--description", "Versioned environment for the isolated persistent Academy API development instance",
    "--type", "SecureString",
    "--tier", "Advanced",
    "--value", $value,
    "--overwrite",
    "--region", $script:Region,
    "--output", "json"
)
if (-not $put -or -not $put.Version) {
    throw "API development env publication returned no parameter version."
}
$version = [int]$put.Version

$workersPut = Invoke-AwsJson @(
    "ssm", "put-parameter",
    "--name", $script:ApiDevelopmentWorkersEnvParameter,
    "--description", "Versioned worker environment for isolated Academy development tools jobs",
    "--type", "SecureString",
    "--tier", "Advanced",
    "--value", $workersValue,
    "--overwrite",
    "--region", $script:Region,
    "--output", "json"
)
if (-not $workersPut -or -not $workersPut.Version) {
    throw "API development workers env publication returned no parameter version."
}
$workersVersion = [int]$workersPut.Version

$readback = Invoke-AwsJson @(
    "ssm", "get-parameter",
    "--name", "$($script:ApiDevelopmentEnvParameter):$version",
    "--with-decryption",
    "--region", $script:Region,
    "--output", "json"
)
if (-not $readback -or -not $readback.Parameter -or -not $readback.Parameter.Value) {
    throw "Versioned API development env readback failed."
}
$actual = [string]$readback.Parameter.Value | ConvertFrom-Json
if (
    [string]$actual.ACADEMY_DEVELOPMENT_RELEASE_ID -ne $ReleaseId -or
    [string]$actual.DB_NAME -ne $developmentDatabaseName -or
    [string]$actual.DB_USER -ne $developmentDatabaseUser -or
    [string]$actual.DB_PASSWORD -ne $credentialPassword -or
    [string]$actual.DJANGO_SETTINGS_MODULE -ne "apps.api.config.settings.development" -or
    [string]$actual.ACADEMY_RUNTIME_ENV -ne "development" -or
    [string]$actual.CDN_HLS_BASE_URL -ne "https://cdn.hakwonplus.com" -or
    [string]$actual.CDN_HLS_SIGNING_SECRET -ne $script:ApiDevelopmentCdnSigningSecret -or
    [string]$actual.CDN_HLS_SIGNING_KEY_ID -ne "v1" -or
    [string]$actual.TOOLS_SQS_QUEUE_NAME -ne $script:ApiDevelopmentToolsQueueName -or
    [string]$actual.VIDEO_BATCH_JOB_QUEUE -ne "" -or
    [string]$actual.VIDEO_BATCH_JOB_DEFINITION -ne "" -or
    [string]$actual.R2_ENDPOINT -ne $r2Endpoint -or
    [string]$actual.R2_ACCESS_KEY -ne $r2AccessKey -or
    [string]$actual.R2_SECRET_KEY -ne $r2SecretKey -or
    [string]$actual.R2_STORAGE_BUCKET -ne $r2Bucket
) {
    throw "Versioned API development env readback does not match the isolated boundary."
}
$workersReadback = Invoke-AwsJson @(
    "ssm", "get-parameter",
    "--name", "$($script:ApiDevelopmentWorkersEnvParameter):$workersVersion",
    "--with-decryption",
    "--region", $script:Region,
    "--output", "json"
)
try {
    $actualWorkersJson = [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String([string]$workersReadback.Parameter.Value)
    )
    $actualWorkers = $actualWorkersJson | ConvertFrom-Json
} catch {
    throw "Versioned API development workers env readback is invalid."
}
if (
    [string]$actualWorkers.ACADEMY_DEVELOPMENT_RELEASE_ID -ne $ReleaseId -or
    [string]$actualWorkers.DB_NAME -ne $developmentDatabaseName -or
    [string]$actualWorkers.DB_USER -ne $developmentDatabaseUser -or
    [string]$actualWorkers.DJANGO_SETTINGS_MODULE -ne "apps.api.config.settings.worker" -or
    [string]$actualWorkers.CDN_HLS_BASE_URL -ne "https://cdn.hakwonplus.com" -or
    [string]$actualWorkers.CDN_HLS_SIGNING_SECRET -ne $script:ApiDevelopmentCdnSigningSecret -or
    [string]$actualWorkers.CDN_HLS_SIGNING_KEY_ID -ne "v1" -or
    [string]$actualWorkers.TOOLS_SQS_QUEUE_NAME -ne $script:ApiDevelopmentToolsQueueName -or
    [string]$actualWorkers.VIDEO_BATCH_JOB_QUEUE -ne "" -or
    [string]$actualWorkers.VIDEO_BATCH_JOB_DEFINITION -ne "" -or
    [string]$actualWorkers.R2_ENDPOINT -ne $r2Endpoint -or
    [string]$actualWorkers.R2_ACCESS_KEY -ne $r2AccessKey -or
    [string]$actualWorkers.R2_SECRET_KEY -ne $r2SecretKey -or
    [string]$actualWorkers.R2_STORAGE_BUCKET -ne $r2Bucket
) {
    throw "Versioned API development workers env readback does not match the isolated boundary."
}

$safeOutputs = [ordered]@{
    parameter_name = $script:ApiDevelopmentEnvParameter
    parameter_version = [string]$version
    workers_parameter_name = $script:ApiDevelopmentWorkersEnvParameter
    workers_parameter_version = [string]$workersVersion
    release_id = $ReleaseId
    development_database_name = $developmentDatabaseName
    development_database_user = $developmentDatabaseUser
    production_database_name = $productionDatabaseName
}
if ($GithubOutputPath) {
    foreach ($entry in $safeOutputs.GetEnumerator()) {
        Add-Content -LiteralPath $GithubOutputPath -Value "$($entry.Key)=$($entry.Value)"
    }
}
Write-Host (
    "API_DEVELOPMENT_ENV_PUBLISHED parameter={0} version={1} workers_version={2} release={3} database={4} role={5}" -f
    $script:ApiDevelopmentEnvParameter,
    $version,
    $workersVersion,
    $ReleaseId,
    $developmentDatabaseName,
    $developmentDatabaseUser
) -ForegroundColor Green
