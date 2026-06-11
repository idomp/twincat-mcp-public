<#
.SYNOPSIS
  Builds TcAutomation.exe on a machine with TwinCAT XAE / TcXaeShell but NO Visual Studio.

.DESCRIPTION
  The normal build (setup.bat / scripts/build.ps1) requires Visual Studio or VS Build Tools.
  This alternative uses the MSBuild that ships inside TcXaeShell plus NuGet-provided Roslyn,
  .NET 4.7.2 reference assemblies, and EnvDTE PIAs, and a pre-generated TCatSysManagerLib
  interop -- enabled by the opt-in /p:NoVisualStudioBuild=true overlay
  (TcAutomation/Directory.Build.props + .targets). See README "Build without Visual Studio".

  Produces TcAutomation\bin\Release\TcAutomation.exe.
#>
[CmdletBinding()]
param(
    [string]$Configuration = 'Release',
    [string]$Platform = 'x64'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$proj = Join-Path $repo 'TcAutomation\TcAutomation.csproj'

# 1. Locate an MSBuild >= 15 (TcXaeShell ships 15.9; VS Build Tools also fine if present).
$msbuildCandidates = @(
    'C:\Program Files (x86)\Beckhoff\TcXaeShell\MSBuild\15.0\Bin\MSBuild.exe',
    'C:\Program Files (x86)\Beckhoff\TcXaeShell\Common7\IDE\..\..\MSBuild\Current\Bin\MSBuild.exe'
)
$msbuild = $msbuildCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $msbuild) { throw "No MSBuild found. Expected TcXaeShell's MSBuild at $($msbuildCandidates[0])." }
Write-Host "Using MSBuild: $msbuild" -ForegroundColor Gray

# 2. Generate the TCatSysManagerLib interop (needs Windows PowerShell 5.1) if missing.
$interop = Join-Path $repo 'TcAutomation\interop\Interop.TCatSysManagerLib.dll'
if (-not (Test-Path $interop)) {
    Write-Host "Generating COM interop (no Windows SDK present)..." -ForegroundColor Yellow
    $gen = Join-Path $repo 'TcAutomation\gen-interop.ps1'
    & "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -File $gen
    if (-not (Test-Path $interop)) { throw "Interop generation failed: $interop not produced." }
}

# 3. Restore + build with the opt-in overlay enabled.
& $msbuild $proj /t:Restore /p:RestoreForce=true /p:NoVisualStudioBuild=true /p:Configuration=$Configuration /p:Platform=$Platform /v:minimal
if ($LASTEXITCODE -ne 0) { throw "Restore failed ($LASTEXITCODE)." }
& $msbuild $proj /p:NoVisualStudioBuild=true /p:Configuration=$Configuration /p:Platform=$Platform /v:minimal
if ($LASTEXITCODE -ne 0) { throw "Build failed ($LASTEXITCODE)." }

$exe = Join-Path $repo "TcAutomation\bin\$Configuration\TcAutomation.exe"
if (Test-Path $exe) { Write-Host "`n[OK] Built $exe" -ForegroundColor Green }
else { throw "Build reported success but $exe is missing." }
