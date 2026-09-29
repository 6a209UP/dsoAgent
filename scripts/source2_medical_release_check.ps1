[CmdletBinding()]
param(
    [Parameter()]
    [string] $Source2Root,

    [Parameter()]
    [string] $JavaHome,

    [Parameter()]
    [switch] $PlanOnly,

    [Parameter()]
    [switch] $SkipFrontendBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-ExistingPath {
    param(
        [Parameter(Mandatory = $true)]
        [string] $LiteralPath,

        [Parameter(Mandatory = $true)]
        [ValidateSet('Leaf', 'Container')]
        [string] $PathType
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType $PathType)) {
        throw "Required $PathType path is missing: $LiteralPath"
    }
}

function Resolve-RequiredExecutable {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $Candidates
    )

    foreach ($candidate in $Candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $command) {
            return $command.Source
        }
    }
    throw "Required executable is unavailable: $($Candidates -join ', ')"
}

function Invoke-CheckedStep {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,

        [Parameter(Mandatory = $true)]
        [string] $Executable,

        [Parameter(Mandatory = $true)]
        [string[]] $ArgumentList,

        [Parameter(Mandatory = $true)]
        [string] $WorkingDirectory
    )

    Write-Host "[START] $Name"
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Executable @ArgumentList
        $stepExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($stepExitCode -ne 0) {
        throw "$Name failed with exit code $stepExitCode"
    }
    Write-Host "[PASS]  $Name"
}

function Assert-DocumentPair {
    param(
        [Parameter(Mandatory = $true)]
        [string] $MainDocument,

        [Parameter(Mandatory = $true)]
        [string] $CopyDocument
    )

    $mainHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $MainDocument).Hash
    $copyHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $CopyDocument).Hash
    if ($mainHash -ne $copyHash) {
        throw 'CowAgent and source2 integration documents are not byte-identical'
    }

    foreach ($document in @($MainDocument, $CopyDocument)) {
        $conflict = Select-String -LiteralPath $document -Pattern '^(<<<<<<<|=======|>>>>>>>)' | Select-Object -First 1
        if ($null -ne $conflict) {
            throw "Merge conflict marker found in integration document: $document"
        }
    }
    Write-Host "[PASS]  Integration documents match (SHA-256: $mainHash)"
}

$cowAgentRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($Source2Root)) {
    $workspaceRoot = Split-Path -Parent (Split-Path -Parent $cowAgentRoot)
    $Source2Root = Join-Path $workspaceRoot 'Medical Card\source2'
}

Assert-ExistingPath -LiteralPath $Source2Root -PathType Container
$source2ResolvedRoot = (Resolve-Path -LiteralPath $Source2Root).Path
$backendRoot = Join-Path $source2ResolvedRoot 'yudao-cloud'
$frontendRoot = Join-Path $source2ResolvedRoot 'yudao-ui-admin-vue3'
$uniAppRoot = Join-Path $source2ResolvedRoot 'yudao-mall-uniapp'
$mainDocument = Join-Path $cowAgentRoot 'docs\zh\development\source2-medical-chat-integration-plan.md'
$copyDocument = Join-Path $source2ResolvedRoot 'cowagent-medical-chat-integration-plan.md'
$migrationTest = Join-Path $backendRoot 'script\db\test-medical-cowagent-migration-runner.ps1'
$migrationRunner = Join-Path $backendRoot 'script\db\run-medical-cowagent-migrations.ps1'
$backendUpgradeTest = Join-Path $backendRoot 'script\db\test-medical-backend-upgrade-runner.ps1'
$backendUpgradeRunner = Join-Path $backendRoot 'script\db\run-medical-backend-upgrade.ps1'

$requiredFiles = @(
    $mainDocument,
    $copyDocument,
    (Join-Path $cowAgentRoot 'tests\test_agent_registry.py'),
    (Join-Path $cowAgentRoot 'tests\test_source2_integration.py'),
    (Join-Path $cowAgentRoot 'tests\test_source2_medical_preflight.py'),
    (Join-Path $cowAgentRoot 'tests\test_source2_medical_storage_preflight.py'),
    (Join-Path $cowAgentRoot 'tests\test_source2_medical_archive_admin.py'),
    (Join-Path $cowAgentRoot 'scripts\source2_medical_storage_preflight.py'),
    (Join-Path $backendRoot 'pom.xml'),
    (Join-Path $frontendRoot 'package.json'),
    (Join-Path $uniAppRoot 'scripts\verify-medical-api-contracts.mjs'),
    $migrationTest,
    $migrationRunner,
    $backendUpgradeTest,
    $backendUpgradeRunner,
    (Join-Path $backendRoot 'sql\mysql\medical-family-member-context-upgrade.sql'),
    (Join-Path $backendRoot 'sql\mysql\medical-recent-feature-upgrade.sql'),
    (Join-Path $backendRoot 'sql\mysql\system-user-global-username-upgrade.sql')
)
foreach ($requiredFile in $requiredFiles) {
    Assert-ExistingPath -LiteralPath $requiredFile -PathType Leaf
}

Assert-DocumentPair -MainDocument $mainDocument -CopyDocument $copyDocument

$python = Resolve-RequiredExecutable -Candidates @('python.exe', 'python')
$node = Resolve-RequiredExecutable -Candidates @('node.exe', 'node')
$maven = Resolve-RequiredExecutable -Candidates @('mvn.cmd', 'mvn')
$powerShell = Resolve-RequiredExecutable -Candidates @('pwsh.exe', 'pwsh')
$pnpm = Resolve-RequiredExecutable -Candidates @('pnpm.cmd', 'pnpm')

if (-not [string]::IsNullOrWhiteSpace($JavaHome)) {
    Assert-ExistingPath -LiteralPath $JavaHome -PathType Container
    $javaExecutable = Join-Path $JavaHome 'bin\java.exe'
    Assert-ExistingPath -LiteralPath $javaExecutable -PathType Leaf
    $env:JAVA_HOME = (Resolve-Path -LiteralPath $JavaHome).Path
    $javaBin = Join-Path $env:JAVA_HOME 'bin'
    $env:Path = "$javaBin;$($env:Path)"
}

$backendTests = @(
    'MedicalAiControllerTest',
    'MedicalAiMediaControllerTest',
    'AppChatControllerTest',
    'AppMessageControllerTest',
    'AppPatientControllerTest',
    'DoctorChatControllerTest',
    'DoctorPatientControllerTest',
    'MedicalAiChatTaskSchedulerTest',
    'CowAgentMedicalClientTest',
    'MedicalAiAdminServiceImplTest',
    'MedicalAiChatTaskServiceImplTest',
    'MedicalAiMediaTokenServiceTest',
    'MedicalAiSecretEncryptionKeyValidatorTest',
    'MedicalAiSecretTypeHandlerTest',
    'MedicalChatServiceImplTest',
    'MedicalChatMessageServiceImplTest',
    'MedicalDoctorServiceImplTest',
    'MedicalPatientServiceImplTest'
) -join ','

$steps = @(
    'CowAgent Agent isolation, source2 protocol and operations tests',
    'source2 CowAgent medical cross-project contracts',
    'source2 migration runner safety tests',
    'source2 migration package plan',
    'source2 full medical backend upgrade safety tests',
    'source2 full medical backend upgrade plan',
    'source2 medical AI backend regression tests',
    'source2 medical AI frontend type gate'
)
if (-not $SkipFrontendBuild) {
    $steps += 'source2 admin production build'
}

if ($PlanOnly) {
    Write-Host '[PLAN] No test, build, migration, network, or database command was executed.'
    foreach ($step in $steps) {
        Write-Host "[PLAN] $step"
    }
    exit 0
}

Invoke-CheckedStep -Name $steps[0] -Executable $python -WorkingDirectory $cowAgentRoot -ArgumentList @(
    '-m', 'pytest',
    'tests/test_agent_registry.py',
    'tests/test_source2_integration.py',
    'tests/test_source2_medical_preflight.py',
    'tests/test_source2_medical_storage_preflight.py',
    'tests/test_source2_medical_archive_admin.py',
    '-q'
)
Invoke-CheckedStep -Name $steps[1] -Executable $node -WorkingDirectory $uniAppRoot -ArgumentList @(
    'scripts/verify-medical-api-contracts.mjs', '--cowagent-medical-only'
)
Invoke-CheckedStep -Name $steps[2] -Executable $powerShell -WorkingDirectory $backendRoot -ArgumentList @(
    '-NoProfile', '-File', $migrationTest
)
Invoke-CheckedStep -Name $steps[3] -Executable $powerShell -WorkingDirectory $backendRoot -ArgumentList @(
    '-NoProfile', '-File', $migrationRunner, '-PlanOnly'
)
Invoke-CheckedStep -Name $steps[4] -Executable $powerShell -WorkingDirectory $backendRoot -ArgumentList @(
    '-NoProfile', '-File', $backendUpgradeTest
)
Invoke-CheckedStep -Name $steps[5] -Executable $powerShell -WorkingDirectory $backendRoot -ArgumentList @(
    '-NoProfile', '-File', $backendUpgradeRunner, '-PlanOnly'
)
Invoke-CheckedStep -Name $steps[6] -Executable $maven -WorkingDirectory $backendRoot -ArgumentList @(
    '-q',
    '-pl', 'yudao-module-medical/yudao-module-medical-server',
    '-am',
    '-DskipTests=false',
    '-Dnet.bytebuddy.experimental=true',
    "-Dtest=$backendTests",
    '-Dsurefire.failIfNoSpecifiedTests=false',
    'test'
)
Invoke-CheckedStep -Name $steps[7] -Executable $pnpm -WorkingDirectory $frontendRoot -ArgumentList @(
    'ts:check:medical-ai'
)
if (-not $SkipFrontendBuild) {
    Invoke-CheckedStep -Name $steps[8] -Executable $pnpm -WorkingDirectory $frontendRoot -ArgumentList @(
        'build:prod'
    )
}

Write-Host '[PASS]  All selected local release checks completed successfully.'
