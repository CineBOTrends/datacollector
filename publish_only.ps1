# publish_only.ps1
# Rebuild data/ from the EXISTING raw outputs (no scrape) and push to the dashboard.
# Use this to re-publish with the poster fix using data you already collected.
# Place in the collector root (next to cli.py) and run: .\publish_only.ps1
$ErrorActionPreference = "Stop"
$Collector = $PSScriptRoot
$Publish   = Join-Path $Collector "..\dashboard-publish"

Write-Host "== rebuild data tree (with posters) ==" -ForegroundColor Cyan
py build_data.py .

Write-Host "== push data/ -> dashboard ==" -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $Publish ".git"))) {
  git clone https://github.com/CineBOTrends/dashboard.git $Publish
}
Push-Location $Publish
git pull --quiet
if (Test-Path "data") { Remove-Item -Recurse -Force "data" }
Copy-Item -Recurse (Join-Path $Collector "data") "data"
git add -f data
git commit -m ("data(rebuild-posters): {0}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")) 2>$null
if ($LASTEXITCODE -eq 0) { git push } else { Write-Host "no changes to push" }
Pop-Location
Write-Host "DONE -> rebuilt with posters + pushed; Cloudflare will rebuild" -ForegroundColor Green
