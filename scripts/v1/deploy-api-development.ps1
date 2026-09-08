# Blue/green replacement for the one persistent, SSM-only development API.
# A candidate is promoted only after image, env, DB, queue, object-storage,
# Redis, and HTTP proofs pass. The prior active instance remains running on
# every candidate failure.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/academy-api@sha256:[0-9a-fA-F]{64}$')]
    [string]$ApiImageUri,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/academy-tools-worker@sha256:[0-9a-fA-F]{64}$')]
    [string]$ToolsImageUri,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/academy-ai-worker-cpu@sha256:[0-9a-fA-F]{64}$')]
    [string]$AiImageUri,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 100000)]
    [int]$ExpectedEnvVersion,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 100000)]
    [int]$ExpectedWorkersEnvVersion,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?:sha-[0-9a-fA-F]{40}-run-[0-9]+-[0-9]+|manual-sha256-[0-9a-fA-F]{64})$')]
    [string]$ExpectedReleaseId,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z][a-z0-9_]{2,62}$')]
    [string]$ExpectedProductionDatabaseName,
    [ValidateRange(300, 1800)]
    [int]$TimeoutSec = 900,
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
. (Join-Path $ScriptRoot "core\logging.ps1")
. (Join-Path $ScriptRoot "core\aws.ps1")
. (Join-Path $ScriptRoot "core\wait.ps1")
. (Join-Path $ScriptRoot "resources\worker_userdata.ps1")
. (Join-Path $ScriptRoot "resources\api.ps1")
Assert-AwsMutationIdentity | Out-Null
Load-SSOT -Env prod | Out-Null

if (-not $script:ApiDevelopmentEnabled) {
    throw "Persistent API development environment is disabled in params.yaml."
}
if ($script:ApiDevelopmentAccessMode -ne "ssm-only") {
    throw "API development access must remain ssm-only."
}
if (-not $script:ApiDevelopmentMatchProductionCompute) {
    throw "API development must match the production compute contract."
}
Assert-ImmutableEcrImageUri -ImageUri $ApiImageUri
Assert-ImmutableEcrImageUri -ImageUri $ToolsImageUri
Assert-ImmutableEcrImageUri -ImageUri $AiImageUri

$profile = Invoke-AwsJson @(
    "iam", "get-instance-profile",
    "--instance-profile-name", $script:ApiDevelopmentInstanceProfileName,
    "--output", "json"
)
$profileRoles = @($profile.InstanceProfile.Roles | Where-Object { $_.RoleName })
if (
    $profileRoles.Count -ne 1 -or
    [string]$profileRoles[0].RoleName -ne $script:ApiDevelopmentRoleName
) {
    throw "API development instance profile is not bound exclusively to its dedicated role."
}

$securityGroupResult = Invoke-AwsJson @(
    "ec2", "describe-security-groups",
    "--filters",
    "Name=vpc-id,Values=$($script:VpcId)",
    "Name=group-name,Values=$($script:ApiDevelopmentSecurityGroupName)",
    "--region", $script:Region,
    "--output", "json"
)
$securityGroup = @($securityGroupResult.SecurityGroups)[0]
if (-not $securityGroup -or @($securityGroup.IpPermissions).Count -ne 0) {
    throw "API development security group is missing or has inbound rules."
}

$asgResult = Invoke-AwsJson @(
    "autoscaling", "describe-auto-scaling-groups",
    "--auto-scaling-group-names", $script:ApiASGName,
    "--region", $script:Region,
    "--output", "json"
)
$productionAsg = @($asgResult.AutoScalingGroups)[0]
$productionLaunchTemplate = $productionAsg.LaunchTemplate
if (-not $productionAsg -or -not $productionLaunchTemplate.LaunchTemplateId) {
    throw "Production API ASG/Launch Template is unavailable for compute-shape readback."
}
$productionVersion = Invoke-AwsJson @(
    "ec2", "describe-launch-template-versions",
    "--launch-template-id", $productionLaunchTemplate.LaunchTemplateId,
    "--versions", ([string]$productionLaunchTemplate.Version),
    "--region", $script:Region,
    "--output", "json"
)
$productionData = @($productionVersion.LaunchTemplateVersions)[0].LaunchTemplateData
$ami = [string]$productionData.ImageId
$instanceType = [string]$productionData.InstanceType
if (-not $ami) { $ami = [string]$script:ApiAmiId }
if (-not $instanceType) { $instanceType = [string]$script:ApiInstanceType }
if ($ami -ne [string]$script:ApiAmiId -or $instanceType -ne [string]$script:ApiInstanceType) {
    throw "Production Launch Template compute shape does not match API SSOT."
}

$subnets = @($script:PublicSubnets | Where-Object { $_ })
$productionSubnets = @(
    ([string]$productionAsg.VPCZoneIdentifier -split ",") |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
if ($subnets.Count -eq 0) {
    $subnets = $productionSubnets
}
if ($subnets.Count -eq 0) {
    throw "No production API subnet is available for the SSM-only development host."
}
$subnet = [string]$subnets[0]

$apiEnv = "$($script:ApiDevelopmentEnvParameter):$ExpectedEnvVersion"
$workersEnv = "$($script:ApiDevelopmentWorkersEnvParameter):$ExpectedWorkersEnvVersion"
$userData = Get-ApiLaunchTemplateUserData `
    -ApiImageUri $ApiImageUri `
    -Region $script:Region `
    -SsmApiEnvParam $apiEnv `
    -DeploymentId "development-$ExpectedReleaseId" `
    -ExpectedSettingsModule "apps.api.config.settings.development"
if (-not $userData) { throw "API development userdata rendering failed." }

$bootstrap = @'
# Development-only local cache. No production Redis endpoint is used.
if ! dnf install -y redis6; then
  dnf install -y valkey
fi
if systemctl list-unit-files redis6.service >/dev/null 2>&1; then
  systemctl enable --now redis6
  CACHE_CLI=redis6-cli
elif systemctl list-unit-files valkey.service >/dev/null 2>&1; then
  systemctl enable --now valkey
  CACHE_CLI=valkey-cli
else
  echo "No supported local Redis/Valkey service was installed" >&2
  exit 1
fi
if ! command -v "$CACHE_CLI" >/dev/null 2>&1; then
  CACHE_CLI=$(command -v redis-cli || command -v valkey-cli || true)
fi
test -n "$CACHE_CLI"
"$CACHE_CLI" ping | grep -q PONG

# The isolated development database role may migrate only its own database.
docker run --rm --network host --env-file /opt/api.env "__API_IMAGE__" \
  python manage.py migrate --noinput
'@
$bootstrap = $bootstrap.Replace("__API_IMAGE__", $ApiImageUri)
$userData = [regex]::Replace($userData, '(?m)^# 4\)', "$bootstrap`n# 4)", 1)
$userData = $userData.Replace(
    "docker run -d --restart unless-stopped --name academy-api -p 8000:8000",
    "docker run -d --restart unless-stopped --network host --name academy-api"
)

$toolsBootstrap = @'

# Development Tools worker stays a separate container/process and consumes
# only the dedicated development queue.
TOOLS_IMAGE="__TOOLS_IMAGE__"
if ! docker pull "$TOOLS_IMAGE" 2>>"$LOG"; then
  log "Development tools image pull failed: $TOOLS_IMAGE"
  exit 1
fi
WORKERS_ENV_B64="$(aws ssm get-parameter \
  --name "__WORKERS_ENV__" \
  --with-decryption \
  --query Parameter.Value \
  --output text \
  --region "__REGION__" 2>>"$LOG")"
printf '%s' "$WORKERS_ENV_B64" | base64 -d | python3 -c \
  "import sys,json; d=json.load(sys.stdin); assert d.get('DJANGO_SETTINGS_MODULE') == 'apps.api.config.settings.worker'; assert d.get('ACADEMY_RUNTIME_ENV') == 'development'; assert d.get('ACADEMY_DEVELOPMENT_RELEASE_ID') == '__RELEASE_ID__'; assert d.get('DB_NAME') == '__DATABASE__'; assert d.get('TOOLS_SQS_QUEUE_NAME') == '__TOOLS_QUEUE__'; assert d.get('AI_SQS_QUEUE_NAME_LITE') == '__AI_QUEUE__'; assert d.get('AI_SQS_QUEUE_NAME_BASIC') == '__AI_QUEUE__'; assert d.get('AI_SQS_QUEUE_NAME_PREMIUM') == '__AI_QUEUE__'; assert d.get('VIDEO_BATCH_JOB_QUEUE') == ''; assert d.get('VIDEO_BATCH_JOB_DEFINITION') == ''; [print(k+'='+str(v)) for k,v in d.items()]" \
  > /opt/workers-development.env
test -s /opt/workers-development.env
docker stop academy-tools-development 2>/dev/null || true
docker rm academy-tools-development 2>/dev/null || true
docker run -d --restart unless-stopped --network host \
  --name academy-tools-development \
  --env-file /opt/workers-development.env \
  -e DJANGO_SETTINGS_MODULE=apps.api.config.settings.worker \
  "$TOOLS_IMAGE"
'@
$toolsBootstrap = $toolsBootstrap.Replace("__TOOLS_IMAGE__", $ToolsImageUri)
$toolsBootstrap = $toolsBootstrap.Replace("__WORKERS_ENV__", $workersEnv)
$toolsBootstrap = $toolsBootstrap.Replace("__REGION__", $script:Region)
$toolsBootstrap = $toolsBootstrap.Replace("__RELEASE_ID__", $ExpectedReleaseId)
$toolsBootstrap = $toolsBootstrap.Replace("__DATABASE__", $script:ApiDevelopmentDatabaseName)
$toolsBootstrap = $toolsBootstrap.Replace("__TOOLS_QUEUE__", $script:ApiDevelopmentToolsQueueName)
$toolsBootstrap = $toolsBootstrap.Replace("__AI_QUEUE__", $script:ApiDevelopmentAiQueueName)
$userData = "$userData`n$toolsBootstrap"

$aiBootstrap = @'

# Development AI worker stays a separate container/process and consumes only
# the dedicated development AI queue. Disable production ASG scale-in logic.
AI_IMAGE="__AI_IMAGE__"
if ! docker pull "$AI_IMAGE" 2>>"$LOG"; then
  log "Development AI image pull failed: $AI_IMAGE"
  exit 1
fi
docker stop academy-ai-development 2>/dev/null || true
docker rm academy-ai-development 2>/dev/null || true
docker run -d --restart unless-stopped --network host \
  --name academy-ai-development \
  --env-file /opt/workers-development.env \
  -e DJANGO_SETTINGS_MODULE=apps.api.config.settings.worker \
  -e ACADEMY_RUNTIME_ENV=development \
  -e AI_WORKER_IDLE_SCALE_IN_ENABLED=0 \
  -e EC2_IDLE_STOP_THRESHOLD=0 \
  "$AI_IMAGE"
'@
$aiBootstrap = $aiBootstrap.Replace("__AI_IMAGE__", $AiImageUri)
$userData = "$userData`n$aiBootstrap"
if (
    $userData -notmatch 'apps\.api\.config\.settings\.development' -or
    $userData -notmatch [regex]::Escape($script:ApiDevelopmentAiQueueName) -or
    $userData -notmatch [regex]::Escape($script:ApiDevelopmentToolsQueueName) -or
    $userData -match '/academy/api/env'
) {
    throw "API development userdata failed the isolated env/worker boundary check."
}

$networkInterfacePayload = @(
    [ordered]@{
        AssociatePublicIpAddress = $true
        DeleteOnTermination = $true
        DeviceIndex = 0
        Groups = @([string]$securityGroup.GroupId)
        SubnetId = $subnet
    }
)
$networkInterfaces = ConvertTo-Json `
    -InputObject $networkInterfacePayload `
    -Depth 8 `
    -Compress
$networkRef = Convert-JsonArgToFileRef $networkInterfaces
$networkFile = $networkRef -replace '^file://', ''
$tagSpec = (
    "ResourceType=instance,Tags=[" +
    "{Key=Name,Value=$($script:ApiDevelopmentInstanceName)}," +
    "{Key=Project,Value=academy}," +
    "{Key=Environment,Value=development}," +
    "{Key=ManagedBy,Value=$($script:ApiDevelopmentManagedByTag)}," +
    "{Key=Lifecycle,Value=candidate}," +
    "{Key=ReleaseId,Value=$ExpectedReleaseId}]"
)

$oldResult = Invoke-AwsJson @(
    "ec2", "describe-instances",
    "--filters",
    "Name=tag:Name,Values=$($script:ApiDevelopmentInstanceName)",
    "Name=tag:ManagedBy,Values=$($script:ApiDevelopmentManagedByTag)",
    "Name=tag:Lifecycle,Values=active",
    "Name=instance-state-name,Values=pending,running,stopping,stopped",
    "--region", $script:Region,
    "--output", "json"
)
$oldInstanceIds = @(
    $oldResult.Reservations.Instances |
        ForEach-Object { [string]$_.InstanceId } |
        Where-Object { $_ }
)

$instanceId = ""
$promoted = $false
try {
    $runArgs = @(
        "ec2", "run-instances",
        "--image-id", $ami,
        "--instance-type", $instanceType,
        "--iam-instance-profile", "Name=$($script:ApiDevelopmentInstanceProfileName)",
        "--network-interfaces", $networkRef,
        "--user-data", $userData,
        # The API and Tools containers use the dedicated EC2 role for SQS.
        # IMDSv2 responses cross the host/container network boundary, so a hop
        # limit of two is required while tokens remain mandatory.
        "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled,HttpPutResponseHopLimit=2",
        "--instance-initiated-shutdown-behavior", "stop",
        "--tag-specifications", $tagSpec,
        "--count", "1",
        "--region", $script:Region,
        "--output", "json"
    )
    $runRaw = Invoke-Aws $runArgs -ErrorMessage "launch API development candidate"
    $run = ($runRaw | Out-String).Trim() | ConvertFrom-Json
    $instanceId = [string]@($run.Instances)[0].InstanceId
    if (-not $instanceId) { throw "API development launch returned no instance id." }

    Invoke-Aws @(
        "ec2", "wait", "instance-running",
        "--instance-ids", $instanceId,
        "--region", $script:Region
    ) -ErrorMessage "wait for API development instance-running" | Out-Null
    Wait-SSMOnline -InstanceId $instanceId -Reg $script:Region -TimeoutSec $TimeoutSec

$remote = @'
set -euo pipefail
for container in academy-api academy-tools-development academy-ai-development; do
  for i in $(seq 1 90); do
    state=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)
    [ "$state" = "running" ] && break
    sleep 5
  done
  state=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)
  [ "$state" = "running" ] || { echo "DEVELOPMENT_FAIL container=$container state=$state" >&2; exit 1; }
done

cat >/tmp/academy_development_verify.py <<'PY'
import boto3
import os
from botocore.exceptions import ClientError
from django.conf import settings
from django.db import connection

assert os.environ["DJANGO_SETTINGS_MODULE"] == "apps.api.config.settings.development"
assert settings.DATABASES["default"]["NAME"] == "__DATABASE__"
assert settings.DATABASES["default"]["USER"] == "__DATABASE_USER__"
assert settings.TOOLS_SQS_QUEUE_NAME == "__TOOLS_QUEUE__"
assert settings.MESSAGING_SQS_QUEUE_NAME == "__MESSAGING_QUEUE__"
assert settings.R2_STORAGE_BUCKET == "__BUCKET__"
assert settings.R2_ENDPOINT.endswith(".r2.cloudflarestorage.com")
assert settings.R2_ACCESS_KEY and settings.R2_SECRET_KEY
assert settings.CDN_HLS_BASE_URL.rstrip("/") == "https://cdn.hakwonplus.com"
assert len(settings.CDN_HLS_SIGNING_SECRET.strip()) >= 32
assert settings.CDN_HLS_SIGNING_KEY_ID == "v1"
assert not settings.VIDEO_BATCH_JOB_QUEUE
assert not settings.VIDEO_BATCH_JOB_DEFINITION

connection.ensure_connection()
with connection.cursor() as cursor:
    cursor.execute(
        "SELECT current_database(), current_user, "
        "has_database_privilege(current_user, %s, 'CONNECT')",
        ["__PRODUCTION_DATABASE__"],
    )
    database, user, production_connect = cursor.fetchone()
assert database == "__DATABASE__"
assert user == "__DATABASE_USER__"
assert production_connect is False

r2 = boto3.client(
    "s3",
    endpoint_url=settings.R2_ENDPOINT,
    aws_access_key_id=settings.R2_ACCESS_KEY,
    aws_secret_access_key=settings.R2_SECRET_KEY,
    region_name=settings.R2_REGION,
)
r2.head_bucket(Bucket="__BUCKET__")
for forbidden_bucket in (
    "academy-ai",
    "academy-video",
    "academy-excel",
    "academy-storage",
    "academy-admin",
):
    try:
        r2.head_bucket(Bucket=forbidden_bucket)
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        assert error_code in {"403", "404", "AccessDenied", "NoSuchBucket"}
        continue
    raise AssertionError(
        f"development R2 credential can access production bucket {forbidden_bucket}"
    )
sqs = boto3.client("sqs", region_name="__REGION__")
for queue_name in ("__AI_QUEUE__", "__TOOLS_QUEUE__", "__MESSAGING_QUEUE__"):
    sqs.get_queue_url(QueueName=queue_name)
print("DEVELOPMENT_BOUNDARY_PASS")
PY
docker exec -i academy-api python manage.py shell </tmp/academy_development_verify.py |
  grep -q DEVELOPMENT_BOUNDARY_PASS
rm -f /tmp/academy_development_verify.py

cdn_fingerprint_code='import hashlib; from django.conf import settings; secret=settings.CDN_HLS_SIGNING_SECRET.strip(); assert settings.CDN_HLS_BASE_URL.rstrip("/") == "https://cdn.hakwonplus.com"; assert len(secret) >= 32; assert settings.CDN_HLS_SIGNING_KEY_ID == "v1"; print(hashlib.sha256(secret.encode()).hexdigest())'
api_cdn_signing_fingerprint=$(docker exec academy-api python -c "$cdn_fingerprint_code")
for container in academy-tools-development academy-ai-development; do
  worker_cdn_signing_fingerprint=$(docker exec "$container" python -c "$cdn_fingerprint_code")
  [ "$worker_cdn_signing_fingerprint" = "$api_cdn_signing_fingerprint" ] || {
    echo "DEVELOPMENT_FAIL container=$container video_signing_mismatch=true" >&2
    exit 1
  }
done

curl -fsS --max-time 10 http://127.0.0.1:8000/healthz >/dev/null
curl -fsS --max-time 10 http://127.0.0.1:8000/health >/dev/null
api_image=$(docker inspect -f '{{.Config.Image}}' academy-api)
tools_image=$(docker inspect -f '{{.Config.Image}}' academy-tools-development)
ai_image=$(docker inspect -f '{{.Config.Image}}' academy-ai-development)
[ "$api_image" = "__API_IMAGE__" ]
[ "$tools_image" = "__TOOLS_IMAGE__" ]
[ "$ai_image" = "__AI_IMAGE__" ]
(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' academy-ai-development | grep -qx 'ACADEMY_RUNTIME_ENV=development')
(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' academy-ai-development | grep -qx 'AI_WORKER_IDLE_SCALE_IN_ENABLED=0')
(redis6-cli ping 2>/dev/null || valkey-cli ping 2>/dev/null || redis-cli ping) | grep -q PONG
echo DEVELOPMENT_RUNTIME_PASS
'@
    $remote = $remote.Replace("__DATABASE__", $script:ApiDevelopmentDatabaseName)
    $remote = $remote.Replace("__DATABASE_USER__", $script:ApiDevelopmentDatabaseUser)
    $remote = $remote.Replace("__PRODUCTION_DATABASE__", $ExpectedProductionDatabaseName)
    $remote = $remote.Replace("__BUCKET__", $script:ApiDevelopmentR2BucketName)
    $remote = $remote.Replace("__AI_QUEUE__", $script:ApiDevelopmentAiQueueName)
    $remote = $remote.Replace("__TOOLS_QUEUE__", $script:ApiDevelopmentToolsQueueName)
    $remote = $remote.Replace("__MESSAGING_QUEUE__", $script:ApiDevelopmentMessagingQueueName)
    $remote = $remote.Replace("__REGION__", $script:Region)
    $remote = $remote.Replace("__API_IMAGE__", $ApiImageUri)
    $remote = $remote.Replace("__TOOLS_IMAGE__", $ToolsImageUri)
    $remote = $remote.Replace("__AI_IMAGE__", $AiImageUri)
    $remote = $remote.Replace("`r", "")
    $remoteB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remote))
    $command = "echo '$remoteB64' | base64 -d | bash"
    $paramsRef = Convert-JsonArgToFileRef (
        @{
            commands = @($command)
            executionTimeout = @([string]$TimeoutSec)
        } | ConvertTo-Json -Compress
    )
    $paramsFile = $paramsRef -replace '^file://', ''
    try {
        $sent = Invoke-AwsJson @(
            "ssm", "send-command",
            "--instance-ids", $instanceId,
            "--document-name", "AWS-RunShellScript",
            "--parameters", $paramsRef,
            "--timeout-seconds", [string]$TimeoutSec,
            "--region", $script:Region,
            "--comment", "Verify isolated persistent Academy API development candidate",
            "--output", "json"
        )
    } finally {
        Remove-TempFiles @($paramsFile)
    }
    $commandId = [string]$sent.Command.CommandId
    if (-not $commandId) { throw "API development verification returned no SSM command id." }

    $invocation = $null
    for ($elapsed = 0; $elapsed -lt $TimeoutSec; $elapsed += 5) {
        Start-Sleep -Seconds 5
        $invocation = Invoke-AwsJson @(
            "ssm", "get-command-invocation",
            "--command-id", $commandId,
            "--instance-id", $instanceId,
            "--region", $script:Region,
            "--output", "json"
        )
        if ([string]$invocation.Status -eq "Success") { break }
        if ([string]$invocation.Status -in @("Failed","Cancelled","TimedOut","Cancelling")) { break }
    }
    if (
        [string]$invocation.Status -ne "Success" -or
        [string]$invocation.StandardOutputContent -notmatch "DEVELOPMENT_RUNTIME_PASS"
    ) {
        $stderr = ([string]$invocation.StandardErrorContent).Trim()
        throw "API development candidate verification failed: status=$($invocation.Status) stderr=$stderr"
    }

    & (Join-Path $ScriptRoot "run-api-development-smoke.ps1") `
        -InstanceId $instanceId `
        -TimeoutSec ([Math]::Min($TimeoutSec, 600)) `
        -Ci:$Ci `
        -AwsProfile $AwsProfile

    Invoke-Aws @(
        "ec2", "create-tags",
        "--resources", $instanceId,
        "--tags",
        "Key=Lifecycle,Value=active",
        "Key=VerifiedReleaseId,Value=$ExpectedReleaseId",
        "--region", $script:Region
    ) -ErrorMessage "promote API development candidate tags" | Out-Null
    Invoke-Aws @(
        "ec2", "modify-instance-attribute",
        "--instance-id", $instanceId,
        "--disable-api-termination", "Value=true",
        "--region", $script:Region
    ) -ErrorMessage "enable API development termination protection" | Out-Null
    $promoted = $true

    foreach ($oldId in $oldInstanceIds) {
        if ($oldId -eq $instanceId) { continue }
        Invoke-Aws @(
            "ec2", "create-tags",
            "--resources", $oldId,
            "--tags", "Key=Lifecycle,Value=retired",
            "--region", $script:Region
        ) -ErrorMessage "retire prior API development instance tags" | Out-Null
        Invoke-Aws @(
            "ec2", "modify-instance-attribute",
            "--instance-id", $oldId,
            "--disable-api-termination", "Value=false",
            "--region", $script:Region
        ) -ErrorMessage "disable prior API development termination protection" | Out-Null
        Invoke-Aws @(
            "ec2", "terminate-instances",
            "--instance-ids", $oldId,
            "--region", $script:Region
        ) -ErrorMessage "terminate prior API development instance" | Out-Null
    }
    if ($oldInstanceIds.Count -gt 0) {
        Invoke-Aws @(
            "ec2", "wait", "instance-terminated",
            "--instance-ids", $oldInstanceIds,
            "--region", $script:Region
        ) -ErrorMessage "wait for prior API development termination" | Out-Null
    }
    Write-Host (
        "API_DEVELOPMENT_DEPLOY_PASS instance={0} release={1} api={2} tools={3} ai={4}" -f
        $instanceId,
        $ExpectedReleaseId,
        $ApiImageUri,
        $ToolsImageUri,
        $AiImageUri
    ) -ForegroundColor Green
} finally {
    Remove-TempFiles @($networkFile)
    if ($instanceId -and -not $promoted) {
        try {
            Invoke-Aws @(
                "ec2", "terminate-instances",
                "--instance-ids", $instanceId,
                "--region", $script:Region
            ) -ErrorMessage "terminate failed API development candidate" | Out-Null
            Invoke-Aws @(
                "ec2", "wait", "instance-terminated",
                "--instance-ids", $instanceId,
                "--region", $script:Region
            ) -ErrorMessage "wait for failed API development candidate termination" | Out-Null
        } catch {
            throw "API development candidate cleanup failed for $instanceId. $($_.Exception.Message)"
        }
    }
}
