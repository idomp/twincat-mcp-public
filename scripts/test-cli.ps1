# Test TcAutomation.exe directly
# Usage: .\scripts\test-cli.ps1 -Solution "C:\path\to\solution.sln"

param(
    [Parameter(Mandatory=$true)]
    [string]$Solution,
    
    [Parameter()]
    [ValidateSet("build", "info")]
    [string]$Command = "build"
)

$ErrorActionPreference = "Stop"

# Find the executable
# Classic .NET Framework build output: bin\<Config>\TcAutomation.exe (no TFM subfolder).
$exePaths = @(
    "$PSScriptRoot\..\TcAutomation\bin\Release\TcAutomation.exe",
    "$PSScriptRoot\..\TcAutomation\bin\Debug\TcAutomation.exe",
    "$PSScriptRoot\..\TcAutomation\publish\TcAutomation.exe"
)

$exe = $null
foreach ($path in $exePaths) {
    if (Test-Path $path) {
        $exe = Resolve-Path $path
        break
    }
}

if (-not $exe) {
    Write-Host "❌ TcAutomation.exe not found. Run build.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Using: $exe" -ForegroundColor Cyan
Write-Host "Command: $Command" -ForegroundColor Cyan
Write-Host "Solution: $Solution" -ForegroundColor Cyan
Write-Host ""

& $exe $Command --solution $Solution
