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

function Resolve-SopsPath {
  param([string]$Bin)
  if (Test-Path $Bin) { return (Resolve-Path $Bin).Path }
  $cmd = Get-Command $Bin -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($c in @(
      (Join-Path $Root ".tools\gobin\sops.exe"),
      (Join-Path $Root ".tools\sops.exe"),
      (Join-Path $Root ".tools\sops")
    )) {
    if (Test-Path $c) { return (Resolve-Path $c).Path }
  }
  return $null
}

function Invoke-SopsRaw {
  param(
    [Parameter(Mandatory = $true)][string]$SopsPath,
    [Parameter(Mandatory = $true)][string]$Arguments,
    [hashtable]$ExtraEnv = @{}
  )

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $SopsPath
  $psi.Arguments = $Arguments
  $psi.WorkingDirectory = $Root
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  # Critical on Chinese Windows: do not decode sops stdout as system ANSI/GBK.
  $utf8 = New-Object System.Text.UTF8Encoding $false
  $psi.StandardOutputEncoding = $utf8
  $psi.StandardErrorEncoding = $utf8
  foreach ($key in $ExtraEnv.Keys) {
    $psi.Environment[$key] = [string]$ExtraEnv[$key]
  }

  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  [void]$p.Start()

  # Read raw bytes from stdout to preserve UTF-8 config content exactly.
  $stdoutStream = $p.StandardOutput.BaseStream
  $ms = New-Object System.IO.MemoryStream
  $stdoutStream.CopyTo($ms)
  $err = $p.StandardError.ReadToEnd()
  $p.WaitForExit()

  if ($p.ExitCode -ne 0) {
    throw "sops failed (exit $($p.ExitCode)): $err"
  }
  if ($err -and ($err -notmatch 'a new version of sops')) {
    Write-Host $err
  }
  return $ms.ToArray()
}

if (-not (Test-Path $Ciphertext)) { throw "encrypted config not found: $Ciphertext" }

$sopsPath = Resolve-SopsPath -Bin $SopsBin
if (-not $sopsPath) { throw "sops not found" }

if (-not $AgeKeyFile) {
  $AgeKeyFile = Join-Path $Root ".tools\notifybot.agekey"
}
if (-not (Test-Path $AgeKeyFile)) {
  throw "age private key not found: $AgeKeyFile"
}

$env:SOPS_AGE_KEY_FILE = $AgeKeyFile
$bytes = Invoke-SopsRaw -SopsPath $sopsPath -Arguments "--decrypt --input-type yaml --output-type binary `"$Ciphertext`"" -ExtraEnv @{ SOPS_AGE_KEY_FILE = $AgeKeyFile }

$utf8 = New-Object System.Text.UTF8Encoding $false
$text = $utf8.GetString($bytes)
if ($text -notmatch 'ws_url:' -or $text -notmatch 'ws_token:') {
  throw "decoded config missing required markers"
}

$target = if ([System.IO.Path]::IsPathRooted($Plaintext)) { $Plaintext } else { Join-Path $Root $Plaintext }
$targetDir = Split-Path -Parent $target
if ($targetDir -and -not (Test-Path $targetDir)) {
  New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
}

$tmp = "$target.tmp.$PID"
[System.IO.File]::WriteAllBytes($tmp, $bytes)
Move-Item -Force $tmp $target
Write-Host "applied encrypted config -> $Plaintext"

if ($WaitReload) {
  $status = Join-Path $Root "archives/reload-status.json"
  $deadline = (Get-Date).AddSeconds($WaitSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Path $status) {
      $data = Get-Content $status -Raw -Encoding utf8 | ConvertFrom-Json
      if ($data.ok) {
        Write-Host "reload status ok"
        return
      }
    }
    Start-Sleep -Seconds 2
  }
  throw "timed out waiting for reload status"
}