# Build the Windows installer with Inno Setup 6.
$ErrorActionPreference = "Stop"
$Iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Iscc)) {
    $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path $Iscc)) { throw "ISCC.exe not found; install Inno Setup 6" }
& $Iscc (Join-Path $PSScriptRoot "OpenVSP-AI-R2.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed: $LASTEXITCODE" }
Write-Host "Installer written under dist\installer"
