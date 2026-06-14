# Publish TcAutomation.exe for distribution
# Usage: .\scripts\publish.ps1
#
# TcAutomation is a classic .NET Framework 4.7.2 project with COM references,
# so it MUST be built with MSBuild.exe (via build.ps1), NOT the dotnet CLI —
# dotnet cannot resolve COMReferences (MSB4803). "Publish" here simply builds
# Release and stages the output into a publish\ folder.

$ErrorActionPreference = "Stop"

Write-Host "Publishing TcAutomation..." -ForegroundColor Cyan

# Build (Release) via the MSBuild-based build script.
& "$PSScriptRoot\build.ps1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Build failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

$releaseDir = Join-Path $PSScriptRoot "..\TcAutomation\bin\Release"
$publishDir = Join-Path $PSScriptRoot "..\TcAutomation\publish"
$exe = Join-Path $releaseDir "TcAutomation.exe"

if (-not (Test-Path $exe)) {
    Write-Host "❌ TcAutomation.exe not found in $releaseDir after build" -ForegroundColor Red
    exit 1
}

# Stage the Release output (exe + dependencies) into publish\.
if (Test-Path $publishDir) { Remove-Item $publishDir -Recurse -Force }
New-Item -ItemType Directory -Path $publishDir | Out-Null
Copy-Item (Join-Path $releaseDir "*") $publishDir -Recurse -Force

Write-Host "✅ Publish succeeded!" -ForegroundColor Green
Write-Host "Executable: TcAutomation\publish\TcAutomation.exe"
Write-Host "`nPublished files:" -ForegroundColor Yellow
Get-ChildItem $publishDir | Format-Table Name, Length -AutoSize
