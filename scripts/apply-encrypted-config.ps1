param(
  [string]$Ciphertext = "config.yaml.enc",
  [string]$Plaintext = "config.yaml",
  [string]$AgeKeyFile = $(if ($env:SOPS_AGE_KEY_FILE) { $env:SOPS_AGE_KEY_FILE } elseif ($env:AGE_KEY_FILE) { $env:AGE_KEY_FILE } else { "" }),
  [string]$SopsBin = $(if ($env:SOPS_BIN) { $env:SOPS_BIN } else { "sops" }),
  [switch]$WaitReload,
  [int]$WaitSeconds = 90
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path $Ciphertext)) { throw "encrypted config not found: $Ciphertext" }

if (-not (Get-Command $SopsBin -ErrorAction SilentlyContinue) -and -not (Test-Path $SopsBin)) {
  foreach ($c in @((Join-Path $Root ".tools\gobin\sops.exe"), (Join-Path $Root ".tools\sops.exe"), (Join-Path $Root ".tools\sops"))) {
    if (Test-Path $c) { $SopsBin = $c; break }
  }
}
if (-not (Get-Command $SopsBin -ErrorAction SilentlyContinue) -and -not (Test-Path $SopsBin)) {
  throw "sops not found"
}

if (-not $AgeKeyFile) {
  $AgeKeyFile = Join-Path $Root ".tools\notifybot.agekey"
}
if (-not (Test-Path $AgeKeyFile)) {
  throw "age private key not found: $AgeKeyFile"
}

$env:SOPS_AGE_KEY_FILE = $AgeKeyFile
$sopsPath = if (Test-Path $SopsBin) { (Resolve-Path $SopsBin).Path } else { (Get-Command $SopsBin).Source }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $sopsPath
$psi.Arguments = "--decrypt --input-type yaml --output-type binary `"$Ciphertext`""
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.Environment["SOPS_AGE_KEY_FILE"] = $AgeKeyFile
$p = [System.Diagnostics.Process]::Start($psi)
$out = $p.StandardOutput.ReadToEnd()
$err = $p.StandardError.ReadToEnd()
$p.WaitForExit()
if ($p.ExitCode -ne 0) { throw "sops decrypt failed: $err" }

if ($out -notmatch 'ws_url:' -or $out -notmatch 'ws_token:') {
  throw "decoded config missing required markers"
}

$oldMtime = if (Test-Path $Plaintext) { (Get-Item $Plaintext).LastWriteTimeUtc.Ticks } else { 0 }
[System.IO.File]::WriteAllText((Join-Path $Root $Plaintext), $out)
Write-Host "applied encrypted config -> $Plaintext"

if ($WaitReload) {
  $status = Join-Path $Root "archives/reload-status.json"
  $deadline = (Get-Date).AddSeconds($WaitSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Path $status) {
      $data = Get-Content $status -Raw | ConvertFrom-Json
      if ($data.ok) {
        Write-Host "reload status ok"
        return
      }
    }
    Start-Sleep -Seconds 2
  }
  throw "timed out waiting for reload status"
}