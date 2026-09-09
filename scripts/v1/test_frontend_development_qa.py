"""Offline contracts; never load credentials or contact AWS."""
import ast
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shlex
import sys
import subprocess
import tempfile
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
BOUNDARY = ROOT / "scripts/v1/templates/iam/policy_api_development_parameter_boundary.json"


def powershell_host_writer(mode="direct", holder="", fault="", missing=False, env_overrides=None):
    """Execute the real initializer/prerequisite/IAM/guard against local fakes only."""
    with tempfile.TemporaryDirectory(prefix="academy-host-lock-test-") as directory:
        fixture = Path(directory)
        scripts = fixture / "scripts/v1"
        for name in ("initialize-api-development.ps1", "converge-api-development-prerequisites.ps1",
                     "resources/iam.ps1", "core/guard.ps1", "templates/iam/trust_ec2.json",
                     "templates/iam/policy_api_development_parameter_boundary.json"):
            path = scripts / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / "scripts/v1" / name).read_bytes())
        stubs = {
            "core/env.ps1": "function Assert-AwsMutationIdentity { return @{ Account='809466760795' } }",
            "core/logging.ps1": "function Write-Step {}\nfunction Write-Ok {}\nfunction Write-Warn {}",
            "core/aws.ps1": "# AWS functions are fail-closed local fakes in the harness.",
            "core/ssot.ps1": """
function Load-SSOT {
    $script:Region='ap-northeast-2'; $script:AccountId='809466760795'
    $script:DynamoLockTableName='academy-v1-video-job-lock'
    $script:ApiDevelopmentEnabled=$true; $script:ApiDevelopmentAccessMode='ssm-only'
    $script:ApiDevelopmentRoleName='academy-api-development-role'
    $script:ApiDevelopmentInstanceProfileName='academy-api-development'
    $script:ApiDevelopmentR2CredentialParameter='/academy/r2/development/credentials'
    $script:ApiDevelopmentSecurityGroupName='development'; $script:VpcId='vpc-fixture'
    $script:SecurityGroupData='sg-data'; $script:ApiDevelopmentAiQueueName='dev-ai'
    $script:ApiDevelopmentToolsQueueName='dev-tools'; $script:ApiDevelopmentMessagingQueueName='dev-messaging'
    $script:EcrApiRepo='academy-api'; $script:EcrToolsRepo='academy-tools-worker'; $script:EcrAiRepo='academy-ai-worker-cpu'; $script:EcrMessagingRepo='academy-messaging-worker'
}
""",
            "resources/worker_userdata.ps1": "function Get-ReleaseManifestImage { return @{GitSha=('a'*40);Digest=('sha256:'+('b'*64))} }",
            "converge-api-development-oidc.ps1": "param($AwsProfile)\n$global:events.Add('child:oidc')",
            "converge-api-development-database.ps1": "param($TimeoutSec,$AwsProfile)\n$global:events.Add('child:database')",
            "publish-api-development-env.ps1": """
param($ReleaseId,$GithubOutputPath,$AwsProfile)
$global:events.Add('child:publish')
Set-Content -LiteralPath $GithubOutputPath -Value "parameter_version=1`nworkers_parameter_version=2`nproduction_database_name=production"
""",
            "deploy-api-development.ps1": """
param($ApiImageUri,$ToolsImageUri,$AiImageUri,$MessagingImageUri,$ExpectedEnvVersion,$ExpectedWorkersEnvVersion,$ExpectedReleaseId,$ExpectedProductionDatabaseName,$TimeoutSec,$AwsProfile)
if ($ExpectedEnvVersion -ne 1 -or $ExpectedWorkersEnvVersion -ne 2 -or $ExpectedProductionDatabaseName -ne 'production' -or $ApiImageUri -notmatch '@sha256:' -or $MessagingImageUri -notmatch 'academy-messaging-worker@sha256:') { throw 'initializer argument regression' }
$global:events.Add('child:deploy')
""",
        }
        for name, source in stubs.items():
            (scripts / name).write_text(source, encoding="utf-8")
        harness = r"""
$ErrorActionPreference='Stop'
$global:events=[Collections.Generic.List[string]]::new()
$global:holder=$env:FIXTURE_HOLDER
$global:fault=$env:FIXTURE_FAULT
$global:roleExists=$env:FIXTURE_MISSING -ne 'true'
$global:profileExists=$global:roleExists
$global:profileBound=$global:roleExists -and $global:fault -ne 'unbound'
$global:hostWrites=0
$global:policy=$null
function global:aws {
    if ($args[0] -eq 'sts' -and $args[1] -eq 'get-caller-identity') {
        '{"Account":"809466760795","Arn":"arn:aws:iam::809466760795:user/offline"}'
        $global:LASTEXITCODE=0; return
    }
    throw "UNSTUBBED NATIVE AWS: $($args[0])/$($args[1])"
}
function global:python {
    $action=$args[1]; $owner=$args[([array]::IndexOf($args,'--owner')+1)]
    $table=$args[([array]::IndexOf($args,'--table-name')+1)]
    if ($table -ne 'academy-v1-video-job-lock') { throw 'wrong lock table' }
    $global:events.Add("lock:$action")
    $global:LASTEXITCODE=0
    switch ($action) {
        'acquire' { if ($global:holder) {$global:LASTEXITCODE=2} else {$global:holder=$owner} }
        'assert-owned' { if ($global:holder -ne $owner) {$global:LASTEXITCODE=2} }
        'renew' { if ($global:holder -ne $owner) {$global:LASTEXITCODE=2} }
        'release' {
            if ($global:holder -ne $owner -or $global:fault -eq 'release') {$global:LASTEXITCODE=2}
            else {$global:holder=''}
        }
        default { throw "unknown lock action $action" }
    }
}
function global:Convert-JsonArgToFileRef { param($Json) $global:converted=$Json; return 'file://offline.json' }
function global:Remove-TempFiles {}
function global:Invoke-AwsJson {
    param([string[]]$ArgsArray)
    switch ("$($ArgsArray[0])/$($ArgsArray[1])") {
        'sts/get-caller-identity' { return @{Account='809466760795';Arn='arn:aws:iam::809466760795:user/offline'} }
        'ssm/describe-parameters' { return @{Parameters=@(@{Type='SecureString'})} }
        'ec2/describe-security-groups' {
            if ($ArgsArray -contains '--group-ids') { return @{SecurityGroups=@(@{IpPermissions=@(@{IpProtocol='tcp';FromPort=5432;ToPort=5432;UserIdGroupPairs=@(@{GroupId='sg-dev'})})})} }
            return @{SecurityGroups=@(@{GroupId='sg-dev';IpPermissions=@()})}
        }
        'sqs/get-queue-url' { return @{QueueUrl='https://offline.invalid/queue'} }
        'sqs/get-queue-attributes' { return @{Attributes=@{QueueArn='arn:offline'}} }
        'iam/get-role' { if ($global:roleExists) {return @{Role=@{RoleName='academy-api-development-role'}}}; return $null }
        'iam/get-instance-profile' {
            if (-not $global:profileExists) { return $null }
            $roles=if($global:profileBound){@(@{RoleName='academy-api-development-role'})}else{@()}
            return @{InstanceProfile=@{Roles=$roles}}
        }
        'iam/get-role-policy' {
            if ($global:fault -eq 'postverify') {return @{PolicyDocument=@{Statement=@()}}}
            return @{PolicyDocument=$global:policy}
        }
        'iam/list-attached-role-policies' {return @{AttachedPolicies=@(@{PolicyArn='arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'})}}
        'iam/list-role-policies' {return @{PolicyNames=@('academy-api-development-runtime')}}
        default { throw "UNSTUBBED AWS READ: $($ArgsArray[0])/$($ArgsArray[1])" }
    }
}
function global:Invoke-Aws {
    param([string[]]$ArgsArray,[string]$ErrorMessage)
    if ($ArgsArray[0] -eq 'sqs' -and $ArgsArray[1] -eq 'set-queue-attributes') { $global:events.Add('queue:attributes'); return }
    if ($ArgsArray[0] -ne 'iam') {throw 'unapproved fake mutation'}
    $global:hostWrites++; $global:events.Add("iam:$($ArgsArray[1])")
    switch ($ArgsArray[1]) {
        'create-role' {$global:roleExists=$true}
        'update-assume-role-policy' {}
        'attach-role-policy' {}
        'put-role-policy' {$global:policy=$global:converted | ConvertFrom-Json}
        'create-instance-profile' {$global:profileExists=$true}
        'add-role-to-instance-profile' {$global:profileBound=$true}
        default {throw 'unexpected IAM mutation'}
    }
    if ($global:fault -eq 'lost' -and $global:hostWrites -eq 1) {$global:holder='peer'}
    if ($global:fault -eq 'timeout' -and $ArgsArray[1] -eq 'put-role-policy') {throw 'commit then timeout'}
}
$failure=''
try {
    if ($env:FIXTURE_MODE -eq 'initializer') {
        & "$env:FIXTURE_ROOT/initialize-api-development.ps1" -AwsProfile offline
    } elseif ($env:FIXTURE_MODE -eq 'prerequisites') {
        & "$env:FIXTURE_ROOT/converge-api-development-prerequisites.ps1" -AwsProfile offline
    } else {
        . "$env:FIXTURE_ROOT/core/ssot.ps1"
        . "$env:FIXTURE_ROOT/core/logging.ps1"
        . "$env:FIXTURE_ROOT/core/guard.ps1"
        . "$env:FIXTURE_ROOT/resources/iam.ps1"
        Load-SSOT
        $script:PlanMode=$env:FIXTURE_MODE -eq 'plan'
        $script:DeployLockAcquired=$false
        Ensure-ApiDevelopmentIAM | Out-Null
    }
} catch {$failure=$_.Exception.Message}
'FIXTURE_RESULT='+(@{error=$failure;events=@($global:events);holder=$global:holder;writes=$global:hostWrites;policy=$global:policy}|ConvertTo-Json -Depth 30 -Compress)
"""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AWS_", "ACADEMY_DEPLOY_LOCK", "ACADEMY_RUNTIME_ENV_LOCK"))}
        env.update(FIXTURE_ROOT=str(scripts), FIXTURE_MODE=mode, FIXTURE_HOLDER=holder,
                   FIXTURE_FAULT=fault, FIXTURE_MISSING=str(missing).lower())
        env.update(env_overrides or {})
        result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", harness],
                                capture_output=True, text=True, timeout=30, env=env)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("FIXTURE_RESULT=")]
        if len(lines) != 1:
            raise AssertionError(result.stdout + result.stderr)
        return json.loads(lines[0].split("=", 1)[1])


class HostWriterLockTests(unittest.TestCase):
    def test_direct_host_ensure_refuses_all_writes_without_owned_lock(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                result = powershell_host_writer(missing=missing)
                self.assertEqual(result["writes"], 0, result)
                self.assertTrue(result["error"], result)

    def test_prerequisites_cannot_write_or_release_another_writers_lease(self):
        result = powershell_host_writer("prerequisites", holder="host-boundary-peer")
        self.assertEqual(result["writes"], 0, result)
        self.assertEqual(result["holder"], "host-boundary-peer")
        self.assertNotIn("lock:release", result["events"])
        self.assertTrue(result["error"])

    def test_lost_ownership_stops_before_next_iam_write(self):
        result = powershell_host_writer("prerequisites", fault="lost")
        self.assertEqual(result["writes"], 1, result)
        self.assertEqual(result["holder"], "peer")
        self.assertTrue(result["error"])
        self.assertNotIn("lock:release", result["events"])

    def test_commit_timeout_and_postverify_failure_retain_without_retry_or_rollback(self):
        for fault in ("timeout", "postverify"):
            with self.subTest(fault=fault):
                result = powershell_host_writer("prerequisites", fault=fault)
                self.assertTrue(result["holder"], result)
                self.assertTrue(result["error"], result)
                self.assertEqual(result["events"].count("iam:put-role-policy"), 1)
                self.assertNotIn("lock:release", result["events"])
                self.assertNotIn("child:oidc", result["events"])

    def test_initializer_existing_and_first_bootstrap_normal_flow_is_preserved(self):
        for missing, fault, expected_writes in ((False, "", 3), (True, "", 5), (False, "unbound", 4)):
            with self.subTest(missing=missing, fault=fault):
                result = powershell_host_writer("initializer", missing=missing, fault=fault)
                self.assertEqual(result["error"], "", result)
                self.assertEqual(result["events"][-3:], ["child:database", "child:publish", "child:deploy"])
                self.assertEqual(result["writes"], expected_writes)
                self.assertEqual(result["holder"], "")
                self.assertEqual(result["events"].count("lock:acquire"), 1)
                self.assertEqual(result["events"].count("lock:release"), 1)
                self.assertLess(result["events"].index("lock:acquire"), next(
                    index for index, event in enumerate(result["events"]) if event.startswith("iam:")))
                self.assertLess(result["events"].index("iam:put-role-policy"), result["events"].index("lock:release"))
                self.assertLess(result["events"].index("lock:release"), result["events"].index("child:oidc"))

    def test_plan_does_not_acquire_or_mutate(self):
        result = powershell_host_writer("plan")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["events"], [])

    def test_bootstrap_refuses_inherited_owner_and_release_failure_stops_initializer(self):
        for key in ("ACADEMY_DEPLOY_LOCK_OWNER", "ACADEMY_RUNTIME_ENV_LOCK_OWNER"):
            result = powershell_host_writer("initializer", holder="parent", env_overrides={key: "parent"})
            self.assertTrue(result["error"])
            self.assertEqual(result["writes"], 0)
            self.assertEqual(result["holder"], "parent")
            self.assertNotIn("lock:acquire", result["events"])
            self.assertNotIn("lock:release", result["events"])
        result = powershell_host_writer("initializer", fault="release")
        self.assertIn("HOST_IAM_LOCK_RELEASE_FAILED", result["error"])
        self.assertTrue(result["holder"])
        self.assertNotIn("child:deploy", result["events"])

    def test_host_iam_direct_call_inventory_is_explicit(self):
        callers = {path.relative_to(ROOT / "scripts/v1").as_posix()
                   for path in (ROOT / "scripts/v1").rglob("*.ps1")
                   if re.search(r"(?m)^\s*Ensure-ApiDevelopmentIAM\s", path.read_text(encoding="utf-8-sig"))}
        self.assertEqual(callers, {"converge-api-development-prerequisites.ps1"})


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/v1" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHostAws:
    def __init__(self, subject):
        self.subject = subject
        self.calls = []
        self.fault = ""
        self.reads = 0
        self.policy = {"Version": "2012-10-17", "Id": "preserve-manual-fields",
                       "Statement": [{"Sid": "Existing", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}]}
        self.role = {"Arn": f"arn:aws:iam::{subject.ACCOUNT}:role/{subject.ROLE}", "RoleId": "AROAOFFLINE",
                     "AssumeRolePolicyDocument": json.loads((BOUNDARY.parent / "trust_ec2.json").read_text())}
        self.profile = {"Arn": f"arn:aws:iam::{subject.ACCOUNT}:instance-profile/academy-api-development",
                        "InstanceProfileId": "AIPAOFFLINE", "InstanceProfileName": "academy-api-development",
                        "Roles": [{**self.role, "RoleName": subject.ROLE}]}
        self.instance = {"InstanceId": "i-offline", "State": {"Name": "running"},
                         "IamInstanceProfile": {"Arn": self.profile["Arn"]}, "Tags": [
                             {"Key": key, "Value": value} for key, value in {
                                 "Name": "academy-v1-api-development", "Environment": "development",
                                 "ManagedBy": "academy-api-development", "Lifecycle": "active"}.items()]}

    def __call__(self, args, profile):
        self.calls.append(copy.deepcopy(args))
        operation = tuple(args[:2])
        s = self.subject
        if operation == ("sts", "get-caller-identity"):
            return {"Account": s.ACCOUNT, "Arn": f"arn:aws:iam::{s.ACCOUNT}:user/offline"}
        if operation == ("iam", "get-role"):
            return {"Role": copy.deepcopy(self.role)}
        if operation == ("iam", "list-role-policies"):
            return {"PolicyNames": [s.INLINE]}
        if operation == ("iam", "get-role-policy"):
            self.reads += 1
            if self.fault == "hash-drift" and self.reads == 2:
                self.policy["Id"] = "peer-edit"
            return {"PolicyDocument": copy.deepcopy(self.policy)}
        if operation == ("iam", "list-attached-role-policies"):
            return {"AttachedPolicies": [{"PolicyName": "AmazonSSMManagedInstanceCore",
                                          "PolicyArn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}]}
        if operation == ("iam", "get-policy"):
            return {"Policy": {"DefaultVersionId": "v2"}}
        if operation == ("iam", "get-policy-version"):
            return {"PolicyVersion": {"VersionId": "v2", "Document": {"Version": "2012-10-17", "Statement": []}}}
        if operation == ("iam", "list-instance-profiles-for-role"):
            return {"InstanceProfiles": [copy.deepcopy(self.profile)]}
        if operation == ("ec2", "describe-instances"):
            return {"Reservations": [{"Instances": [copy.deepcopy(self.instance)]}]}
        if operation == ("ec2", "describe-instance-attribute"):
            return {"DisableApiTermination": {"Value": True}}
        if operation == ("iam", "put-role-policy"):
            self.policy = json.loads(args[args.index("--policy-document") + 1])
            if self.fault == "commit-timeout":
                raise subprocess.TimeoutExpired("offline-put", 30)
            if self.fault == "commit-interrupt":
                raise KeyboardInterrupt("must-not-print-this-exception-payload")
            if self.fault == "inventory-drift":
                self.role["RoleId"] = "PEER"
            return {}
        if operation == ("iam", "simulate-principal-policy"):
            action = args[args.index("--action-names") + 1]
            end = args.index("--policy-input-list") if "--policy-input-list" in args else len(args)
            resources = args[args.index("--resource-arns") + 1:end]
            expected = {(action, resource): decision for action, resource, decision in s.simulation_cases()}
            results = [{"EvalActionName": action, "EvalResourceName": resource,
                        "EvalDecision": expected[(action, resource)]} for resource in resources]
            if self.fault == "postverify":
                results[0]["EvalDecision"] = "implicitDeny"
            if self.fault == "duplicate":
                results.append(copy.deepcopy(results[0]))
            if self.fault == "missing":
                results.pop()
            if self.fault == "context":
                results[0]["MissingContextValues"] = ["offline-key"]
            return {"EvaluationResults": results}
        raise AssertionError(f"Unstubbed AWS call: {operation}")


class FakeSharedLease:
    """Run the actual shared-lock algorithm with an in-memory DynamoDB boundary."""
    def __init__(self):
        self.module = load_script("deployment_lock")
        self.item = None
        self.actions = []
        self.after_acquire = None
        self.fail_release = False

    def ddb(self, operation, *args):
        self.assert_table(args)
        if operation == "get-item":
            return {"Item": copy.deepcopy(self.item or {})}
        values = json.loads(args[args.index("--expression-attribute-values") + 1])
        now = int(values.get(":now", {"N": "0"})["N"])
        if operation == "put-item":
            if self.item and int(self.item["ttl"]["N"]) >= now:
                raise RuntimeError("ConditionalCheckFailedException")
            self.item = json.loads(args[args.index("--item") + 1])
        elif operation in ("update-item", "delete-item"):
            if (not self.item or self.item["owner"] != values[":owner"] or
                    (operation == "update-item" and int(self.item["ttl"]["N"]) < now)):
                raise RuntimeError("ConditionalCheckFailedException")
            if operation == "delete-item":
                if self.fail_release:
                    raise RuntimeError("offline release failure")
                self.item = None
            else:
                self.item["ttl"] = values[":expires"]
        else:
            raise AssertionError(operation)
        return {}

    def assert_table(self, args):
        assert args[args.index("--table-name") + 1] == "academy-v1-video-job-lock"
        key = json.loads(args[args.index("--item") + 1] if "--item" in args else args[args.index("--key") + 1])
        assert key["videoId"]["S"] == "__deployment_control_v2__"

    def __call__(self, action, owner, profile):
        self.actions.append(action)
        function = getattr(self.module, action.replace("-", "_"))
        args = (self.module.DEFAULT_TABLE, owner)
        with patch.object(self.module, "_aws", side_effect=self.ddb):
            function(*args, 10800) if action in ("acquire", "renew") else function(*args)
        if action == "acquire" and self.after_acquire:
            self.after_acquire(owner)


class HostBoundaryApplyTests(unittest.TestCase):
    def setUp(self):
        self.subject = load_script("converge_frontend_development_qa")
        self.aws = FakeHostAws(self.subject)
        self.lease = FakeSharedLease()
        self.enterContext(patch.dict(os.environ, {key: value for key, value in os.environ.items()
            if not key.startswith(("AWS_", "ACADEMY_DEPLOY_LOCK", "ACADEMY_RUNTIME_ENV_LOCK"))}, clear=True))
        self.enterContext(patch.object(self.subject, "aws", side_effect=self.aws))
        self.enterContext(patch.object(self.subject, "lock_action", side_effect=self.lease))
        self.before = copy.deepcopy(self.aws.policy)
        self.after = {**self.before, "Statement": self.before["Statement"] + self.subject.proposed_boundary()["Statement"]}
        snapshot = self.subject.host_snapshot("offline")
        self.hashes = (self.subject.policy_hash(self.before), self.subject.policy_hash(self.after),
                       self.subject.policy_hash(snapshot["inventory"]))
        self.aws.calls.clear()
        self.aws.reads = 0

    def apply(self):
        return self.subject.apply_host_boundary("offline", *self.hashes)

    def puts(self):
        return [call for call in self.aws.calls if call[:2] == ["iam", "put-role-policy"]]

    def test_exact_single_policy_apply_preserves_fields_and_verifies_without_overlay(self):
        result = self.apply()
        self.assertEqual(result["mode"], "HOST_POLICY_APPLIED")
        self.assertEqual(result["lock_state"], "released")
        self.assertEqual(self.aws.policy, self.after)
        self.assertEqual(len(self.puts()), 1)
        call, = self.puts()
        self.assertEqual(call[2:6], ["--role-name", self.subject.ROLE, "--policy-name", self.subject.INLINE])
        self.assertEqual(len(result["verification"]["cases"]), 44)
        self.assertFalse(any("--policy-input-list" in args for args in self.aws.calls))
        self.assertIsNone(self.lease.item)

    def test_hash_drift_before_dispatch_releases_without_put(self):
        self.aws.fault = "hash-drift"
        with self.assertRaises(self.subject.HostApplyError) as caught:
            self.apply()
        self.assertEqual(self.puts(), [])
        self.assertEqual(caught.exception.report["lock_state"], "released")

    def test_other_writer_held_lock_fails_acquisition_without_release(self):
        self.lease("acquire", "bootstrap-peer", "offline")
        with self.assertRaises(self.subject.HostApplyError):
            self.apply()
        self.assertEqual(self.puts(), [])
        self.assertNotIn("release", self.lease.actions)
        self.assertEqual(self.lease.item["owner"]["S"], "bootstrap-peer")

    def test_two_real_writer_paths_contend_for_same_owner_item(self):
        # Interleave the actual PowerShell prerequisite while the Python writer
        # holds the item; its fake native boundary receives that exact owner.
        def competing_prerequisite(owner):
            result = powershell_host_writer("prerequisites", holder=owner)
            self.assertEqual(result["writes"], 0, result)
            self.assertEqual(result["holder"], owner)
            self.assertNotIn("lock:release", result["events"])
        self.lease.after_acquire = competing_prerequisite
        self.apply()
        self.assertEqual(len(self.puts()), 1)
        # Reverse order: the real initializer leaves its lease after an uncertain
        # write. The narrow writer must not acquire, write, or release it.
        held = powershell_host_writer("initializer", fault="timeout")
        self.assertTrue(held["holder"])
        self.lease.after_acquire = None
        self.lease("acquire", held["holder"], "offline")
        self.aws.calls.clear()
        with self.assertRaises(self.subject.HostApplyError):
            self.apply()
        self.assertEqual(self.puts(), [])
        self.assertEqual(self.lease.item["owner"]["S"], held["holder"])

    def test_commit_timeout_and_postverify_errors_never_retry_or_rollback(self):
        for fault in ("commit-timeout", "commit-interrupt", "postverify", "inventory-drift", "duplicate", "missing", "context"):
            with self.subTest(fault=fault):
                self.aws.policy = copy.deepcopy(self.before)
                self.aws.calls.clear()
                self.lease.item = None
                self.aws.fault = fault
                with self.assertRaises(self.subject.HostApplyError) as caught:
                    self.apply()
                self.assertEqual(len(self.puts()), 1)
                self.assertEqual(caught.exception.report["lock_state"], "retained_until_ttl")
                self.assertEqual(caught.exception.report["mode"], "HOST_POLICY_INDETERMINATE")
                self.assertEqual(self.aws.policy, self.after)
                self.assertNotIn("must-not-print", json.dumps(caught.exception.report))
                self.aws.role["RoleId"] = "AROAOFFLINE"

    def test_lost_or_expired_lease_never_releases_peer_or_writes(self):
        for kind in ("lost", "expired"):
            with self.subTest(kind=kind):
                self.lease.item = None
                self.lease.actions.clear()
                def lose(owner):
                    if kind == "lost":
                        self.lease.item["owner"]["S"] = "peer"
                    else:
                        self.lease.item["ttl"]["N"] = "1"
                self.lease.after_acquire = lose
                with self.assertRaises(self.subject.HostApplyError):
                    self.apply()
                self.assertEqual(self.puts(), [])
                # Even pre-write failure must assert before attempting release.
                self.assertNotIn("release", self.lease.actions)

    def test_release_failure_is_not_success(self):
        self.lease.fail_release = True
        with self.assertRaises(self.subject.HostApplyError) as caught:
            self.apply()
        self.assertEqual(len(self.puts()), 1)
        self.assertEqual(caught.exception.report["lock_state"], "release_failed")

    def test_invalid_or_inherited_input_has_no_lock_or_iam_mutation(self):
        for key, value in (("ACADEMY_DEPLOY_LOCK_OWNER", "parent"), ("ACADEMY_RUNTIME_ENV_LOCK_OWNER", "parent"),
                           ("ACADEMY_DEPLOY_LOCK_TABLE", "wrong"), ("AWS_REGION", "us-east-1")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    self.apply()
        self.assertEqual(self.lease.actions, [])
        self.assertEqual(self.puts(), [])


    def test_default_plan_and_already_applied_verifier_are_read_only(self):
        result = self.subject.audit("offline")
        self.assertEqual(result["inventory_sha256"], self.hashes[2])
        self.assertEqual(result["before_sha256"], self.hashes[0])
        self.assertEqual(result["after_sha256"], self.hashes[1])
        self.assertTrue(result["proposed_simulation_pass"])
        self.aws.policy = copy.deepcopy(self.after)
        self.aws.calls.clear()
        result = self.subject.verify_host_boundary("offline", *self.hashes[1:])
        self.assertEqual(len(result["cases"]), 44)
        self.assertEqual(self.lease.actions, [])
        self.assertEqual(self.puts(), [])
        self.assertFalse(any("--policy-input-list" in args for args in self.aws.calls))

    def test_all_hash_gates_fail_before_put(self):
        original = self.hashes
        for index in range(3):
            with self.subTest(index=index):
                self.hashes = tuple("0" * 64 if position == index else value for position, value in enumerate(original))
                with self.assertRaises(self.subject.HostApplyError):
                    self.apply()
                self.assertEqual(self.puts(), [])
                self.assertIsNone(self.lease.item)
        with self.assertRaises(ValueError):
            self.subject.apply_host_boundary("offline", "", *original[1:])

    def test_unexpected_grants_trust_boundary_profile_and_instance_fail_before_put(self):
        original_call = self.aws.__call__
        variants = (
            (("iam", "list-role-policies"), lambda value: value["PolicyNames"].append("foreign")),
            (("iam", "list-attached-role-policies"), lambda value: value["AttachedPolicies"].append({"PolicyArn": "foreign"})),
            (("iam", "get-role"), lambda value: value["Role"].update(PermissionsBoundary={"PermissionsBoundaryArn": "foreign"})),
            (("iam", "get-role"), lambda value: value["Role"]["AssumeRolePolicyDocument"].update(Statement=[])),
            (("iam", "list-instance-profiles-for-role"), lambda value: value["InstanceProfiles"][0].update(Roles=[])),
            (("ec2", "describe-instances"), lambda value: value["Reservations"][0]["Instances"].append(copy.deepcopy(self.aws.instance))),
            (("ec2", "describe-instance-attribute"), lambda value: value.update(DisableApiTermination={"Value": False})),
            (("iam", "get-policy-version"), lambda value: value["PolicyVersion"]["Document"].update(Id="managed-drift")),
        )
        for operation, mutate in variants:
            def altered(args, profile):
                value = original_call(args, profile)
                if tuple(args[:2]) == operation:
                    mutate(value)
                return value
            with self.subTest(operation=operation), patch.object(self.subject, "aws", side_effect=altered):
                with self.assertRaises(self.subject.HostApplyError):
                    self.apply()
                self.assertEqual(self.puts(), [])
                self.assertIsNone(self.lease.item)

    def test_cli_modes_are_mutually_exclusive_and_require_reviewed_hashes(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for args in (["--apply-host-boundary"], ["--verify-host-boundary"],
                         ["--frontend-role-plan", "--apply-host-boundary"],
                         ["--expected-current-hash", "0" * 64]):
                with self.assertRaises(SystemExit) as caught:
                    self.subject.main(args)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.lease.actions, [])
        self.assertEqual(self.aws.calls, [])
        with patch.object(self.subject, "frontend_plan", return_value={"proposed_simulation_pass": True}) as frontend:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(self.subject.main(["--frontend-role-plan", "--aws-profile", "offline"]), 0)
            frontend.assert_called_once_with("offline")
        self.assertEqual(self.aws.calls, [])

    def test_native_iam_and_lock_use_same_profile_environment_without_retry(self):
        subject = load_script("converge_frontend_development_qa")
        secret_env = {"AWS_ACCESS_KEY_ID": "offline-marker", "AWS_SECRET_ACCESS_KEY": "offline-marker",
                      "AWS_SESSION_TOKEN": "offline-marker", "AWS_PROFILE": "foreign", "AWS_REGION": "us-east-1"}
        with patch.dict(os.environ, secret_env), patch.object(subject.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="{}", stderr="")) as native:
            subject.aws(["iam", "get-role", "--role-name", subject.ROLE], "offline")
            subject.lock_action("assert-owned", "local-owner", "offline")
            iam, lock = native.call_args_list
            self.assertEqual(iam.kwargs["env"], lock.kwargs["env"])
            env = lock.kwargs["env"]
            self.assertEqual(env["AWS_PROFILE"], "offline")
            self.assertEqual(env["AWS_REGION"], subject.REGION)
            self.assertEqual(env["AWS_DEFAULT_REGION"], subject.REGION)
            self.assertEqual(env["AWS_MAX_ATTEMPTS"], "1")
            self.assertNotIn("AWS_ACCESS_KEY_ID", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertNotIn("AWS_SESSION_TOKEN", env)
            self.assertEqual(os.environ["AWS_PROFILE"], "foreign", "parent environment must not change")
            self.assertEqual(iam.kwargs["timeout"], 30)
            self.assertEqual(lock.kwargs["timeout"], 30)
            self.assertIn(str(subject.LOCK_HELPER), lock.args[0])
            self.assertIn("--cli-read-timeout", iam.args[0])

    def test_same_owner_reacquire_and_stale_lease_fail_existing_algorithm(self):
        self.lease("acquire", "same-owner", "offline")
        with self.assertRaises(RuntimeError):
            self.lease("acquire", "same-owner", "offline")
        self.lease.item["ttl"]["N"] = "1"
        with self.assertRaises(RuntimeError):
            self.lease("renew", "same-owner", "offline")
        with self.assertRaises(RuntimeError):
            self.lease("assert-owned", "same-owner", "offline")

    def test_expired_operation_deadline_stops_before_mutation(self):
        with patch.object(self.subject.time, "monotonic", side_effect=[0, 601]):
            with self.assertRaises(self.subject.HostApplyError):
                self.apply()
        self.assertEqual(self.puts(), [])
        self.assertIsNone(self.lease.item)


class DevelopmentParameterBoundaryTests(unittest.TestCase):
    def test_cleanup_binds_creation_capability_and_refuses_other_runs_offline(self):
        session = json.loads((ROOT / "scripts/v1/templates/ssm/frontend_development_qa.json").read_text())
        shell_script = shlex.split(session["properties"]["linux"]["commands"], posix=True)[2]
        source = shell_script.split("<<'ACADEMY_QA_PY'\n", 1)[1].rsplit("ACADEMY_QA_PY", 1)[0]
        functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                     and node.name in {"ownership_payload", "assert_cleanup_owner"}]
        self.assertEqual(len(functions), 2, "missing server-side creation/cleanup ownership binding")
        namespace = {"hashlib": hashlib, "hmac": hmac, "re": re}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "fixed-ownership", "exec"), namespace)
        payload = namespace["ownership_payload"]
        check = namespace["assert_cleanup_owner"]
        first = "qa-ymath-realuse-fe-123-1-abcdef123456"
        other = "qa-ymath-realuse-fe-456-1-fedcba654321"
        owner = "a" * 64
        foreign = "b" * 64
        record = payload(first, 71, owner)
        destroyed = []

        def cleanup(code, tenant_id, capability, records):
            check(code, tenant_id, capability, records)
            destroyed.append(code)  # Local spy only; no Django, DB or AWS calls.

        for request in [(first, 71, foreign, [record]), (other, 72, owner, [record]),
                        (first, 72, owner, [record]), (first, 71, owner, []),
                        (first, 71, owner, [record, record]), (first, 71, owner, [{}])]:
            with self.assertRaises(PermissionError):
                cleanup(*request)
        self.assertEqual(destroyed, [])
        cleanup(first, 71, owner, [record])
        self.assertEqual(destroyed, [first])
        self.assertNotIn(owner, json.dumps(record))
        self.assertIn('OpsAuditLog.objects.create(', source)
        self.assertIn('assert_cleanup_owner(tenant, existing.pk, capability, records)', source)
        self.assertLess(source.index('assert_cleanup_owner(tenant, existing.pk, capability, records)'),
                        source.index('destroy=True'))

    def test_fixed_documents_bound_remote_session_lifetime_and_operation(self):
        directory = ROOT / "scripts/v1/templates/ssm"
        for filename, duration in [("frontend_development_qa.json", "5"),
                                   ("frontend_development_api_port.json", "25")]:
            document = json.loads((directory / filename).read_text())
            self.assertEqual(document.get("inputs"), {"maxSessionDuration": duration, "idleSessionTimeout": "5"})
        command = json.loads((directory / "frontend_development_qa.json").read_text())["properties"]["linux"]["commands"]
        self.assertIn("signal.alarm(180)", command)
        self.assertIn("timeout --kill-after=5s 210s docker exec", command)

    def test_existing_tenant_inspection_reads_owned_residue_inside_atomic(self):
        session = json.loads(
            (ROOT / "scripts/v1/templates/ssm/frontend_development_qa.json").read_text()
        )
        shell_script = shlex.split(
            session["properties"]["linux"]["commands"],
            posix=True,
        )[2]
        source = shell_script.split("<<'ACADEMY_QA_PY'\n", 1)[1].rsplit(
            "ACADEMY_QA_PY",
            1,
        )[0]
        functions = [
            node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
        ]
        namespace = {
            "hashlib": hashlib,
            "hmac": hmac,
            "re": re,
            "io": io,
            "json": json,
            "os": os,
            "django": SimpleNamespace(setup=Mock()),
        }
        exec(
            compile(
                ast.Module(body=functions, type_ignores=[]),
                "fixed-inspect-existing",
                "exec",
            ),
            namespace,
        )

        class AtomicReadback:
            depth = 0

            def __call__(self):
                return self

            def __enter__(self):
                self.depth += 1

            def __exit__(self, *_args):
                self.depth -= 1

        atomic = AtomicReadback()
        tenant = "qa-ymath-realuse-fe-456-1-fedcba654321"
        existing = SimpleNamespace(pk=72)
        command = Mock()
        command._remaining_for_code.return_value = {"tenants": 1, "users": 2}
        command._video_residue_for_code.return_value = {
            "active_playback_sessions": 0,
            "playback_events": 0,
            "playback_sessions": 0,
            "player_errors": 0,
            "proctored_video_accesses": 0,
            "video_accesses": 0,
            "video_progresses": 0,
            "videos": 0,
            "violated_events": 0,
        }
        command._exact_tenant_or_fail_on_case_variant.return_value = existing

        def owned_residue(subject):
            self.assertIs(subject, existing)
            if atomic.depth != 1:
                raise RuntimeError("owned residue read escaped transaction.atomic")
            return {"activity_audits": 0, "outstanding_tokens": 0}

        command._owned_database_residue.side_effect = owned_residue
        command._non_database_residue.return_value = {
            "listeners": 0,
            "processes": 0,
            "r2_objects": 0,
        }
        bucket_keys = (
            "R2_AI_BUCKET",
            "R2_STORAGE_BUCKET",
            "R2_ADMIN_BUCKET",
            "R2_VIDEO_BUCKET",
            "R2_EXCEL_BUCKET",
        )
        settings = SimpleNamespace(
            VIDEO_BATCH_JOB_QUEUE="",
            VIDEO_BATCH_JOB_DEFINITION="",
            TOOLS_SQS_QUEUE_NAME="academy-v1-development-tools-queue",
            MESSAGING_SQS_QUEUE_NAME="academy-v1-development-messaging-queue",
            DATABASES={
                "default": {
                    "NAME": "academy_api_development",
                    "USER": "academy_api_development_app",
                }
            },
            **{key: "academy-development-artifacts" for key in bucket_keys},
        )
        modules = {
            "django.conf": SimpleNamespace(settings=settings),
            "django.core.management": SimpleNamespace(call_command=Mock()),
            "django.db": SimpleNamespace(
                transaction=SimpleNamespace(atomic=atomic),
                connection=SimpleNamespace(cursor=Mock()),
            ),
            "apps.core.models": SimpleNamespace(OpsAuditLog=Mock()),
            "apps.core.management.commands.setup_ymath_realuse_scenario": SimpleNamespace(
                Command=lambda: command,
                assert_isolated_runtime=Mock(),
            ),
        }
        digest = "sha256:" + "b" * 64
        release = "sha-" + "a" * 40 + "-run-123-1"
        env = {
            "QA_ACTION": "Inspect",
            "QA_TENANT": tenant,
            "QA_CAPABILITY": "a" * 64,
            "QA_RELEASE": release,
            "QA_DIGEST": digest,
            "QA_SYNTHETIC_LONG_VIDEO": "false",
            "QA_IMAGE": (
                "809466760795.dkr.ecr.ap-northeast-2.amazonaws.com/academy-api@"
                + digest
            ),
            "DJANGO_SETTINGS_MODULE": "apps.api.config.settings.development",
            "ACADEMY_RUNTIME_ENV": "development",
            "ACADEMY_DEVELOPMENT_RELEASE_ID": release,
            "SOLAPI_MOCK": "true",
            "TOSS_AUTO_BILLING_ENABLED": "false",
            **{
                key: "academy-v1-development-ai-queue"
                for key in (
                    "AI_SQS_QUEUE_NAME_LITE",
                    "AI_SQS_QUEUE_NAME_BASIC",
                    "AI_SQS_QUEUE_NAME_PREMIUM",
                )
            },
        }
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True):
            result = namespace["run"]()

        self.assertEqual(result["status"], "DEVELOPMENT_QA_IDENTITY_PASS")
        self.assertEqual(result["remaining"], {"tenants": 1, "users": 2})
        self.assertEqual(result["residue"]["activity_audits"], 0)
        self.assertEqual(atomic.depth, 0)

    def test_fixed_video_state_contract_is_numeric_and_contains_no_identity_fields(self):
        session = json.loads(
            (ROOT / "scripts/v1/templates/ssm/frontend_development_qa.json").read_text()
        )
        shell_script = shlex.split(
            session["properties"]["linux"]["commands"],
            posix=True,
        )[2]
        source = shell_script.split("<<'ACADEMY_QA_PY'\n", 1)[1].rsplit(
            "ACADEMY_QA_PY",
            1,
        )[0]
        function = next(
            node
            for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "expected_video_state"
        )
        namespace = {}
        exec(
            compile(
                ast.Module(body=[function], type_ignores=[]),
                "fixed-video-state",
                "exec",
            ),
            namespace,
        )

        disabled = namespace["expected_video_state"](False)
        enabled = namespace["expected_video_state"](True)

        self.assertEqual(
            set(enabled),
            {
                "active_playback_sessions",
                "playback_events",
                "playback_sessions",
                "player_errors",
                "proctored_video_accesses",
                "video_accesses",
                "video_progresses",
                "videos",
                "violated_events",
            },
        )
        self.assertTrue(all(type(value) is int for value in enabled.values()))
        self.assertTrue(all(value == 0 for value in disabled.values()))
        self.assertEqual(enabled["videos"], 1)
        self.assertEqual(enabled["video_accesses"], 2)
        self.assertEqual(enabled["proctored_video_accesses"], 2)
        self.assertFalse(
            {"name", "phone", "username", "student_id", "user_id"} & set(enabled)
        )

    def test_actual_fixed_cleanup_control_flow_never_destroys_foreign_or_unowned_tenant(self):
        session = json.loads((ROOT / "scripts/v1/templates/ssm/frontend_development_qa.json").read_text())
        shell_script = shlex.split(session["properties"]["linux"]["commands"], posix=True)[2]
        source = shell_script.split("<<'ACADEMY_QA_PY'\n", 1)[1].rsplit("ACADEMY_QA_PY", 1)[0]
        functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)]
        namespace = {"hashlib": hashlib, "hmac": hmac, "re": re, "io": io, "json": json,
                     "os": os, "django": SimpleNamespace(setup=Mock())}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "fixed-actual-cleanup", "exec"), namespace)
        tenant = "qa-ymath-realuse-fe-456-1-fedcba654321"
        capability = "a" * 64
        foreign_owner = namespace["ownership_payload"](tenant, 72, "b" * 64)
        own_record = namespace["ownership_payload"](tenant, 72, capability)
        command = Mock()
        command._exact_tenant_or_fail_on_case_variant.return_value = SimpleNamespace(pk=72)
        command._remaining_for_code.return_value = {"tenants": 1, "users": 2}
        command._owned_database_residue.return_value = {
            "activity_audits": 0,
            "outstanding_tokens": 0,
        }
        command._non_database_residue.return_value = {
            "listeners": 0,
            "processes": 0,
            "r2_objects": 0,
        }
        video_zero = {
            "active_playback_sessions": 0,
            "playback_events": 0,
            "playback_sessions": 0,
            "player_errors": 0,
            "proctored_video_accesses": 0,
            "video_accesses": 0,
            "video_progresses": 0,
            "videos": 0,
            "violated_events": 0,
        }
        command._video_residue_for_code.return_value = video_zero
        cursor = MagicMock()
        audit = Mock()
        destroy = Mock(side_effect=lambda *args, **kwargs: kwargs["stdout"].write(json.dumps({
            "status": "YMATH_REALUSE_SCENARIO_DESTROYED", "tenant_code": tenant,
            "remaining": {"tenants": 0, "users": 0},
            "residue": {"activity_audits": 0, "outstanding_tokens": 0},
            "video_residue": video_zero,
        })))
        bucket_keys = ("R2_AI_BUCKET", "R2_STORAGE_BUCKET", "R2_ADMIN_BUCKET", "R2_VIDEO_BUCKET", "R2_EXCEL_BUCKET")
        settings = SimpleNamespace(
            VIDEO_BATCH_JOB_QUEUE="", VIDEO_BATCH_JOB_DEFINITION="",
            TOOLS_SQS_QUEUE_NAME="academy-v1-development-tools-queue",
            MESSAGING_SQS_QUEUE_NAME="academy-v1-development-messaging-queue",
            DATABASES={"default": {"NAME": "academy_api_development", "USER": "academy_api_development_app"}},
            **{key: "academy-development-artifacts" for key in bucket_keys})
        modules = {
            "django.conf": SimpleNamespace(settings=settings),
            "django.core.management": SimpleNamespace(call_command=destroy),
            "django.db": SimpleNamespace(transaction=SimpleNamespace(atomic=nullcontext),
                                         connection=SimpleNamespace(cursor=lambda: cursor)),
            "apps.core.models": SimpleNamespace(OpsAuditLog=SimpleNamespace(objects=audit)),
            "apps.core.management.commands.setup_ymath_realuse_scenario":
                SimpleNamespace(Command=lambda: command, assert_isolated_runtime=Mock()),
        }
        digest = "sha256:" + "b" * 64
        release = "sha-" + "a" * 40 + "-run-123-1"
        env = {"QA_ACTION": "Cleanup", "QA_TENANT": tenant, "QA_CAPABILITY": capability,
               "QA_RELEASE": release, "QA_DIGEST": digest,
               "QA_SYNTHETIC_LONG_VIDEO": "false",
               "QA_IMAGE": "809466760795.dkr.ecr.ap-northeast-2.amazonaws.com/academy-api@" + digest,
               "DJANGO_SETTINGS_MODULE": "apps.api.config.settings.development", "ACADEMY_RUNTIME_ENV": "development",
               "ACADEMY_DEVELOPMENT_RELEASE_ID": release, "SOLAPI_MOCK": "true", "TOSS_AUTO_BILLING_ENABLED": "false",
               **{key: "academy-v1-development-ai-queue" for key in
                  ("AI_SQS_QUEUE_NAME_LITE", "AI_SQS_QUEUE_NAME_BASIC", "AI_SQS_QUEUE_NAME_PREMIUM")}}
        with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True):
            for records in ([foreign_owner], [], [own_record, own_record]):
                audit.filter.return_value.values_list.return_value = records
                with self.assertRaises(PermissionError):
                    namespace["run"]()
                destroy.assert_not_called()
                audit.create.assert_not_called()
            audit.filter.return_value.values_list.return_value = [own_record]
            result = namespace["run"]()
            self.assertEqual(result["remaining"], {"tenants": 0, "users": 0})
            self.assertEqual(result["residue"], {
                "activity_audits": 0,
                "listeners": 0,
                "outstanding_tokens": 0,
                "processes": 0,
                "r2_objects": 0,
            })
            self.assertEqual(result["video_residue"], video_zero)
            self.assertEqual(destroy.call_count, 1)
            self.assertTrue(destroy.call_args.kwargs["destroy"])
            self.assertEqual(destroy.call_args.kwargs["tenant_code"], tenant)
            command._non_database_residue.assert_called_once_with(
                tenant_id=72,
                tenant_code=tenant,
            )
            command._exact_tenant_or_fail_on_case_variant.return_value = None
            command._remaining_for_code.return_value = {"tenants": 0, "users": 0}
            self.assertEqual(namespace["run"]()["status"], "YMATH_REALUSE_SCENARIO_ABSENT")
            self.assertEqual(destroy.call_count, 1, "absent cleanup must not call destroy")

    def test_managed_ssm_parameter_allow_is_explicitly_bounded(self):
        source = (ROOT / "scripts/v1/resources/iam.ps1").read_text(encoding="utf-8-sig")
        development = source.split("function Ensure-ApiDevelopmentIAM {", 1)[1].split(
            "function Legacy-GitHubActionsDeployIAM", 1
        )[0]
        self.assertIn("policy_api_development_parameter_boundary.json", development)
        self.assertIn("$runtimePolicy.Statement +=", development)

    def test_parameter_boundary_denies_recursion_history_and_unlisted_resources(self):
        self.assertTrue(BOUNDARY.exists(), "missing explicit development parameter deny")
        policy = json.loads(BOUNDARY.read_text(encoding="utf-8"))
        by_sid = {statement["Sid"]: statement for statement in policy["Statement"]}
        self.assertEqual(set(by_sid), {"DenyParameterReadsOutsideDevelopment", "DenyParameterEnumerationAndHistory"})
        outside = by_sid["DenyParameterReadsOutsideDevelopment"]
        self.assertEqual(outside["Effect"], "Deny")
        self.assertEqual(set(outside["Action"]), {"ssm:GetParameter", "ssm:GetParameters"})
        self.assertEqual(len(outside["NotResource"]), 5)
        for resource in outside["NotResource"]:
            self.assertNotIn("*", resource)
            self.assertIn("/development/", resource)
        enumeration = by_sid["DenyParameterEnumerationAndHistory"]
        self.assertEqual(enumeration["Effect"], "Deny")
        self.assertEqual(enumeration["Resource"], "*")
        self.assertEqual(set(enumeration["Action"]), {"ssm:GetParametersByPath", "ssm:GetParameterHistory"})

    def test_frontend_role_is_separate_main_only_and_has_no_global_command_output(self):
        directory = ROOT / "scripts/v1/templates/iam"
        trust = json.loads((directory / "trust_frontend_development_qa.json").read_text())
        statement, = trust["Statement"]
        self.assertEqual(statement["Action"], "sts:AssumeRoleWithWebIdentity")
        self.assertEqual(statement["Condition"]["StringEquals"], {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:guswls3028-art/academy-frontend:ref:refs/heads/main",
        })
        policy = json.loads((directory / "policy_frontend_development_qa.json").read_text())
        allows = [entry for entry in policy["Statement"] if entry["Effect"] == "Allow"]
        actions = {action for entry in allows for action in
                   (entry["Action"] if isinstance(entry["Action"], list) else [entry["Action"]])}
        self.assertNotIn("ssm:GetCommandInvocation", actions)
        self.assertNotIn("ssm:SendCommand", actions)
        self.assertFalse(any(action.startswith(("iam:Put", "iam:Create", "ssm:Put", "ssm:Update", "ec2:Run")) for action in actions))
        self.assertNotIn("academy-gha-ecr-build", json.dumps(policy))
        by_sid = {entry["Sid"]: entry for entry in policy["Statement"]}
        self.assertEqual(by_sid["StartVerifiedDevelopmentOnly"]["Condition"], {"StringEquals": {
            "ssm:resourceTag/Name": "academy-v1-api-development",
            "ssm:resourceTag/ManagedBy": "academy-api-development",
            "ssm:resourceTag/Environment": "development",
            "ssm:resourceTag/Lifecycle": "active",
        }})
        self.assertEqual(by_sid["StartFixedDocuments"]["Condition"], {
            "BoolIfExists": {"ssm:SessionDocumentAccessCheck": "true"},
        })
        self.assertEqual(by_sid["StartFixedDocuments"]["Resource"], [
            "arn:aws:ssm:ap-northeast-2:809466760795:document/academy-frontend-development-qa",
            "arn:aws:ssm:ap-northeast-2:809466760795:document/academy-frontend-development-api-port",
        ])
        self.assertEqual(by_sid["TerminateOwnedSessionOnly"]["Condition"]["StringEquals"], {
            "ssm:resourceTag/aws:ssmmessages:session-id": "${aws:userid}",
        })

    def test_document_condition_change_preserves_exact_policy_trust_and_documents(self):
        subject = load_script("converge_frontend_development_qa")
        policy = json.loads((ROOT / "scripts/v1/templates/iam/policy_frontend_development_qa.json").read_text())
        self.assertEqual(subject.policy_hash(policy),
                         "d48b3b3fe4e732e44b3a6230e2bfe74d327ff177ec4a25522c194c26775a6e98")
        condition = next(entry for entry in policy["Statement"] if entry["Sid"] == "StartFixedDocuments")["Condition"]
        condition["Bool"] = condition.pop("BoolIfExists")
        self.assertEqual(subject.policy_hash(policy),
                         "187f6ac218435d3b3f938d903153c5785db3529ace89f4e79ea9b6e1bde8ddb6")
        for path, expected in (
            ("iam/trust_frontend_development_qa.json", "aa2c1a60b63ad287c2e8caba7257beaafe5d602df66659c3093f917ad670713a"),
            ("ssm/frontend_development_qa.json", "7c0aa1f5b6793a4115dc1769bbe95811fa299aa26818e498e4d8bcfaea4378a2"),
            ("ssm/frontend_development_api_port.json", "974b6bf4e518533ee0ecd14c5e82b0a5f0538813e41253940cd46a6cb5e8d173"),
        ):
            with self.subTest(path=path):
                document = json.loads((ROOT / "scripts/v1/templates" / path).read_text())
                self.assertEqual(subject.policy_hash(document), expected)

    def test_fixed_session_document_has_no_command_parameter_or_interpolation_escape(self):
        directory = ROOT / "scripts/v1/templates/ssm"
        session = json.loads((directory / "frontend_development_qa.json").read_text())
        self.assertEqual(session["sessionType"], "NonInteractiveCommands")
        self.assertEqual(
            set(session["parameters"]),
            {
                "Action",
                "TenantCode",
                "ReleaseId",
                "ApiDigest",
                "OwnershipCapability",
                "SyntheticLongVideo",
            },
        )
        self.assertEqual(
            session["parameters"]["SyntheticLongVideo"],
            {
                "type": "String",
                "default": "false",
                "allowedValues": ["false", "true"],
                "allowedPattern": "^(false|true)$",
            },
        )
        for parameter in session["parameters"].values():
            for unsafe in ("'; touch /tmp/escape; '", "$(id)", "{{ssm:/academy/api/env}}", "x\ny", "../production"):
                self.assertIsNone(re.fullmatch(parameter["allowedPattern"], unsafe))
        command = session["properties"]["linux"]["commands"]
        self.assertEqual(set(re.findall(r"{{\s*([^}]+?)\s*}}", command)), set(session["parameters"]))
        agent_argv = shlex.split(command, posix=True)
        self.assertEqual(agent_argv[:2], ["/bin/sh", "-lc"])
        self.assertEqual(len(agent_argv), 3)
        shell_script = agent_argv[2]
        self.assertEqual(shell_script.splitlines()[0], "set -eu")
        self.assertNotEqual(agent_argv[0], "set")
        python = shell_script.split("<<'ACADEMY_QA_PY'\n", 1)[1].rsplit("ACADEMY_QA_PY", 1)[0]
        compile(python, "fixed-development-session", "exec")
        self.assertIn('assert existing is None', python)
        self.assertIn('payload["remaining"] == {"tenants": 0, "users": 0}', python)
        self.assertIn('payload["residue"] == {"activity_audits": 0, "outstanding_tokens": 0}', python)
        self.assertIn('payload["residue"] == {"activity_audits": 0, "outstanding_tokens": 0, "listeners": 0,', python)
        self.assertIn("student_count=2 if synthetic_long_video else 1", python)
        self.assertIn("synthetic_long_video=synthetic_long_video", python)
        self.assertIn('assert payload["video_residue"] == expected_video_state(False)', python)
        for residue_key in (
            "activity_audits",
            "listeners",
            "outstanding_tokens",
            "processes",
            "r2_objects",
        ):
            self.assertIn(f'"{residue_key}"', python)
        for video_key in (
            "active_playback_sessions",
            "playback_events",
            "playback_sessions",
            "player_errors",
            "proctored_video_accesses",
            "video_accesses",
            "video_progresses",
            "videos",
            "violated_events",
        ):
            self.assertIn(f'"{video_key}"', python)
        self.assertNotIn("reset=True", python)
        port = json.loads((directory / "frontend_development_api_port.json").read_text())
        self.assertEqual(port["properties"], {"portNumber": "8000", "localPortNumber": "18000", "type": "LocalPortForwarding"})
        self.assertNotIn("parameters", port)


class FrontendDocumentPlanTests(unittest.TestCase):
    def setUp(self):
        self.subject = load_script("converge_frontend_development_qa")
        self.requests = []

    def plan(self, responses=None, passed=False):
        self.requests.clear()

        def fake_aws(args, profile):
            self.assertEqual(profile, "offline")
            operation = tuple(args[:2])
            if operation == ("sts", "get-caller-identity"):
                return {"Account": self.subject.ACCOUNT}
            if operation == ("iam", "get-role"):
                raise RuntimeError("NoSuchEntity")
            if operation == ("ssm", "get-document"):
                raise RuntimeError("InvalidDocument")
            self.assertEqual(operation, ("iam", "simulate-custom-policy"))
            self.requests.append(copy.deepcopy(args))
            # Deliberately not an IAM evaluator: exercise the planner's request
            # matrix and prove it rejects an all-allowed simulation response.
            return {"EvaluationResults": [next(responses) if responses is not None
                                           else {"EvalDecision": "allowed"}]}

        with patch.object(self.subject, "aws", side_effect=fake_aws):
            result = self.subject.frontend_plan("offline")
        self.assertEqual(result["proposed_simulation_pass"], passed)
        self.assertEqual((result["iam_mutation"], result["ssm_document_mutation"],
                          result["parameter_value_reads"]), (0, 0, 0))
        for case, request in zip(result["cases"], self.requests, strict=True):
            sent = json.loads(request[request.index("--context-entries") + 1])
            self.assertEqual({item["ContextKeyName"]: item["ContextKeyValues"][0]
                              for item in sent}, case["context"])
        return result["cases"]

    def test_existing_frontend_inventory_requires_one_exact_inline_and_no_attached_grants(self):
        role = "academy-frontend-development-qa"
        for names, attached in (([role], []), ([], []), (["foreign"], []),
                                ([role, role], []), ([role, "foreign"], []),
                                ([role], [{"PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}])):
            responses = {
                ("sts", "get-caller-identity"): {"Account": self.subject.ACCOUNT},
                ("iam", "get-role"): {"Role": {"RoleName": role}},
                ("iam", "list-role-policies"): {"PolicyNames": names},
                ("iam", "list-attached-role-policies"): {"AttachedPolicies": attached},
            }
            expected = "role already exists" if names == [role] and not attached else "policy inventory"
            with self.subTest(names=names, attached=attached), patch.object(
                self.subject, "aws", side_effect=lambda args, profile: responses[tuple(args[:2])]
            ) as aws:
                with self.assertRaisesRegex(RuntimeError, expected):
                    self.subject.frontend_plan("offline")
                self.assertEqual([tuple(call.args[0][:2]) for call in aws.call_args_list], list(responses))
                for call in aws.call_args_list[1:]:
                    self.assertEqual(call.args, ([*call.args[0][:2], "--role-name", role], "offline"))

    def test_verdict_accepts_only_declared_omissions_and_rejects_wrong_decisions(self):
        # Stubbed verdicts test aggregation only, not IAM policy evaluation.
        cases = self.plan()
        key = "ssm:SessionDocumentAccessCheck"
        responses = [{"EvalDecision": case["expected"],
                      "MissingContextValues": [] if key in case["context"] else [key]}
                     for case in cases]
        self.plan(iter(responses), passed=True)
        for missing in ("unexpected-context", "ssm:resourceTag/Name"):
            with self.subTest(missing=missing):
                broken = copy.deepcopy(responses)
                broken[0]["MissingContextValues"] = [missing]
                self.plan(iter(broken))
        supplied = next(index for index, case in enumerate(cases) if key in case["context"])
        broken = copy.deepcopy(responses)
        broken[supplied]["MissingContextValues"] = [key]
        self.plan(iter(broken))
        denied = next(index for index, case in enumerate(cases) if case["expected"] == "implicitDeny")
        broken = copy.deepcopy(responses)
        broken[denied]["EvalDecision"] = "allowed"
        self.plan(iter(broken))

    def test_fixed_documents_have_missing_false_true_cases_without_context_refill(self):
        cases = self.plan()
        key = "ssm:SessionDocumentAccessCheck"
        prefix = "arn:aws:ssm:ap-northeast-2:809466760795:document/"
        for document in ("academy-frontend-development-qa", "academy-frontend-development-api-port"):
            with self.subTest(document=document):
                rows = [case for case in cases if case["resource"] == prefix + document
                        and case["action"] == "ssm:StartSession"]
                self.assertEqual({(case["context"].get(key), case["expected"]) for case in rows},
                                 {(None, "allowed"), ("false", "implicitDeny"), ("true", "allowed")})
                self.assertEqual(len(rows), 3)

    def test_default_and_public_shell_forwarding_documents_use_exact_negative_arns(self):
        cases = self.plan()
        public = "arn:aws:ssm:ap-northeast-2::document/"
        account = "arn:aws:ssm:ap-northeast-2:809466760795:document/"
        resources = [account + "SSM-SessionManagerRunShell", public + "SSM-SessionManagerRunShell",
                     account + "unapproved-session-negative-only"]
        resources += [public + name for name in (
            "AWS-StartInteractiveCommand", "AWS-StartSSHSession", "AWS-StartPortForwardingSession",
            "AWS-StartPortForwardingSessionToRemoteHost")]
        for resource in resources:
            with self.subTest(resource=resource):
                rows = [case for case in cases if case["resource"] == resource]
                self.assertEqual({(case["context"].get("ssm:SessionDocumentAccessCheck"), case["expected"])
                                  for case in rows},
                                 {(None, "implicitDeny"), ("false", "implicitDeny"), ("true", "implicitDeny")})
                self.assertEqual(len(rows), 3)
        self.assertFalse(any(":809466760795:document/AWS-" in case["resource"] for case in cases))

    def test_instance_cases_keep_all_four_tags_and_do_not_require_document_context(self):
        cases = self.plan()
        rows = [case for case in cases if case["action"] == "ssm:StartSession"
                and ":instance/" in case["resource"]]
        tags = {"ssm:resourceTag/Name": "academy-v1-api-development",
                "ssm:resourceTag/ManagedBy": "academy-api-development",
                "ssm:resourceTag/Environment": "development", "ssm:resourceTag/Lifecycle": "active"}
        allowed = [case for case in rows if case["expected"] == "allowed"]
        self.assertEqual(len(allowed), 1)
        self.assertNotIn("ssm:SessionDocumentAccessCheck", allowed[0]["context"])
        self.assertTrue(all(allowed[0]["context"][key] == value for key, value in tags.items()))
        for key, value in tags.items():
            self.assertTrue(any(case["expected"] == "implicitDeny" and case["context"].get(key) != value
                                for case in rows), key)


class DevelopmentVideoSigningContractTests(unittest.TestCase):
    def test_published_development_env_uses_an_isolated_video_signing_secret(self):
        source = (ROOT / "scripts/v1/publish-api-development-env.ps1").read_text()

        self.assertIn("New-Object byte[] 32", source)
        self.assertIn(
            "[System.Security.Cryptography.RandomNumberGenerator]::Fill($cdnSigningSecretBytes)",
            source,
        )
        self.assertNotIn("academy-development-cdn-signing:$credentialPassword", source)
        self.assertIn('CDN_HLS_BASE_URL = "https://cdn.hakwonplus.com"', source)
        self.assertRegex(
            source,
            r"CDN_HLS_SIGNING_SECRET\s*=\s*\$script:ApiDevelopmentCdnSigningSecret",
        )
        self.assertRegex(source, r'CDN_HLS_SIGNING_KEY_ID\s*=\s*"v1"')
        self.assertGreaterEqual(
            source.count('[string]$actual.CDN_HLS_SIGNING_SECRET -ne $script:ApiDevelopmentCdnSigningSecret'),
            1,
        )
        self.assertGreaterEqual(
            source.count('[string]$actualWorkers.CDN_HLS_SIGNING_SECRET -ne $script:ApiDevelopmentCdnSigningSecret'),
            1,
        )
        self.assertIn('[string]$actual.CDN_HLS_SIGNING_KEY_ID -ne "v1"', source)
        self.assertIn('[string]$actualWorkers.CDN_HLS_SIGNING_KEY_ID -ne "v1"', source)
        self.assertIn(
            "$script:ApiDevelopmentCdnSigningSecret -eq $productionCdnSigningSecret",
            source,
        )


if __name__ == "__main__":
    unittest.main()
