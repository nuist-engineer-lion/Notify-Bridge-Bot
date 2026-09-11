param(
  [string]$Plaintext = "config.yaml",
  [string]$Ciphertext = "config.yaml.enc",
  [string]$SopsBin = $(if ($env:SOPS_BIN) { $env:SOPS_BIN } else { "sops" })
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
    [Parameter(Mandatory = $true)][string]$Arguments
  )

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $SopsPath
  $psi.Arguments = $Arguments
  $psi.WorkingDirectory = $Root
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $utf8 = New-Object System.Text.UTF8Encoding $false
  $psi.StandardOutputEncoding = $utf8
  $psi.StandardErrorEncoding = $utf8

  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  [void]$p.Start()

  $ms = New-Object System.IO.MemoryStream
  $p.StandardOutput.BaseStream.CopyTo($ms)
  $err = $p.StandardError.ReadToEnd()
  $p.WaitForExit()

  if ($p.ExitCode -ne 0) {
    throw "sops failed (exit $($p.ExitCode)): $err"
  }
  return $ms.ToArray()
}

if (-not (Test-Path $Plaintext)) {
  throw "plaintext config not found: $Plaintext (copy config.example.yaml to config.yaml first)"
}
if (-not (Test-Path .sops.yaml)) {
  throw ".sops.yaml missing"
}

$sopsPath = Resolve-SopsPath -Bin $SopsBin
if (-not $sopsPath) {
  throw "sops not found. Install sops or set SOPS_BIN / place binary at .tools/gobin/sops.exe"
}

$bytes = Invoke-SopsRaw -SopsPath $sopsPath -Arguments "--encrypt --input-type binary --output-type yaml `"$Plaintext`""
$target = if ([System.IO.Path]::IsPathRooted($Ciphertext)) { $Ciphertext } else { Join-Path $Root $Ciphertext }
$tmp = "$target.tmp.$PID"
[System.IO.File]::WriteAllBytes($tmp, $bytes)
Move-Item -Force $tmp $target
Write-Host "wrote $Ciphertext"