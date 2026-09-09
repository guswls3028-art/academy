# IAM: Batch roles + instance profile. Uses v1/templates/iam.
# AWS/Cloudflare credentials are supplied by the caller through the intended profile or process environment; this script does not load backend/.env.
$ErrorActionPreference = "Stop"
$IamDir = $PSScriptRoot
$V4Root = (Resolve-Path (Join-Path $IamDir "..")).Path
$TemplatesPath = Join-Path $V4Root "templates\iam"

$BatchServiceRoleName = "academy-batch-service-role"
$EcsInstanceRoleName = "academy-batch-ecs-instance-role"
$InstanceProfileName = "academy-batch-ecs-instance-profile"
$JobRoleName = "academy-video-batch-job-role"
$ExecutionRoleName = "academy-batch-ecs-task-execution-role"
$ApiPreprodCanaryRoleName = "academy-api-preprod-canary-role"
$ApiPreprodCanaryProfileName = "academy-api-preprod-canary"
$ApiPreprodCanaryPolicyName = "academy-api-preprod-canary-runtime"
$ApiDevelopmentPolicyName = "academy-api-development-runtime"

function Get-ASGInstanceRefreshResourceArn {
    param([string]$AutoScalingGroupName)
    if (-not $AutoScalingGroupName -or $AutoScalingGroupName.Trim() -eq "") { return $null }
    return "arn:aws:autoscaling:$($script:Region):$($script:AccountId):autoScalingGroup:*:autoScalingGroupName/$AutoScalingGroupName"
}

function Ensure-ApiPreprodCanaryIAM {
    if ($script:PlanMode) { return $ApiPreprodCanaryProfileName }
    Write-Step "Ensure isolated API preprod canary IAM"

    $trustPath = Join-Path $TemplatesPath "trust_ec2.json"
    if (-not (Test-Path -LiteralPath $trustPath)) { throw "EC2 trust policy template not found: $trustPath" }
    $trustRef = "file://$($trustPath -replace '\\','/')"
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $ApiPreprodCanaryRoleName, "--output", "json")
    if (-not $role) {
        Invoke-Aws @(
            "iam", "create-role",
            "--role-name", $ApiPreprodCanaryRoleName,
            "--assume-role-policy-document", $trustRef
        ) -ErrorMessage "create API preprod canary role" | Out-Null
        $script:ChangesMade = $true
    } else {
        Invoke-Aws @(
            "iam", "update-assume-role-policy",
            "--role-name", $ApiPreprodCanaryRoleName,
            "--policy-document", $trustRef
        ) -ErrorMessage "update API preprod canary trust policy" | Out-Null
    }

    Invoke-Aws @(
        "iam", "attach-role-policy",
        "--role-name", $ApiPreprodCanaryRoleName,
        "--policy-arn", "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    ) -ErrorMessage "attach SSM core to API preprod canary role" | Out-Null

    $runtimePolicy = [ordered]@{
        Version = "2012-10-17"
        Statement = @(
            [ordered]@{Sid="EcrAuth";Effect="Allow";Action="ecr:GetAuthorizationToken";Resource="*"},
            [ordered]@{
                Sid = "ApiImagePull"
                Effect = "Allow"
                Action = @("ecr:BatchCheckLayerAvailability","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer")
                Resource = "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$($script:EcrApiRepo)"
            },
            [ordered]@{
                Sid = "ApiPreprodEnvRead"
                Effect = "Allow"
                Action = "ssm:GetParameter"
                Resource = "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/preprod/env"
            }
        )
    }
    $runtimePolicyJson = $runtimePolicy | ConvertTo-Json -Depth 20 -Compress
    $runtimePolicyRef = Convert-JsonArgToFileRef $runtimePolicyJson
    $runtimePolicyFile = $runtimePolicyRef -replace '^file://', ''
    try {
        Invoke-Aws @(
            "iam", "put-role-policy",
            "--role-name", $ApiPreprodCanaryRoleName,
            "--policy-name", $ApiPreprodCanaryPolicyName,
            "--policy-document", $runtimePolicyRef
        ) -ErrorMessage "put API preprod canary runtime policy" | Out-Null
    } finally {
        Remove-TempFiles @($runtimePolicyFile)
    }

    $profile = Invoke-AwsJson @(
        "iam", "get-instance-profile",
        "--instance-profile-name", $ApiPreprodCanaryProfileName,
        "--output", "json"
    )
    if (-not $profile) {
        Invoke-Aws @(
            "iam", "create-instance-profile",
            "--instance-profile-name", $ApiPreprodCanaryProfileName
        ) -ErrorMessage "create API preprod canary instance profile" | Out-Null
        Invoke-Aws @(
            "iam", "add-role-to-instance-profile",
            "--instance-profile-name", $ApiPreprodCanaryProfileName,
            "--role-name", $ApiPreprodCanaryRoleName
        ) -ErrorMessage "attach API preprod canary role to profile" | Out-Null
        $script:ChangesMade = $true
    } else {
        $roles = @($profile.InstanceProfile.Roles)
        $unexpected = @($roles | Where-Object { $_.RoleName -ne $ApiPreprodCanaryRoleName })
        if ($unexpected.Count -gt 0) { throw "API preprod canary profile contains an unexpected role." }
        if (-not ($roles | Where-Object { $_.RoleName -eq $ApiPreprodCanaryRoleName })) {
            Invoke-Aws @(
                "iam", "add-role-to-instance-profile",
                "--instance-profile-name", $ApiPreprodCanaryProfileName,
                "--role-name", $ApiPreprodCanaryRoleName
            ) -ErrorMessage "attach API preprod canary role to profile" | Out-Null
            $script:ChangesMade = $true
        }
    }

    $readback = Invoke-AwsJson @(
        "iam", "get-role-policy",
        "--role-name", $ApiPreprodCanaryRoleName,
        "--policy-name", $ApiPreprodCanaryPolicyName,
        "--output", "json"
    )
    $actualJson = $readback.PolicyDocument | ConvertTo-Json -Depth 20 -Compress
    if ($actualJson -ne $runtimePolicyJson) { throw "API preprod canary IAM readback mismatch." }
    $attached = Invoke-AwsJson @(
        "iam", "list-attached-role-policies",
        "--role-name", $ApiPreprodCanaryRoleName,
        "--output", "json"
    )
    $attachedArns = @($attached.AttachedPolicies | ForEach-Object { [string]$_.PolicyArn } | Sort-Object -Unique)
    if (
        $attachedArns.Count -ne 1 -or
        $attachedArns[0] -ne "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    ) {
        throw "API preprod canary role has unexpected managed policies."
    }
    $inline = Invoke-AwsJson @(
        "iam", "list-role-policies",
        "--role-name", $ApiPreprodCanaryRoleName,
        "--output", "json"
    )
    $inlineNames = @($inline.PolicyNames | Sort-Object -Unique)
    if ($inlineNames.Count -ne 1 -or $inlineNames[0] -ne $ApiPreprodCanaryPolicyName) {
        throw "API preprod canary role has unexpected inline policies."
    }
    Write-Ok "API preprod canary IAM is dedicated and least-privilege."
    return $ApiPreprodCanaryProfileName
}

function Ensure-ApiDevelopmentIAM {
    if ($script:PlanMode) { return $script:ApiDevelopmentInstanceProfileName }
    if (
        $script:ApiDevelopmentRoleName -cne "academy-api-development-role" -or
        $script:ApiDevelopmentInstanceProfileName -cne "academy-api-development" -or
        $script:AccountId -cne "809466760795" -or $script:Region -cne "ap-northeast-2" -or
        $script:DynamoLockTableName -cne "academy-v1-video-job-lock" -or
        ($env:AWS_REGION -and $env:AWS_REGION -cne $script:Region)
    ) {
        throw "Development IAM target and shared lock coordinates must match exactly."
    }
    Assert-DeployLockAcquired -Reg $script:Region
    Write-Step "Ensure isolated persistent API development IAM"

    $roleName = [string]$script:ApiDevelopmentRoleName
    $profileName = [string]$script:ApiDevelopmentInstanceProfileName
    if (-not $roleName -or -not $profileName) {
        throw "API development role/profile names are missing from SSOT."
    }
    $trustPath = Join-Path $TemplatesPath "trust_ec2.json"
    if (-not (Test-Path -LiteralPath $trustPath)) {
        throw "EC2 trust policy template not found: $trustPath"
    }
    $trustRef = "file://$($trustPath -replace '\\','/')"
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $roleName, "--output", "json")
    if (-not $role) {
        Assert-DeployLockAcquired -Reg $script:Region
        $script:ApiDevelopmentIAMWriteAttempted = $true
        Invoke-Aws @(
            "iam", "create-role",
            "--role-name", $roleName,
            "--assume-role-policy-document", $trustRef
        ) -ErrorMessage "create API development role" | Out-Null
        $script:ChangesMade = $true
    } else {
        Assert-DeployLockAcquired -Reg $script:Region
        $script:ApiDevelopmentIAMWriteAttempted = $true
        Invoke-Aws @(
            "iam", "update-assume-role-policy",
            "--role-name", $roleName,
            "--policy-document", $trustRef
        ) -ErrorMessage "update API development trust policy" | Out-Null
    }

    Assert-DeployLockAcquired -Reg $script:Region
    $script:ApiDevelopmentIAMWriteAttempted = $true
    Invoke-Aws @(
        "iam", "attach-role-policy",
        "--role-name", $roleName,
        "--policy-arn", "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    ) -ErrorMessage "attach SSM core to API development role" | Out-Null

    $runtimePolicy = [ordered]@{
        Version = "2012-10-17"
        Statement = @(
            [ordered]@{Sid="EcrAuth";Effect="Allow";Action="ecr:GetAuthorizationToken";Resource="*"},
            [ordered]@{
                Sid = "DevelopmentImagePull"
                Effect = "Allow"
                Action = @("ecr:BatchCheckLayerAvailability","ecr:BatchGetImage","ecr:GetDownloadUrlForLayer")
                Resource = @(
                    "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$($script:EcrApiRepo)",
                    "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$($script:EcrAiRepo)",
                    "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$($script:EcrMessagingRepo)",
                    "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$($script:EcrToolsRepo)"
                )
            },
            [ordered]@{
                Sid = "DevelopmentEnvRead"
                Effect = "Allow"
                Action = "ssm:GetParameter"
                Resource = @(
                    "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/development/env",
                    "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/workers/development/env"
                )
            },
            [ordered]@{
                Sid = "DevelopmentQueueAccess"
                Effect = "Allow"
                Action = @(
                    "sqs:GetQueueUrl",
                    "sqs:GetQueueAttributes",
                    "sqs:SendMessage",
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:ChangeMessageVisibility"
                )
                Resource = @(
                    "arn:aws:sqs:$($script:Region):$($script:AccountId):$($script:ApiDevelopmentAiQueueName)",
                    "arn:aws:sqs:$($script:Region):$($script:AccountId):$($script:ApiDevelopmentToolsQueueName)",
                    "arn:aws:sqs:$($script:Region):$($script:AccountId):$($script:ApiDevelopmentMessagingQueueName)"
                )
            }
        )
    }
    # SSM ManagedInstanceCore also allows parameter reads on Resource=*. An
    # allow-only development policy does not constrain that managed grant.
    $parameterBoundaryPath = Join-Path $TemplatesPath "policy_api_development_parameter_boundary.json"
    $parameterBoundaryJson = (Get-Content -LiteralPath $parameterBoundaryPath -Raw).
        Replace("__REGION__", $script:Region).Replace("__ACCOUNT_ID__", $script:AccountId)
    $runtimePolicy.Statement += @(($parameterBoundaryJson | ConvertFrom-Json).Statement)
    $runtimePolicyJson = $runtimePolicy | ConvertTo-Json -Depth 20 -Compress
    $runtimePolicyRef = Convert-JsonArgToFileRef $runtimePolicyJson
    $runtimePolicyFile = $runtimePolicyRef -replace '^file://', ''
    try {
        Assert-DeployLockAcquired -Reg $script:Region
        $script:ApiDevelopmentIAMWriteAttempted = $true
        Invoke-Aws @(
            "iam", "put-role-policy",
            "--role-name", $roleName,
            "--policy-name", $ApiDevelopmentPolicyName,
            "--policy-document", $runtimePolicyRef
        ) -ErrorMessage "put API development runtime policy" | Out-Null
    } finally {
        Remove-TempFiles @($runtimePolicyFile)
    }

    $profile = Invoke-AwsJson @(
        "iam", "get-instance-profile",
        "--instance-profile-name", $profileName,
        "--output", "json"
    )
    if (-not $profile) {
        Assert-DeployLockAcquired -Reg $script:Region
        $script:ApiDevelopmentIAMWriteAttempted = $true
        Invoke-Aws @(
            "iam", "create-instance-profile",
            "--instance-profile-name", $profileName
        ) -ErrorMessage "create API development instance profile" | Out-Null
        Assert-DeployLockAcquired -Reg $script:Region
        $script:ApiDevelopmentIAMWriteAttempted = $true
        Invoke-Aws @(
            "iam", "add-role-to-instance-profile",
            "--instance-profile-name", $profileName,
            "--role-name", $roleName
        ) -ErrorMessage "attach API development role to profile" | Out-Null
        $script:ChangesMade = $true
    } else {
        $roles = @($profile.InstanceProfile.Roles)
        $unexpected = @($roles | Where-Object { $_.RoleName -ne $roleName })
        if ($unexpected.Count -gt 0) {
            throw "API development profile contains an unexpected role."
        }
        if (-not ($roles | Where-Object { $_.RoleName -eq $roleName })) {
            Assert-DeployLockAcquired -Reg $script:Region
            $script:ApiDevelopmentIAMWriteAttempted = $true
            Invoke-Aws @(
                "iam", "add-role-to-instance-profile",
                "--instance-profile-name", $profileName,
                "--role-name", $roleName
            ) -ErrorMessage "attach API development role to profile" | Out-Null
            $script:ChangesMade = $true
        }
    }

    Assert-DeployLockAcquired -Reg $script:Region
    $readback = Invoke-AwsJson @(
        "iam", "get-role-policy",
        "--role-name", $roleName,
        "--policy-name", $ApiDevelopmentPolicyName,
        "--output", "json"
    )
    $actualJson = $readback.PolicyDocument | ConvertTo-Json -Depth 20 -Compress
    if ($actualJson -ne $runtimePolicyJson) {
        throw "API development IAM readback mismatch."
    }
    $attached = Invoke-AwsJson @(
        "iam", "list-attached-role-policies",
        "--role-name", $roleName,
        "--output", "json"
    )
    $attachedArns = @($attached.AttachedPolicies | ForEach-Object { [string]$_.PolicyArn } | Sort-Object -Unique)
    if (
        $attachedArns.Count -ne 1 -or
        $attachedArns[0] -ne "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    ) {
        throw "API development role has unexpected managed policies."
    }
    $inline = Invoke-AwsJson @(
        "iam", "list-role-policies",
        "--role-name", $roleName,
        "--output", "json"
    )
    $inlineNames = @($inline.PolicyNames | Sort-Object -Unique)
    if ($inlineNames.Count -ne 1 -or $inlineNames[0] -ne $ApiDevelopmentPolicyName) {
        throw "API development role has unexpected inline policies."
    }
    Assert-DeployLockAcquired -Reg $script:Region
    Write-Ok "API development IAM is dedicated and least-privilege."
    return $profileName
}

function Legacy-GitHubActionsDeployIAM {
    if ($script:PlanMode) { return }
    $roleName = if ($script:GitHubActionsDeployRoleName) { $script:GitHubActionsDeployRoleName } else { "academy-gha-ecr-build" }
    $policyName = if ($script:GitHubActionsDeployPolicyName) { $script:GitHubActionsDeployPolicyName } else { "EcrBuildPush" }
    if (-not $roleName -or -not $policyName) { return }

    Write-Step "Ensure GitHub Actions deploy IAM"
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $roleName, "--output", "json")
    if (-not $role) {
        throw "GitHub Actions role $roleName not found or not readable. Refusing to skip deploy IAM convergence."
    }

    $policy = Invoke-AwsJson @("iam", "get-role-policy", "--role-name", $roleName, "--policy-name", $policyName, "--output", "json")
    if (-not $policy -or -not $policy.PolicyDocument) {
        throw "Inline policy $policyName on $roleName not found or not readable. Refusing to skip deploy IAM convergence."
    }

    $doc = $policy.PolicyDocument
    $statements = [System.Collections.ArrayList]::new()
    foreach ($item in @($doc.Statement)) { [void]$statements.Add($item) }
    $changed = $false

    # A previously hand-edited policy can contain duplicate managed Sids. If
    # duplicates are left in place, updating the first match only makes the
    # readback fail without actually converging the policy. Collapse each
    # managed Sid to one statement before assigning its exact contract below.
    $managedSids = @(
        "AsgInstanceRefresh",
        "AsgDescribe",
        "LaunchTemplateImagePinRead",
        "LaunchTemplateImagePinWrite",
        "EcrPushPull",
        "EcrRepoManage"
    )
    foreach ($sid in $managedSids) {
        $duplicates = @($statements | Where-Object { $_.Sid -eq $sid })
        if ($duplicates.Count -gt 1) {
            foreach ($duplicate in @($duplicates | Select-Object -Skip 1)) {
                [void]$statements.Remove($duplicate)
            }
            $changed = $true
        }
    }

    $requiredAsgArns = @(
        $script:ApiASGName,
        $script:MessagingASGName,
        $script:AiASGName,
        $script:ToolsASGName
    ) | Where-Object { $_ -and $_.Trim() -ne "" } | Sort-Object -Unique | ForEach-Object { Get-ASGInstanceRefreshResourceArn $_ }
    if ($requiredAsgArns.Count -ne 4) { throw "Expected exactly four SSOT ASGs for deploy IAM; actual=$($requiredAsgArns.Count)" }
    $asgActions = @("autoscaling:SetInstanceProtection", "autoscaling:StartInstanceRefresh", "autoscaling:UpdateAutoScalingGroup")
    $asgStatement = @($statements) | Where-Object { $_.Sid -eq "AsgInstanceRefresh" } | Select-Object -First 1
    if (-not $asgStatement) {
        $asgStatement = [PSCustomObject]@{ Sid="AsgInstanceRefresh"; Effect="Allow"; Action=$asgActions; Resource=$requiredAsgArns }
        [void]$statements.Add($asgStatement)
        $changed = $true
    } elseif (
        (@($asgStatement.Action | Sort-Object) -join "`n") -ne (@($asgActions | Sort-Object) -join "`n") -or
        (@($asgStatement.Resource | Sort-Object) -join "`n") -ne (@($requiredAsgArns | Sort-Object) -join "`n") -or
        [string]$asgStatement.Effect -ne "Allow"
    ) {
        $asgStatement.Action = $asgActions
        $asgStatement.Resource = $requiredAsgArns
        $asgStatement.Effect = "Allow"
        $changed = $true
    }

    # CI creates only a new version of the four existing SSOT Launch Templates.
    # It does not modify the default version or repoint an ASG; pin-asg-image.ps1
    # requires each ASG to already track $Latest and fails closed otherwise.
    $ltNames = @(
        $script:ApiLaunchTemplateName,
        $script:MessagingLaunchTemplateName,
        $script:AiLaunchTemplateName,
        $script:ToolsLaunchTemplateName
    ) | Where-Object { $_ -and $_.Trim() -ne "" } | Sort-Object -Unique
    $ltArns = @()
    if ($ltNames.Count -gt 0) {
        $ltArgs = @("ec2", "describe-launch-templates", "--launch-template-names") + [string[]]$ltNames + @("--region", $script:Region, "--output", "json")
        $ltResult = Invoke-AwsJson $ltArgs
        $ltArns = @($ltResult.LaunchTemplates | Where-Object { $_.LaunchTemplateId } | ForEach-Object {
            "arn:aws:ec2:$($script:Region):$($script:AccountId):launch-template/$($_.LaunchTemplateId)"
        } | Sort-Object -Unique)
    }

    $asgReadActions = @("autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeInstanceRefreshes")
    $asgReadStatement = @($statements) | Where-Object { $_.Sid -eq "AsgDescribe" } | Select-Object -First 1
    if (-not $asgReadStatement) {
        [void]$statements.Add([PSCustomObject]@{ Sid="AsgDescribe"; Effect="Allow"; Action=$asgReadActions; Resource="*" })
        $changed = $true
    } elseif (
        (@($asgReadStatement.Action | Sort-Object) -join "`n") -ne (@($asgReadActions | Sort-Object) -join "`n") -or
        [string]$asgReadStatement.Resource -ne "*" -or
        [string]$asgReadStatement.Effect -ne "Allow"
    ) {
        $asgReadStatement.Action = $asgReadActions
        $asgReadStatement.Resource = "*"
        $asgReadStatement.Effect = "Allow"
        $changed = $true
    }

    $readStatement = @($statements) | Where-Object { $_.Sid -eq "LaunchTemplateImagePinRead" } | Select-Object -First 1
    $readActions = @("ec2:DescribeLaunchTemplates", "ec2:DescribeLaunchTemplateVersions")
    if (-not $readStatement) {
        [void]$statements.Add([PSCustomObject]@{
            Sid = "LaunchTemplateImagePinRead"
            Effect = "Allow"
            Action = $readActions
            Resource = "*"
        })
        $changed = $true
    } else {
        if (
            (@($readStatement.Action | Sort-Object) -join "`n") -ne (@($readActions | Sort-Object) -join "`n") -or
            [string]$readStatement.Resource -ne "*" -or
            [string]$readStatement.Effect -ne "Allow"
        ) {
            $readStatement.Action = $readActions
            $readStatement.Resource = "*"
            $readStatement.Effect = "Allow"
            $changed = $true
        }
    }

    $writeStatement = @($statements) | Where-Object { $_.Sid -eq "LaunchTemplateImagePinWrite" } | Select-Object -First 1
    if ($ltArns.Count -eq $ltNames.Count -and $ltArns.Count -gt 0) {
        if (-not $writeStatement) {
            [void]$statements.Add([PSCustomObject]@{
                Sid = "LaunchTemplateImagePinWrite"
                Effect = "Allow"
                Action = "ec2:CreateLaunchTemplateVersion"
                Resource = $ltArns
            })
            $changed = $true
        } else {
            if (
                (@($writeStatement.Action | Sort-Object) -join "`n") -ne "ec2:CreateLaunchTemplateVersion" -or
                (@($writeStatement.Resource | Sort-Object) -join "`n") -ne (@($ltArns | Sort-Object) -join "`n") -or
                [string]$writeStatement.Effect -ne "Allow"
            ) {
                $writeStatement.Action = "ec2:CreateLaunchTemplateVersion"
                $writeStatement.Resource = $ltArns
                $writeStatement.Effect = "Allow"
                $changed = $true
            }
        }
    } else {
        throw "Launch Template image-pin IAM cannot converge until all SSOT Launch Templates exist ($($ltArns.Count)/$($ltNames.Count))."
    }

    $ecrRepoArns = @($script:SSOT_ECR | Where-Object { $_ } | Sort-Object -Unique | ForEach-Object {
        "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$_"
    })
    if ($ecrRepoArns.Count -ne 6) { throw "Expected exactly six SSOT ECR repositories; actual=$($ecrRepoArns.Count)" }
    $ecrManaged = @(
        @{
            Sid="EcrPushPull"
            Action=@("ecr:BatchCheckLayerAvailability", "ecr:BatchDeleteImage", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart")
        },
        @{
            Sid="EcrRepoManage"
            Action=@("ecr:CreateRepository", "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:ListImages", "ecr:PutImageTagMutability")
        }
    )
    foreach ($expected in $ecrManaged) {
        $statement = @($statements) | Where-Object { $_.Sid -eq $expected.Sid } | Select-Object -First 1
        if (-not $statement) {
            [void]$statements.Add([PSCustomObject]@{ Sid=$expected.Sid; Effect="Allow"; Action=$expected.Action; Resource=$ecrRepoArns })
            $changed = $true
        } elseif (
            (@($statement.Action | Sort-Object) -join "`n") -ne (@($expected.Action | Sort-Object) -join "`n") -or
            (@($statement.Resource | Sort-Object) -join "`n") -ne (@($ecrRepoArns | Sort-Object) -join "`n") -or
            [string]$statement.Effect -ne "Allow"
        ) {
            $statement.Action = $expected.Action
            $statement.Resource = $ecrRepoArns
            $statement.Effect = "Allow"
            $changed = $true
        }
    }

    if ($changed) {
        $doc.Statement = @($statements)
        $json = $doc | ConvertTo-Json -Depth 20
        $policyFileRef = Convert-JsonArgToFileRef $json
        $policyFile = $policyFileRef -replace '^file://', ''
        try {
            Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $policyName, "--policy-document", $policyFileRef) -ErrorMessage "put GitHub Actions deploy IAM policy" | Out-Null
        } finally {
            Remove-TempFiles @($policyFile)
        }
        $script:ChangesMade = $true
    }

    # Read back the live policy and prove that every write-capable managed Sid
    # is exact. Additive checks can leave stale wildcard resources behind.
    $verifiedPolicy = Invoke-AwsJson @("iam", "get-role-policy", "--role-name", $roleName, "--policy-name", $policyName, "--output", "json")
    $verifiedStatements = @($verifiedPolicy.PolicyDocument.Statement)
    $expectedManaged = @(
        @{Sid="AsgInstanceRefresh"; Action=$asgActions; Resource=$requiredAsgArns},
        @{Sid="AsgDescribe"; Action=$asgReadActions; Resource=@("*")},
        @{Sid="LaunchTemplateImagePinRead"; Action=$readActions; Resource=@("*")},
        @{Sid="LaunchTemplateImagePinWrite"; Action=@("ec2:CreateLaunchTemplateVersion"); Resource=$ltArns}
    ) + @($ecrManaged | ForEach-Object { @{Sid=$_.Sid; Action=$_.Action; Resource=$ecrRepoArns} })
    foreach ($expected in $expectedManaged) {
        $matches = @($verifiedStatements | Where-Object { $_.Sid -eq $expected.Sid })
        if ($matches.Count -ne 1) { throw "IAM readback expected one $($expected.Sid) statement; actual=$($matches.Count)" }
        $actual = $matches[0]
        if (
            [string]$actual.Effect -ne "Allow" -or
            (@($actual.Action | Sort-Object) -join "`n") -ne (@($expected.Action | Sort-Object) -join "`n") -or
            (@($actual.Resource | Sort-Object) -join "`n") -ne (@($expected.Resource | Sort-Object) -join "`n")
        ) {
            throw "IAM readback mismatch for managed statement $($expected.Sid)"
        }
    }
    Write-Ok "GitHub Actions deploy IAM converged and read back with exact SSOT resources"
}

function Ensure-GitHubActionsDeployIAM {
    if ($script:PlanMode) { return }
    $roleName = if ($script:GitHubActionsDeployRoleName) { $script:GitHubActionsDeployRoleName } else { "academy-gha-ecr-build" }
    $policyName = if ($script:GitHubActionsDeployPolicyName) { $script:GitHubActionsDeployPolicyName } else { "EcrBuildPush" }
    Write-Step "Converge exact GitHub Actions least-privilege policy"
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $roleName, "--output", "json")
    if (-not $role.Role.Arn) { throw "GitHub Actions role not found: $roleName" }

    # Jobs without an environment use the main-ref subject. Jobs protected by
    # the GitHub production environment use the environment subject instead.
    # Keep the trust inventory exact so PRs, tags, and other environments fail
    # closed while both halves of the release workflow can use OIDC.
    $expectedSubjects = @(
        "repo:guswls3028-art/academy-backend:environment:production",
        "repo:guswls3028-art/academy-backend:ref:refs/heads/main"
    ) | Sort-Object
    $expectedTrust = [ordered]@{
        Version = "2012-10-17"
        Statement = @(
            [ordered]@{
                Effect = "Allow"
                Principal = [ordered]@{
                    Federated = (
                        "arn:aws:iam::$($script:AccountId):oidc-provider/" +
                        "token.actions.githubusercontent.com"
                    )
                }
                Action = "sts:AssumeRoleWithWebIdentity"
                Condition = [ordered]@{
                    StringEquals = [ordered]@{
                        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
                        "token.actions.githubusercontent.com:sub" = $expectedSubjects
                    }
                }
            }
        )
    }
    $expectedTrustJson = $expectedTrust | ConvertTo-Json -Depth 20 -Compress
    $actualTrustJson = $role.Role.AssumeRolePolicyDocument |
        ConvertTo-Json -Depth 20 -Compress
    if ($actualTrustJson -ne $expectedTrustJson) {
        $trustRef = Convert-JsonArgToFileRef $expectedTrustJson
        $trustFile = $trustRef -replace '^file://', ''
        try {
            Invoke-Aws @(
                "iam", "update-assume-role-policy",
                "--role-name", $roleName,
                "--policy-document", $trustRef
            ) -ErrorMessage "update GitHub Actions OIDC trust" | Out-Null
        } finally {
            Remove-TempFiles @($trustFile)
        }
        $script:ChangesMade = $true
    }

    $trustReadback = Invoke-AwsJson @(
        "iam", "get-role", "--role-name", $roleName, "--output", "json"
    )
    $trustStatements = @($trustReadback.Role.AssumeRolePolicyDocument.Statement)
    $trustCondition = $(if ($trustStatements.Count -eq 1) {
        $trustStatements[0].Condition.StringEquals
    } else {
        $null
    })
    $actualSubjects = @(
        $trustCondition.'token.actions.githubusercontent.com:sub'
    ) | Sort-Object
    if (
        $trustStatements.Count -ne 1 -or
        [string]$trustStatements[0].Effect -ne "Allow" -or
        [string]$trustStatements[0].Action -ne "sts:AssumeRoleWithWebIdentity" -or
        [string]$trustStatements[0].Principal.Federated -ne
            $expectedTrust.Statement[0].Principal.Federated -or
        [string]$trustCondition.'token.actions.githubusercontent.com:aud' -ne
            "sts.amazonaws.com" -or
        ($actualSubjects -join "`n") -cne ($expectedSubjects -join "`n")
    ) {
        throw "GitHub Actions OIDC trust readback mismatch."
    }

    $currentPolicy = $null
    try { $currentPolicy = Invoke-AwsJson @("iam", "get-role-policy", "--role-name", $roleName, "--policy-name", $policyName, "--output", "json") }
    catch { if ($_.Exception.Message -notmatch "NoSuchEntity") { throw } }

    $repos = @($script:SSOT_ECR | Where-Object { $_ } | Sort-Object -Unique)
    if ($repos.Count -ne 6) { throw "Expected exactly six SSOT ECR repositories; actual=$($repos.Count)" }
    $repoArns = @($repos | ForEach-Object { "arn:aws:ecr:$($script:Region):$($script:AccountId):repository/$_" })
    $asgNames = @($script:ApiASGName, $script:MessagingASGName, $script:AiASGName, $script:ToolsASGName | Where-Object { $_ } | Sort-Object -Unique)
    if ($asgNames.Count -ne 4) { throw "Expected exactly four SSOT ASGs; actual=$($asgNames.Count)" }
    $asgArns = @($asgNames | ForEach-Object { Get-ASGInstanceRefreshResourceArn $_ })
    $ltNames = @($script:ApiLaunchTemplateName, $script:MessagingLaunchTemplateName, $script:AiLaunchTemplateName, $script:ToolsLaunchTemplateName | Where-Object { $_ } | Sort-Object -Unique)
    $ltArgs = @("ec2", "describe-launch-templates", "--launch-template-names") + [string[]]$ltNames + @("--region", $script:Region, "--output", "json")
    $ltResult = Invoke-AwsJson $ltArgs
    $ltArns = @($ltResult.LaunchTemplates | ForEach-Object { "arn:aws:ec2:$($script:Region):$($script:AccountId):launch-template/$($_.LaunchTemplateId)" } | Sort-Object -Unique)
    if ($ltArns.Count -ne 4) { throw "All four SSOT Launch Templates must exist before IAM convergence." }

    # Updating an ASG to a Launch Template version triggers an EC2 RunInstances
    # dry-run authorization check. Derive the smallest complete resource set
    # from the four existing SSOT ASGs and their latest template versions rather
    # than granting RunInstances or PassRole account-wide.
    $runtimeImageIds = [System.Collections.ArrayList]::new()
    $runtimeSecurityGroupIds = [System.Collections.ArrayList]::new()
    $runtimeSubnetIds = [System.Collections.ArrayList]::new()
    $runtimeInstanceRoleArns = [System.Collections.ArrayList]::new()
    $templatesRequireInstanceTags = $false
    foreach ($asgName in $asgNames) {
        $asgResult = Invoke-AwsJson @("autoscaling", "describe-auto-scaling-groups", "--auto-scaling-group-names", $asgName, "--region", $script:Region, "--output", "json")
        $matchingAsgs = @($asgResult.AutoScalingGroups)
        if ($matchingAsgs.Count -ne 1) { throw "Expected exactly one ASG named $asgName while deriving Launch Template use IAM." }
        $asg = $matchingAsgs[0]
        $launchTemplateId = [string]$asg.LaunchTemplate.LaunchTemplateId
        if (-not $launchTemplateId -or "arn:aws:ec2:$($script:Region):$($script:AccountId):launch-template/$launchTemplateId" -notin $ltArns) {
            throw "ASG $asgName does not reference one of the four SSOT Launch Templates."
        }
        foreach ($subnetId in @(([string]$asg.VPCZoneIdentifier -split ",") | Where-Object { $_ -and $_.Trim() })) {
            [void]$runtimeSubnetIds.Add($subnetId.Trim())
        }

        $versionResult = Invoke-AwsJson @("ec2", "describe-launch-template-versions", "--launch-template-id", $launchTemplateId, "--versions", '$Latest', "--region", $script:Region, "--output", "json")
        $versions = @($versionResult.LaunchTemplateVersions)
        if ($versions.Count -ne 1) { throw "Launch Template $launchTemplateId must expose exactly one latest version." }
        $templateData = $versions[0].LaunchTemplateData
        if (-not $templateData.ImageId) { throw "Launch Template $launchTemplateId has no AMI." }
        [void]$runtimeImageIds.Add([string]$templateData.ImageId)
        foreach ($securityGroupId in @($templateData.SecurityGroupIds | Where-Object { $_ })) {
            [void]$runtimeSecurityGroupIds.Add([string]$securityGroupId)
        }
        if (@($templateData.TagSpecifications).Count -gt 0) { $templatesRequireInstanceTags = $true }

        $instanceProfileName = [string]$templateData.IamInstanceProfile.Name
        if (-not $instanceProfileName -and $templateData.IamInstanceProfile.Arn) {
            $instanceProfileName = ([string]$templateData.IamInstanceProfile.Arn -split "/")[-1]
        }
        if (-not $instanceProfileName) { throw "Launch Template $launchTemplateId has no instance profile." }
        $profileResult = Invoke-AwsJson @("iam", "get-instance-profile", "--instance-profile-name", $instanceProfileName, "--output", "json")
        $profileRoles = @($profileResult.InstanceProfile.Roles | Where-Object { $_.Arn })
        if ($profileRoles.Count -ne 1) { throw "Instance profile $instanceProfileName must contain exactly one role." }
        [void]$runtimeInstanceRoleArns.Add([string]$profileRoles[0].Arn)
    }
    $runtimeImageIds = @($runtimeImageIds | Sort-Object -Unique)
    $runtimeSecurityGroupIds = @($runtimeSecurityGroupIds | Sort-Object -Unique)
    $runtimeSubnetIds = @($runtimeSubnetIds | Sort-Object -Unique)
    [void]$runtimeInstanceRoleArns.Add("arn:aws:iam::$($script:AccountId):role/$ApiPreprodCanaryRoleName")
    $runtimeInstanceRoleArns = @($runtimeInstanceRoleArns | Sort-Object -Unique)
    if ($runtimeImageIds.Count -eq 0 -or $runtimeSecurityGroupIds.Count -eq 0 -or $runtimeSubnetIds.Count -eq 0 -or $runtimeInstanceRoleArns.Count -eq 0) {
        throw "Launch Template use IAM derivation produced an incomplete runtime resource set."
    }
    $launchInstanceResources = @(
        $ltArns
        $runtimeImageIds | ForEach-Object { "arn:aws:ec2:$($script:Region)::image/$_" }
        "arn:aws:ec2:$($script:Region):$($script:AccountId):instance/*"
        "arn:aws:ec2:$($script:Region):$($script:AccountId):network-interface/*"
        "arn:aws:ec2:$($script:Region):$($script:AccountId):volume/*"
        $runtimeSecurityGroupIds | ForEach-Object { "arn:aws:ec2:$($script:Region):$($script:AccountId):security-group/$_" }
        $runtimeSubnetIds | ForEach-Object { "arn:aws:ec2:$($script:Region):$($script:AccountId):subnet/$_" }
    ) | Sort-Object -Unique
    $jobDefBaseArns = @($script:SSOT_JobDef | Where-Object { $_ } | Sort-Object -Unique | ForEach-Object { "arn:aws:batch:$($script:Region):$($script:AccountId):job-definition/${_}" })
    $jobDefRevisionArns = @($jobDefBaseArns | ForEach-Object { "${_}:*" })
    if ($jobDefBaseArns.Count -ne 8 -or $jobDefRevisionArns.Count -ne 8) { throw "Expected exactly eight video job definitions." }
    $apiCanaryInstanceTag = "academy-v1-api-preprod-canary"
    $instanceTags = @($script:ApiInstanceTagValue, $script:MessagingInstanceTagValue, $script:AiInstanceTagValue, $script:ToolsInstanceTagValue | Where-Object { $_ } | Sort-Object -Unique)
    $apiCanaryTagConditions = [ordered]@{
        "ec2:ResourceTag/Name" = $apiCanaryInstanceTag
        "ec2:ResourceTag/Project" = "academy"
        "ec2:ResourceTag/ManagedBy" = "academy-deploy-canary"
    }
    $apiCanarySsmTagConditions = [ordered]@{
        "ssm:resourceTag/Name" = $apiCanaryInstanceTag
        "ssm:resourceTag/Project" = "academy"
        "ssm:resourceTag/ManagedBy" = "academy-deploy-canary"
    }

    $statements = @(
        [ordered]@{Sid="EcrAuth";Effect="Allow";Action="ecr:GetAuthorizationToken";Resource="*"},
        [ordered]@{Sid="EcrPushPull";Effect="Allow";Action=@("ecr:BatchCheckLayerAvailability","ecr:BatchDeleteImage","ecr:BatchGetImage","ecr:CompleteLayerUpload","ecr:GetDownloadUrlForLayer","ecr:InitiateLayerUpload","ecr:PutImage","ecr:UploadLayerPart");Resource=$repoArns},
        [ordered]@{Sid="EcrRepoManage";Effect="Allow";Action=@("ecr:CreateRepository","ecr:DescribeImageScanFindings","ecr:DescribeImages","ecr:DescribeRepositories","ecr:GetLifecyclePolicy","ecr:ListImages","ecr:PutImageScanningConfiguration","ecr:PutImageTagMutability","ecr:StartImageScan");Resource=$repoArns},
        [ordered]@{Sid="AsgInstanceRefresh";Effect="Allow";Action=@("autoscaling:CancelInstanceRefresh","autoscaling:SetInstanceProtection","autoscaling:StartInstanceRefresh","autoscaling:UpdateAutoScalingGroup");Resource=$asgArns},
        [ordered]@{Sid="AsgDescribe";Effect="Allow";Action=@("autoscaling:DescribeAutoScalingGroups","autoscaling:DescribeInstanceRefreshes");Resource="*"},
        [ordered]@{Sid="LaunchTemplateImagePinRead";Effect="Allow";Action=@("ec2:DescribeLaunchTemplates","ec2:DescribeLaunchTemplateVersions");Resource="*"},
        [ordered]@{Sid="LaunchTemplateImagePinWrite";Effect="Allow";Action="ec2:CreateLaunchTemplateVersion";Resource=$ltArns},
        [ordered]@{Sid="LaunchTemplateInstanceUse";Effect="Allow";Action="ec2:RunInstances";Resource=$launchInstanceResources},
        $(if ($templatesRequireInstanceTags) { [ordered]@{Sid="LaunchTemplateInstanceTag";Effect="Allow";Action="ec2:CreateTags";Resource="arn:aws:ec2:$($script:Region):$($script:AccountId):instance/*";Condition=[ordered]@{StringEquals=[ordered]@{"ec2:CreateAction"="RunInstances"}}} }),
        [ordered]@{Sid="LaunchTemplatePassRole";Effect="Allow";Action="iam:PassRole";Resource=$runtimeInstanceRoleArns;Condition=[ordered]@{StringEquals=[ordered]@{"iam:PassedToService"="ec2.amazonaws.com"}}},
        [ordered]@{Sid="RuntimeScalePolicyReadback";Effect="Allow";Action="iam:GetRolePolicy";Resource="arn:aws:iam::$($script:AccountId):role/academy-ec2-role"},
        [ordered]@{Sid="RuntimeLogSinkReadback";Effect="Allow";Action="logs:DescribeLogGroups";Resource="*"},
        [ordered]@{Sid="ProductUsagePilotLogQuery";Effect="Allow";Action=@("logs:StartQuery","logs:GetQueryResults");Resource="arn:aws:logs:$($script:Region):$($script:AccountId):log-group:/academy/api:*"},
        [ordered]@{Sid="ApiCanaryInstanceRead";Effect="Allow";Action=@("ec2:DescribeInstances","ec2:DescribeInstanceStatus");Resource="*"},
        [ordered]@{Sid="ApiCanaryInstanceCleanup";Effect="Allow";Action="ec2:TerminateInstances";Resource="arn:aws:ec2:$($script:Region):$($script:AccountId):instance/*";Condition=[ordered]@{StringEquals=$apiCanaryTagConditions}},
        [ordered]@{Sid="ApiCanaryProfileRead";Effect="Allow";Action="iam:GetInstanceProfile";Resource="arn:aws:iam::$($script:AccountId):instance-profile/$ApiPreprodCanaryProfileName"},
        [ordered]@{Sid="ApiCanarySsmRead";Effect="Allow";Action="ssm:DescribeInstanceInformation";Resource="*"},
        [ordered]@{Sid="SsmSendDocument";Effect="Allow";Action="ssm:SendCommand";Resource="arn:aws:ssm:$($script:Region)::document/AWS-RunShellScript"},
        [ordered]@{Sid="SsmSendInstances";Effect="Allow";Action="ssm:SendCommand";Resource="arn:aws:ec2:$($script:Region):$($script:AccountId):instance/*";Condition=[ordered]@{StringEquals=[ordered]@{"ssm:resourceTag/Name"=$instanceTags}}},
        [ordered]@{Sid="SsmSendApiCanary";Effect="Allow";Action="ssm:SendCommand";Resource="arn:aws:ec2:$($script:Region):$($script:AccountId):instance/*";Condition=[ordered]@{StringEquals=$apiCanarySsmTagConditions}},
        [ordered]@{Sid="SsmCommandRead";Effect="Allow";Action="ssm:GetCommandInvocation";Resource="*"},
        [ordered]@{Sid="ApiPreprodEnvSourceRead";Effect="Allow";Action="ssm:GetParameter";Resource=@(
            "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/env",
            "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/preprod/db-credentials",
            "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/r2/preprod/credentials",
            "arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/preprod/env"
        )},
        [ordered]@{Sid="ApiPreprodEnvPublish";Effect="Allow";Action="ssm:PutParameter";Resource="arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/preprod/env"},
        [ordered]@{Sid="ProductUsagePilotEnvPublish";Effect="Allow";Action="ssm:PutParameter";Resource="arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/api/env"},
        [ordered]@{Sid="DevAlertsAlarmRead";Effect="Allow";Action="cloudwatch:DescribeAlarms";Resource="*"},
        [ordered]@{Sid="DevAlertsParameterRead";Effect="Allow";Action="ssm:GetParameter";Resource="arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/ops/dev-alerts-api-user-impact-state"},
        [ordered]@{Sid="DevAlertsTransitionWrite";Effect="Allow";Action="ssm:PutParameter";Resource="arn:aws:ssm:$($script:Region):$($script:AccountId):parameter/academy/ops/dev-alerts-api-user-impact-state"},
        [ordered]@{Sid="BatchRead";Effect="Allow";Action=@("batch:DescribeComputeEnvironments","batch:DescribeJobDefinitions");Resource="*"},
        [ordered]@{Sid="BatchJobDefinitionRegister";Effect="Allow";Action="batch:RegisterJobDefinition";Resource=$jobDefBaseArns},
        [ordered]@{Sid="BatchJobDefinitionRevisionWrite";Effect="Allow";Action=@("batch:DeregisterJobDefinition","batch:TagResource");Resource=$jobDefRevisionArns},
        [ordered]@{Sid="BatchPassRoles";Effect="Allow";Action="iam:PassRole";Resource=@("arn:aws:iam::$($script:AccountId):role/$JobRoleName","arn:aws:iam::$($script:AccountId):role/$ExecutionRoleName");Condition=[ordered]@{StringEquals=[ordered]@{"iam:PassedToService"=@("batch.amazonaws.com","ecs-tasks.amazonaws.com")}}},
        [ordered]@{Sid="ElbRead";Effect="Allow";Action=@("elasticloadbalancing:DescribeTargetGroups","elasticloadbalancing:DescribeTargetHealth");Resource="*"},
        [ordered]@{Sid="SnsFailureNotify";Effect="Allow";Action="sns:Publish";Resource="arn:aws:sns:$($script:Region):$($script:AccountId):academy-ops-alerts"},
        [ordered]@{Sid="StsIdentity";Effect="Allow";Action="sts:GetCallerIdentity";Resource="*"},
        [ordered]@{Sid="DeploymentControlLock";Effect="Allow";Action=@("dynamodb:DeleteItem","dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem");Resource="arn:aws:dynamodb:$($script:Region):$($script:AccountId):table/$($script:DynamoLockTableName)"}
    )
    $expected = [ordered]@{Version="2012-10-17";Statement=$statements}
    $expectedJson = $expected | ConvertTo-Json -Depth 50 -Compress
    $currentJson = if ($currentPolicy -and $currentPolicy.PolicyDocument) { $currentPolicy.PolicyDocument | ConvertTo-Json -Depth 50 -Compress } else { "" }
    if ($currentJson -ne $expectedJson) {
        $policyRef = Convert-JsonArgToFileRef $expectedJson
        $policyFile = $policyRef -replace '^file://', ''
        try { Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $policyName, "--policy-document", $policyRef) -ErrorMessage "put exact GitHub Actions policy" | Out-Null }
        finally { Remove-TempFiles @($policyFile) }
        $script:ChangesMade = $true
    }
    $readback = Invoke-AwsJson @("iam", "get-role-policy", "--role-name", $roleName, "--policy-name", $policyName, "--output", "json")
    $actualJson = $readback.PolicyDocument | ConvertTo-Json -Depth 50 -Compress
    if ($actualJson -ne $expectedJson) { throw "GitHub Actions IAM full-policy readback does not exactly match the managed least-privilege contract." }
    $developmentConverger = Join-Path (Get-Item $PSScriptRoot).Parent.FullName "converge-api-development-oidc.ps1"
    $convergerAwsProfile = if ($env:AWS_PROFILE) {
        [string]$env:AWS_PROFILE
    } else {
        "default"
    }
    & $developmentConverger -AwsProfile $convergerAwsProfile
    $developmentPolicyArn = (
        "arn:aws:iam::$($script:AccountId):policy/" +
        [string]$script:GitHubActionsDevelopmentDeployPolicyName
    )
    $attached = Invoke-AwsJson @(
        "iam", "list-attached-role-policies",
        "--role-name", $roleName,
        "--output", "json"
    )
    $attachedArns = @(
        $attached.AttachedPolicies |
            ForEach-Object { [string]$_.PolicyArn } |
            Sort-Object -Unique
    )
    if (
        $attachedArns.Count -ne 1 -or
        $attachedArns[0] -ne $developmentPolicyArn
    ) {
        throw "GitHub Actions role must have exactly the owned development managed policy attached."
    }
    Write-Ok "GitHub Actions deploy IAM converged with exact inline and attached policy inventory"
}

function Ensure-BatchIAM {
    if ($script:PlanMode) { return @{ ServiceRoleArn = ""; InstanceProfileArn = ""; JobRoleArn = ""; ExecutionRoleArn = "" } }
    Write-Step "Ensure Batch IAM"
    $trustBatch = Join-Path $TemplatesPath "trust_batch_service.json"
    $trustEc2 = Join-Path $TemplatesPath "trust_ec2.json"
    $trustEcsTasks = Join-Path $TemplatesPath "trust_ecs_tasks.json"
    $policyJob = Join-Path $TemplatesPath "policy_video_job_role.json"
    $policyBatchService = Join-Path $TemplatesPath "policy_batch_service_role.json"
    $policyEcsExecution = Join-Path $TemplatesPath "policy_ecs_task_execution_role.json"
    if (-not (Test-Path $trustBatch) -or -not (Test-Path $trustEc2)) {
        throw "IAM template not found under $TemplatesPath"
    }
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $BatchServiceRoleName, "--output", "json")
    if (-not $role) {
        Write-Host "  Creating $BatchServiceRoleName" -ForegroundColor Yellow
        $script:ChangesMade = $true
        Invoke-Aws @("iam", "create-role", "--role-name", $BatchServiceRoleName, "--assume-role-policy-document", "file://$($trustBatch -replace '\\','/')") -ErrorMessage "iam create-role BatchService" | Out-Null
    }
    Invoke-Aws @("iam", "attach-role-policy", "--role-name", $BatchServiceRoleName, "--policy-arn", "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole") -ErrorMessage "attach BatchServiceRole" 2>$null | Out-Null
    if (Test-Path $policyBatchService) {
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $BatchServiceRoleName, "--policy-name", "academy-batch-service-inline", "--policy-document", "file://$($policyBatchService -replace '\\','/')") -ErrorMessage "put-role-policy" 2>$null | Out-Null
    }
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $EcsInstanceRoleName, "--output", "json")
    if (-not $role) {
        Write-Host "  Creating $EcsInstanceRoleName" -ForegroundColor Yellow
        $script:ChangesMade = $true
        Invoke-Aws @("iam", "create-role", "--role-name", $EcsInstanceRoleName, "--assume-role-policy-document", "file://$($trustEc2 -replace '\\','/')") -ErrorMessage "iam create-role ECS instance" | Out-Null
    }
    Invoke-Aws @("iam", "attach-role-policy", "--role-name", $EcsInstanceRoleName, "--policy-arn", "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role") -ErrorMessage "attach ECS instance" 2>$null | Out-Null
    # DynamoDB video job lock: instance role fallback (jobRoleArn이 설정되지 않은 컨테이너 대비)
    $policyInstanceDynamo = Join-Path $TemplatesPath "policy_instance_dynamodb.json"
    if (Test-Path $policyInstanceDynamo) {
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $EcsInstanceRoleName, "--policy-name", "dynamodb-video-job-lock", "--policy-document", "file://$($policyInstanceDynamo -replace '\\','/')") -ErrorMessage "put instance DynamoDB policy" 2>$null | Out-Null
    }
    $ip = Invoke-AwsJson @("iam", "get-instance-profile", "--instance-profile-name", $InstanceProfileName, "--output", "json")
    if (-not $ip) {
        $script:ChangesMade = $true
        Invoke-Aws @("iam", "create-instance-profile", "--instance-profile-name", $InstanceProfileName) -ErrorMessage "create instance profile" | Out-Null
        Invoke-Aws @("iam", "add-role-to-instance-profile", "--instance-profile-name", $InstanceProfileName, "--role-name", $EcsInstanceRoleName) -ErrorMessage "add role to profile" | Out-Null
    } else {
        $hasRole = $ip.InstanceProfile.Roles | Where-Object { $_.RoleName -eq $EcsInstanceRoleName }
        if (-not $hasRole) {
            $script:ChangesMade = $true
            Invoke-Aws @("iam", "add-role-to-instance-profile", "--instance-profile-name", $InstanceProfileName, "--role-name", $EcsInstanceRoleName) -ErrorMessage "add role to profile" | Out-Null
        }
    }
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $ExecutionRoleName, "--output", "json")
    if (-not $role) {
        Write-Host "  Creating $ExecutionRoleName" -ForegroundColor Yellow
        $script:ChangesMade = $true
        Invoke-Aws @("iam", "create-role", "--role-name", $ExecutionRoleName, "--assume-role-policy-document", "file://$($trustEcsTasks -replace '\\','/')") -ErrorMessage "iam create-role execution" | Out-Null
    }
    Invoke-Aws @("iam", "attach-role-policy", "--role-name", $ExecutionRoleName, "--policy-arn", "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy") -ErrorMessage "attach execution" 2>$null | Out-Null
    if (Test-Path $policyEcsExecution) {
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $ExecutionRoleName, "--policy-name", "academy-batch-execution-inline", "--policy-document", "file://$($policyEcsExecution -replace '\\','/')") -ErrorMessage "put execution inline" 2>$null | Out-Null
    }
    $role = Invoke-AwsJson @("iam", "get-role", "--role-name", $JobRoleName, "--output", "json")
    if (-not $role) {
        Write-Host "  Creating $JobRoleName" -ForegroundColor Yellow
        $script:ChangesMade = $true
        Invoke-Aws @("iam", "create-role", "--role-name", $JobRoleName, "--assume-role-policy-document", "file://$($trustEcsTasks -replace '\\','/')") -ErrorMessage "iam create-role job" | Out-Null
    }
    if (Test-Path $policyJob) {
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $JobRoleName, "--policy-name", "academy-video-batch-job-inline", "--policy-document", "file://$($policyJob -replace '\\','/')") -ErrorMessage "put job inline" | Out-Null
    }
    $serviceRoleArn = (Invoke-AwsJson @("iam", "get-role", "--role-name", $BatchServiceRoleName, "--output", "json")).Role.Arn
    $instanceProfileArn = (Invoke-AwsJson @("iam", "get-instance-profile", "--instance-profile-name", $InstanceProfileName, "--output", "json")).InstanceProfile.Arn
    $jobRoleArn = (Invoke-AwsJson @("iam", "get-role", "--role-name", $JobRoleName, "--output", "json")).Role.Arn
    $executionRoleArn = (Invoke-AwsJson @("iam", "get-role", "--role-name", $ExecutionRoleName, "--output", "json")).Role.Arn
    Write-Ok "Batch IAM ready"
    return @{
        ServiceRoleArn = $serviceRoleArn
        InstanceProfileArn = $instanceProfileArn
        JobRoleArn = $jobRoleArn
        ExecutionRoleArn = $executionRoleArn
    }
}

# API/Build EC2 인스턴스가 SSM에 등록되고 ECR에서 이미지를 Pull할 수 있도록 instance profile 역할에 정책 부여
function Ensure-EC2InstanceProfileSSM {
    if ($script:PlanMode) { return }
    $profileName = $script:ApiInstanceProfile
    if (-not $profileName) { $profileName = $script:BuildInstanceProfile }
    if (-not $profileName) { return }
    $ip = Invoke-AwsJson @("iam", "get-instance-profile", "--instance-profile-name", $profileName, "--output", "json")
    if (-not $ip -or -not $ip.InstanceProfile -or -not $ip.InstanceProfile.Roles -or $ip.InstanceProfile.Roles.Count -eq 0) {
        Write-Warn "Instance profile $profileName not found; SSM policy not attached."
        return
    }
    $roleName = $ip.InstanceProfile.Roles[0].RoleName
    $policies = Invoke-AwsJson @("iam", "list-attached-role-policies", "--role-name", $roleName, "--output", "json")
    $hasSsm = $policies.AttachedPolicies | Where-Object { $_.PolicyArn -eq "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" }
    if (-not $hasSsm) {
        Invoke-Aws @("iam", "attach-role-policy", "--role-name", $roleName, "--policy-arn", "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore") -ErrorMessage "attach SSM to EC2 role" | Out-Null
        Write-Ok "Attached AmazonSSMManagedInstanceCore to $roleName (SSM agent can register)"
        $script:ChangesMade = $true
    } else {
        Write-Ok "EC2 role $roleName already has AmazonSSMManagedInstanceCore"
    }
    $hasEcr = $policies.AttachedPolicies | Where-Object { $_.PolicyArn -eq "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly" }
    if (-not $hasEcr) {
        Invoke-Aws @("iam", "attach-role-policy", "--role-name", $roleName, "--policy-arn", "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly") -ErrorMessage "attach ECR read to EC2 role" | Out-Null
        Write-Ok "Attached AmazonEC2ContainerRegistryReadOnly to $roleName (API/Build can pull ECR images)"
        $script:ChangesMade = $true
    } else {
        Write-Ok "EC2 role $roleName already has AmazonEC2ContainerRegistryReadOnly"
    }
    $hasEcrPush = $policies.AttachedPolicies | Where-Object { $_.PolicyArn -eq "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser" }
    if (-not $hasEcrPush) {
        Invoke-Aws @("iam", "attach-role-policy", "--role-name", $roleName, "--policy-arn", "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser") -ErrorMessage "attach ECR PowerUser to EC2 role" | Out-Null
        Write-Ok "Attached AmazonEC2ContainerRegistryPowerUser to $roleName (Build can push ECR images)"
        $script:ChangesMade = $true
    } else {
        Write-Ok "EC2 role $roleName already has AmazonEC2ContainerRegistryPowerUser"
    }
    # API upload_complete: Batch SubmitJob + DynamoDB video job lock
    $policyApiVideo = Join-Path $TemplatesPath "policy_api_video_upload.json"
    if (Test-Path $policyApiVideo) {
        $inlineName = "academy-api-video-upload"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyApiVideo -replace '\\','/')") -ErrorMessage "put API video upload policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (Batch+DynamoDB for upload_complete)"
    }
    # API diagnostics: validate_video_production_readiness needs read-only infra/observability checks.
    $policyVideoReadiness = Join-Path $TemplatesPath "policy_video_readiness_observability_readonly.json"
    if (Test-Path $policyVideoReadiness) {
        $inlineName = "academy-video-readiness-observability-readonly"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyVideoReadiness -replace '\\','/')") -ErrorMessage "put video readiness observability policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (video readiness read-only checks)"
    }
    # Messaging/AI 워커: SQS ReceiveMessage, DeleteMessage, ChangeMessageVisibility
    $policyWorkersSqs = Join-Path $TemplatesPath "policy_workers_sqs.json"
    if (Test-Path $policyWorkersSqs) {
        $inlineName = "academy-workers-sqs"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyWorkersSqs -replace '\\','/')") -ErrorMessage "put workers SQS policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (Messaging/AI SQS consume)"
    }
    # API와 AI/Tools 작업 로그는 인스턴스 교체·scale-in 뒤에도 남긴다.
    $policyEc2CloudWatchLogs = Join-Path $TemplatesPath "policy_ec2_cloudwatch_logs.json"
    if (Test-Path $policyEc2CloudWatchLogs) {
        $inlineName = "academy-ec2-cloudwatch-logs"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyEc2CloudWatchLogs -replace '\\','/')") -ErrorMessage "put API/worker CloudWatch logs policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (API/AI/Tools container logs)"
    }
    # Problem Studio AI transcription: no static provider secret is required.
    # The shared EC2 role may invoke only the sealed Nova 2 Lite inference profile/model.
    $policyWorkersBedrock = Join-Path $TemplatesPath "policy_workers_bedrock_problem_transcription.json"
    if (Test-Path $policyWorkersBedrock) {
        $inlineName = "academy-workers-bedrock-problem-transcription"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyWorkersBedrock -replace '\\','/')") -ErrorMessage "put worker Bedrock transcription policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (Problem Studio Nova 2 Lite only)"
    }
    # OMR/AI and messaging worker scaling: API wakes worker ASGs after enqueue; AI worker scales itself in after live idle checks.
    $policyApiAiWorkerScale = Join-Path $TemplatesPath "policy_api_ai_worker_scale.json"
    if (Test-Path $policyApiAiWorkerScale) {
        $inlineName = "academy-api-ai-worker-scale"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyApiAiWorkerScale -replace '\\','/')") -ErrorMessage "put API AI worker scale policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (AI/messaging worker ASG wake + AI idle scale-in)"
    }
    # 워커 UserData: 부팅 시 aws ssm get-parameter로 /academy/workers/env 조회
    $policyEc2Ssm = Join-Path $TemplatesPath "policy_ec2_ssm_get_parameters.json"
    if (Test-Path $policyEc2Ssm) {
        $inlineName = "academy-ec2-ssm-get-parameters"
        Invoke-Aws @("iam", "put-role-policy", "--role-name", $roleName, "--policy-name", $inlineName, "--policy-document", "file://$($policyEc2Ssm -replace '\\','/')") -ErrorMessage "put EC2 SSM GetParameter policy" | Out-Null
        Write-Ok "Ensured inline policy $inlineName on $roleName (UserData SSM /academy/*)"
    }
}
