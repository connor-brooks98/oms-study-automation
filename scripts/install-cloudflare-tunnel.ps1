[CmdletBinding(SupportsShouldProcess)]
param(
  [SecureString]$TunnelToken
)

$ErrorActionPreference = "Stop"
if (-not $TunnelToken) {
  $TunnelToken = Read-Host "Cloudflare Tunnel token" -AsSecureString
}
if (-not $TunnelToken) {
  throw "A Cloudflare Tunnel token is required"
}

$Cloudflared = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
if (-not $Cloudflared) {
  if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
    throw "Install cloudflared, then run this script again"
  }
  if ($PSCmdlet.ShouldProcess("cloudflared", "Install with winget")) {
    & winget.exe install --id Cloudflare.cloudflared --exact --silent
    $Cloudflared = Get-Command cloudflared.exe -ErrorAction Stop
  } elseif ($WhatIfPreference) {
    Write-Host "Cloudflare Tunnel dry run complete."
    return
  }
}

if ($PSCmdlet.ShouldProcess("cloudflared", "Install named tunnel Windows service")) {
  $TokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($TunnelToken)
  try {
    $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPointer)
    & $Cloudflared.Source service install $PlainToken
    if ($LASTEXITCODE -ne 0) {
      throw "cloudflared service installation failed"
    }
  } finally {
    if ($TokenPointer -ne [IntPtr]::Zero) {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPointer)
    }
    $PlainToken = $null
  }
}

Write-Host "Cloudflare Tunnel service installed. Verify its status in Zero Trust."
