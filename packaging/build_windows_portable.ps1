<#
.SYNOPSIS
Build a portable PDFCleaner Windows package with bundled Ghostscript binaries.

.DESCRIPTION
Creates a PyInstaller distribution that supports drag-and-drop invocation.
Default build is one-folder for stability with native dependencies.

.PARAMETER OneFile
Optional switch to build a single EXE. Use only after validating one-folder.
#>

[CmdletBinding()]
param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pyInstallerArgs = @(
    "--noconfirm"
    "--clean"
    "--name", "pdf_cleaner"
    "--add-binary", "bin\gswin64c.exe;bin"
    "--add-binary", "bin\gsdll64.dll;bin"
)

if ($OneFile) {
    $pyInstallerArgs += "--onefile"
}
else {
    $pyInstallerArgs += "--onedir"
}

$pyInstallerArgs += "pdf_cleaner.py"

python -m PyInstaller @pyInstallerArgs

if ($OneFile) {
    Write-Host "Build complete: dist\pdf_cleaner.exe"
}
else {
    Write-Host "Build complete: dist\pdf_cleaner\pdf_cleaner.exe"
}
