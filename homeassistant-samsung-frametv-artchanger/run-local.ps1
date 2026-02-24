param(
  [string]$TvIps = "192.168.10.170",
  [string]$MediaDir = "$PSScriptRoot\\standalone-media",
  [string]$DataDir = "$PSScriptRoot\\standalone-data",
  [string]$AutomationToken = "test-token",
  [int]$Port = 8099
)

$pythonExe = Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path $pythonExe)) {
  $pythonExe = "python"
}

New-Item -ItemType Directory -Force -Path $MediaDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$env:TV_IPS = $TvIps
$env:MEDIA_DIR = $MediaDir
$env:DATA_DIR = $DataDir
$env:AUTOMATION_TOKEN = $AutomationToken

Write-Host "Starting Frame TV Art Changer locally"
Write-Host "TV_IPS=$($env:TV_IPS)"
Write-Host "MEDIA_DIR=$($env:MEDIA_DIR)"
Write-Host "DATA_DIR=$($env:DATA_DIR)"
Write-Host "PORT=$Port"

& $pythonExe -m uvicorn app.main:app --host 0.0.0.0 --port $Port
