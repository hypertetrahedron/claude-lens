# Claude Code SessionEnd hook wrapper (Windows PowerShell).
#
# Finds a Python and the hook script next to this file. Stdin is inherited by
# the child, which is how the hook payload reaches it. Always exits 0 and
# writes nothing to stdout (Claude Code interprets hook output).
$ErrorActionPreference = 'SilentlyContinue'
$dir = Split-Path -Parent $PSCommandPath
if (-not $dir) { $dir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$script = Join-Path $dir 'session_end_hook.py'
# The py launcher goes first: it is the one interpreter a python.org install
# always provides. `python3` and `python` are tried after it, but never the
# copies under WindowsApps - those are the Microsoft Store stubs, which exit
# 9009 without running anything when no Store Python is installed, and
# finding one first used to mean the hook silently never ran.
foreach ($name in @('py', 'python', 'python3')) {
    $exe = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch '\\WindowsApps\\' } |
        Select-Object -First 1
    if (-not $exe) { continue }
    if ($name -eq 'py') {
        & $exe.Source -3 $script @args
    } else {
        & $exe.Source $script @args
    }
    # The hook itself always exits 0, so anything else means this interpreter
    # did not run it; try the next one rather than giving up.
    if ($LASTEXITCODE -eq 0) { exit 0 }
}
exit 0
