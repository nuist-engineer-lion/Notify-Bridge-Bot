param(
  [string]$Plaintext = "config.yaml",
  [string]$Ciphertext = "config.yaml.enc",
  [string]$SopsBin = $(if ($env:SOPS_BIN) { $env:SOPS_BIN } else { "sops" })
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path $Plaintext)) {
  throw "plaintext config not found: $Plaintext (copy config.example.yaml to config.yaml first)"
}
if (-not (Test-Path .sops.yaml)) {
  throw ".sops.yaml missing"
}

if (-not (Get-Command $SopsBin -ErrorAction SilentlyContinue) -and -not (Test-Path $SopsBin)) {
  foreach ($c in @((Join-Path $Root ".tools\gobin\sops.exe"), (Join-Path $Root ".tools\sops.exe"), (Join-Path $Root ".tools\sops"))) {
    if (Test-Path $c) { $SopsBin = $c; break }
  }
}
if (-not (Get-Command $SopsBin -ErrorAction SilentlyContinue) -and -not (Test-Path $SopsBin)) {
  throw "sops not found. Install sops or set SOPS_BIN / place binary at .tools/gobin/sops.exe"
}

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = if (Test-Path $SopsBin) { (Resolve-Path $SopsBin).Path } else { (Get-Command $SopsBin).Source }
$psi.Arguments = "--encrypt --input-type binary --output-type yaml `"$Plaintext`""
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$p = [System.Diagnostics.Process]::Start($psi)
$out = $p.StandardOutput.ReadToEnd()
$err = $p.StandardError.ReadToEnd()
$p.WaitForExit()
if ($p.ExitCode -ne 0) { throw "sops encrypt failed: $err" }
[System.IO.File]::WriteAllText((Join-Path $Root $Ciphertext), $out)
Write-Host "wrote $Ciphertext"