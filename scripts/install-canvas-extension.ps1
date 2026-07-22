$ErrorActionPreference = "Stop"
$ExtensionPath = "C:\Services\oms-study-automation\extension\canvas-hub"
if (-not (Test-Path (Join-Path $ExtensionPath "manifest.json"))) {
  throw "Canvas extension was not found at $ExtensionPath"
}
Write-Host "1. Open chrome://extensions in your existing Chrome profile."
Write-Host "2. Turn on Developer mode."
Write-Host "3. Choose Load unpacked."
Write-Host "4. Select: $ExtensionPath"
Write-Host "5. Open http://127.0.0.1:8765/canvas/setup to pair it."
