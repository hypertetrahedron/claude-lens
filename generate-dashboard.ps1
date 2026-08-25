# Generate the Claude Code usage dashboard (Windows).
#
# One-shot: parses every Claude Code transcript it can find into a local SQLite
# DB, renders dashboard.html and index.html next to this script, and opens the
# dashboard. Re-running is incremental and always safe. Requires Python 3.9+
# (stdlib only).
#
# By default it reads ~\.claude (or $env:CLAUDE_CONFIG_DIR) plus any sibling
# .claude* directory. Standing configuration for extra locations and remote
# machines lives in sources.json (see sources.example.json); the switches below
# add to it for a single run.
#
#   .\generate-dashboard.ps1                      # ingest + rebuild + open
#   .\generate-dashboard.ps1 -NoOpen              # skip the browser
#   .\generate-dashboard.ps1 -Index               # open index.html instead
#   .\generate-dashboard.ps1 -Force               # re-parse all transcripts
#   .\generate-dashboard.ps1 -ExtraDir D:\backups # also search a location
#   .\generate-dashboard.ps1 -Remote box1,box2    # also collect over SSH
#   .\generate-dashboard.ps1 -SshConfig           # ...every ~\.ssh\config host
param(
    [switch]$NoOpen,
    [switch]$Index,
    [switch]$Force,
    [string[]]$ExtraDir,
    [int]$Depth,
    [switch]$NoSiblings,
    [string[]]$Remote,
    [switch]$SshConfig,
    [switch]$RemoteFull,
    [int]$SshTimeout
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

$ingestArgs = @()
if ($Force)      { $ingestArgs += "--force" }
if ($NoSiblings) { $ingestArgs += "--no-siblings" }
if ($SshConfig)  { $ingestArgs += "--ssh-config" }
if ($RemoteFull) { $ingestArgs += "--remote-full" }
foreach ($d in $ExtraDir) { $ingestArgs += @("--extra-dir", $d) }
foreach ($h in $Remote)   { $ingestArgs += @("--remote", $h) }
if ($PSBoundParameters.ContainsKey("Depth"))      { $ingestArgs += @("--depth", $Depth) }
if ($PSBoundParameters.ContainsKey("SshTimeout")) { $ingestArgs += @("--ssh-timeout", $SshTimeout) }

Write-Host "Ingesting Claude Code transcripts..." -ForegroundColor Cyan
& $py jsonl_ingest.py @ingestArgs
if ($LASTEXITCODE -ne 0) { Write-Error "Transcript ingestion failed." }

Write-Host "Building dashboard..." -ForegroundColor Cyan
& $py build_dashboard.py
if ($LASTEXITCODE -ne 0) { Write-Error "Dashboard build failed." }

$dash = Join-Path $PSScriptRoot "dashboard.html"
$idx = Join-Path $PSScriptRoot "index.html"
Write-Host "Dashboard ready: $dash" -ForegroundColor Green
Write-Host "All reports:     $idx" -ForegroundColor Green
if (-not $NoOpen) { Start-Process $(if ($Index) { $idx } else { $dash }) }
