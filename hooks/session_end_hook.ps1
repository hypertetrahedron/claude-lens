# Claude Code SessionEnd hook wrapper (Windows PowerShell).
#
# Finds a Python and the hook script next to this file. Stdin is inherited by
# the child, which is how the hook payload reaches it. Always exits 0.
$ErrorActionPreference = 'SilentlyContinue'
$dir = Split-Path -Parent $PSCommandPath
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$script = Join-Path $dir 'session_end_hook.py'
foreach ($name in @('python3', 'python', 'py')) {
    $exe = Get-Command $name -ErrorAction SilentlyContinue
    if ($exe) {
        & $exe.Source $script @args
        exit 0
    }
}
exit 0
