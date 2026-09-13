param(
  [string]$Version = $(if ($env:SOPS_VERSION) { $env:SOPS_VERSION } else { "v3.13.3" }),
  [string]$InstallDir = $(Join-Path (Split-Path -Parent $PSScriptRoot) ".tools\bin"),
  [string]$ReleaseBase = $(if ($env:SOPS_RELEASE_BASE) { $env:SOPS_RELEASE_BASE } else { "" })
)

$ErrorActionPreference = "Stop"
# PowerShell installer twin of scripts/install-sops (bash): installs into the
# project-local .tools\bin, never system paths; config-encrypt.ps1 and
# apply-encrypted-config.ps1 pick it up automatically.

$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$arch = switch ($env:PROCESSOR_ARCHITECTURE) {
  "AMD64" { "amd64" }
  "ARM64" { "arm64" }
  default { throw "unsupported architecture: $($env:PROCESSOR_ARCHITECTURE)" }
}

$asset = "sops-$Version.$arch.exe"
$target = Join-Path $InstallDir "sops.exe"
$base = if ($ReleaseBase) { $ReleaseBase } else { "https://github.com/getsops/sops/releases/download/$Version" }

if (Test-Path $target) {
  try {
    $out = & $target --version 2>&1 | Out-String
    if ($out -match [regex]::Escape($Version.TrimStart("v"))) {
      Write-Host "sops $Version already installed at $target"
      return
    }
  } catch { }
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "sops-install-$PID"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile (Join-Path $tmp "sops.exe")
  Invoke-WebRequest -UseBasicParsing -Uri "$base/sops-$Version.checksums.txt" -OutFile (Join-Path $tmp "checksums.txt")

  # checksums.txt is sha256sum-formatted; match the exact asset name so
  # similarly prefixed artifacts (e.g. *.spdx.sbom.json) cannot match.
  $expected = $null
  foreach ($line in Get-Content (Join-Path $tmp "checksums.txt")) {
    $parts = $line -split "\s+", 2
    if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $asset) {
      $expected = $parts[0].Trim().ToLower()
      break
    }
  }
  if (-not $expected) { throw "asset $asset not found in checksums.txt" }
  $actual = (Get-FileHash -Algorithm SHA256 (Join-Path $tmp "sops.exe")).Hash.ToLower()
  if ($actual -ne $expected) { throw "sha256 mismatch for $asset (expected $expected, got $actual)" }

  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  Copy-Item -Force (Join-Path $tmp "sops.exe") $target
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

try { & $target --version } catch { }
Write-Host "installed sops $Version -> $target"
