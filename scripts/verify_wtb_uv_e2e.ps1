param(
    [string]$UvVenvManagerPath = (Join-Path $PSScriptRoot "..\..\uv_venv_manager"),
    [string]$RestUrl = "http://127.0.0.1:10900",
    [string]$GrpcUrl = "localhost:50051",
    [string]$PythonExe = "",
    [switch]$SkipDockerBuild
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Action
}

function Wait-RestReady {
    param([string]$BaseUrl)

    $openApiUrl = "$BaseUrl/openapi.json"
    for ($i = 1; $i -le 30; $i++) {
        try {
            Invoke-WebRequest -Uri $openApiUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
            return
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }

    throw "REST API did not become ready at $openApiUrl"
}

function Wait-TcpReady {
    param(
        [string]$Endpoint,
        [int]$TimeoutSeconds = 60
    )

    $parts = $Endpoint.Split(":")
    if ($parts.Length -ne 2) {
        throw "Expected host:port endpoint, got $Endpoint"
    }

    $hostName = $parts[0]
    $port = [int]$parts[1]
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $task = $client.ConnectAsync($hostName, $port)
            if ($task.Wait(2000) -and $client.Connected) {
                return
            }
        }
        catch {
            Start-Sleep -Seconds 1
        }
        finally {
            $client.Dispose()
        }
    }

    throw "TCP endpoint did not become ready at $Endpoint"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uvRepo = (Resolve-Path $UvVenvManagerPath).Path
$composeFile = Join-Path $uvRepo "docker-compose.yml"
$defaultPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not $PythonExe) {
    if (Test-Path $defaultPython) {
        $PythonExe = $defaultPython
    }
    else {
        $PythonExe = "python"
    }
}

if (-not (Test-Path $composeFile)) {
    throw "docker-compose.yml not found at $composeFile"
}

Invoke-Step "Build uv_venv_manager Docker image" {
    if ($SkipDockerBuild) {
        Write-Host "Skipped by -SkipDockerBuild"
        return
    }

    Push-Location $uvRepo
    try {
        docker compose build
    }
    finally {
        Pop-Location
    }
}

Invoke-Step "Start uv_venv_manager compose services" {
    Push-Location $uvRepo
    try {
        docker compose up -d
        docker compose ps
    }
    finally {
        Pop-Location
    }
}

Invoke-Step "Wait for REST and gRPC endpoints" {
    Wait-RestReady -BaseUrl $RestUrl
    Wait-TcpReady -Endpoint $GrpcUrl
}

Invoke-Step "Run strict Ray batch pytest" {
    Push-Location $repoRoot
    try {
        $env:UV_CACHE_DIR = ".uv-cache"
        & $PythonExe -m pytest tests\integration\test_ray_batch.py -q
    }
    finally {
        Pop-Location
    }
}

Invoke-Step "Run WTB install checker against uv_venv_manager gRPC" {
    Push-Location $repoRoot
    try {
        $env:UV_CACHE_DIR = ".uv-cache"
        & $PythonExe install_checker.py --grpc-url $GrpcUrl
    }
    finally {
        Pop-Location
    }
}

Invoke-Step "Docker compose status" {
    Push-Location $uvRepo
    try {
        docker compose ps
    }
    finally {
        Pop-Location
    }
}
