# Export the live deck to a 16:9 PDF for submission uploads.
#
# The deck pulls its refusal counts from /api/summary at load time, so this
# renders the deployed URL rather than the local file: a PDF built from disk
# would carry the fallback figures written into the markup instead of what the
# desk has actually done.
#
#   powershell -File scripts/build-deck-pdf.ps1

$ErrorActionPreference = "Stop"
$out = Join-Path $PSScriptRoot "..\docs\OptionGenome-deck.pdf"
$url = "https://optiongenome.duckdns.org/deck"

$chrome = @(
  "C:\Program Files\Google\Chrome\Application\chrome.exe",
  "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $chrome) { throw "No Chrome or Edge found to render with." }
if (Test-Path $out) { Remove-Item $out -Force }

Start-Process -FilePath $chrome -Wait -NoNewWindow -ArgumentList @(
  "--headless=new", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
  "--run-all-compositor-stages-before-draw",
  "--virtual-time-budget=10000",   # let the /api/summary fetch land first
  "--print-to-pdf=$out", $url
)

if (-not (Test-Path $out)) { throw "Render produced no file." }
Write-Output ("{0} ({1} KB)" -f $out, [math]::Round((Get-Item $out).Length / 1KB))
