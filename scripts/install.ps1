$ErrorActionPreference = "Stop"
$ReleaseUrl = if ($env:WATSON_RELEASE_URL) { $env:WATSON_RELEASE_URL } else { "https://github.com/shaheedazaad/watson/archive/refs/tags/v0.2.0.zip" }
$InstallRoot = Join-Path $env:LOCALAPPDATA "WatsonApp"

if (-not (Get-Command pixi -ErrorAction SilentlyContinue)) {
    Invoke-Expression ((Invoke-WebRequest -UseBasicParsing https://pixi.sh/install.ps1).Content)
    $env:Path = "$env:USERPROFILE\.pixi\bin;$env:Path"
}

$StagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("watson-" + [System.Guid]::NewGuid().ToString("N"))
$Archive = Join-Path $StagingRoot "watson.zip"
$Expanded = Join-Path $StagingRoot "expanded"
New-Item -ItemType Directory -Force -Path $Expanded | Out-Null
Invoke-WebRequest -UseBasicParsing $ReleaseUrl -OutFile $Archive
Expand-Archive -Path $Archive -DestinationPath $Expanded
$SourceRoot = Get-ChildItem -Directory $Expanded | Select-Object -First 1
$PreviousRoot = Join-Path $StagingRoot "previous"
if (Test-Path $InstallRoot) { Move-Item -Path $InstallRoot -Destination $PreviousRoot }
Move-Item -Path $SourceRoot.FullName -Destination $InstallRoot

try {
    pixi install --manifest-path (Join-Path $InstallRoot "pixi.toml") --locked
    if ($LASTEXITCODE -ne 0) { throw "Pixi installation failed with exit code $LASTEXITCODE." }
} catch {
    Remove-Item -Path $InstallRoot -Recurse -Force
    if (Test-Path $PreviousRoot) { Move-Item -Path $PreviousRoot -Destination $InstallRoot }
    throw
}
Remove-Item -Path $StagingRoot -Recurse -Force
$LauncherDir = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps"
$Launcher = Join-Path $LauncherDir "watson.cmd"
"@echo off`r`npixi run --manifest-path `"$InstallRoot\pixi.toml`" watson %*" | Set-Content -Encoding ASCII $Launcher
Write-Host "Watson installed. Run: watson"
