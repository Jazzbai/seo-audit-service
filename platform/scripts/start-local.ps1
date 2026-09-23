param([switch]$NoBuild)
$ErrorActionPreference = 'Stop'
$candidateRoot = Split-Path -Parent $PSScriptRoot
$candidateEnv = Join-Path $candidateRoot '.env.local-candidate'
$candidateCompose = Join-Path $candidateRoot 'compose.yaml'

# Independent volumes, fixed local ports, no access to legacy environment files.
# Preserve generated secrets on every subsequent launch.
function New-CandidateSecret {
    $bytes = New-Object byte[] 48
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop, then run this command again.' }
if (-not (Test-Path -LiteralPath $candidateEnv)) {
    # Never replace secrets for pre-existing volumes: losing the encryption key
    # would make saved connections unreadable.
    $existingVolumes = docker volume ls --filter 'label=com.docker.compose.project=forgeseo-platform-local' -q
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect local candidate volumes.' }
    if ($existingVolumes) { throw 'Candidate volumes already exist but the private env file is missing. Restore .env.local-candidate; do not regenerate its encryption key.' }
    $content = @(
        "DB_PASSWORD=$(New-CandidateSecret)",
        "QUEUE_PASSWORD=$(New-CandidateSecret)",
        "ENCRYPTION_KEY=$(New-CandidateSecret)",
        "BOOTSTRAP_TOKEN=$(New-CandidateSecret)",
        'PUBLIC_URL=http://localhost:18080',
        'APP_ADDRESS=http://:80',
        'COOKIE_SECURE=false',
        'HTTP_PORT=127.0.0.1:18080',
        'HTTPS_PORT=127.0.0.1:18443'
    )
    # CreateNew refuses an accidental overwrite, including a competing launch.
    $stream = [System.IO.File]::Open($candidateEnv, 'CreateNew', 'Write', 'None')
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($content -join "`n") + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    icacls $candidateEnv /inheritance:r /grant:r "${identity}:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Restrict access to .env.local-candidate before continuing.' }
}
$composeArguments = @('compose', '--env-file', $candidateEnv, '-f', $candidateCompose, '-p', 'forgeseo-platform-local')
if (-not $NoBuild) {
    # Resolve image IDs afresh after building. Some Compose versions retain
    # the pre-build image ID when `up --build` is used in one invocation.
    & docker @composeArguments build
    if ($LASTEXITCODE -ne 0) { throw 'Candidate build failed; existing containers and volumes were preserved.' }
}
& docker @composeArguments up -d --wait --wait-timeout 300
if ($LASTEXITCODE -ne 0) { throw 'Local candidate did not become healthy. Existing volumes were preserved; inspect Compose status/logs and rerun after correcting the failure.' }
Write-Host 'Open http://localhost:18080'
Write-Host "First visit: create your owner account using BOOTSTRAP_TOKEN from $candidateEnv"
Write-Host 'Later visits: sign in with the email and password you chose. All automated writes start paused.'
Write-Host 'Keep the private env file with your backups; do not share it or replace it.'
