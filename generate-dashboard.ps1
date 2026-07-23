# Generate the Claude Code usage dashboard (Windows).
#
# One-shot: parses every Claude Code transcript for the signed-in user
# (~\.claude\projects, or $env:CLAUDE_CONFIG_DIR\projects if set) into a local
# SQLite DB, renders dashboard.html next to this script, and opens it.
# Re-running is incremental and always safe. Requires Python 3.9+ (stdlib only).
#
#   .\generate-dashboard.ps1           # ingest new activity + rebuild + open
#   .\generate-dashboard.ps1 -NoOpen   # skip opening the browser
#   .\generate-dashboard.ps1 -Force    # re-parse all transcripts from scratch
param(
    [switch]$NoOpen,
    [switch]$Force
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
foreach ($cand in @("python", "python3", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) {
    Write-Error "Python 3.9+ is required but was not found on PATH. Install it from https://python.org and re-run."
}
& $py -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.9 or newer is required (found: $(& $py --version))."
}

Write-Host "Ingesting Claude Code transcripts..." -ForegroundColor Cyan
if ($Force) { & $py jsonl_ingest.py --force } else { & $py jsonl_ingest.py }
if ($LASTEXITCODE -ne 0) { Write-Error "Transcript ingestion failed." }

Write-Host "Building dashboard..." -ForegroundColor Cyan
& $py build_dashboard.py
if ($LASTEXITCODE -ne 0) { Write-Error "Dashboard build failed." }

$dash = Join-Path $PSScriptRoot "dashboard.html"
Write-Host "Dashboard ready: $dash" -ForegroundColor Green
if (-not $NoOpen) { Start-Process $dash }
