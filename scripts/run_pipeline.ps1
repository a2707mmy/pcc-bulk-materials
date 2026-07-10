# Local pipeline: fetch -> consolidate -> git push
# Triggered monthly by Windows Task Scheduler (source blocks overseas cloud IPs,
# so fetch must run from a Taiwan-based machine).
# Exit codes: 0 = done (maybe no new data), 1 = failure.
# NOTE: ASCII-only so Windows PowerShell 5.1 parses it regardless of file BOM.
# NOTE: git/python write normal progress to stderr; with ErrorActionPreference='Continue'
#       plus 2>&1 capture, that never turns into a terminating error. We gate on $LASTEXITCODE.

$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path $repo)) { Write-Error "repo not found: $repo"; exit 1 }
Set-Location $repo
$log = Join-Path $repo 'run.log'

function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'o'), $m) }

# Run a native command, tee combined output to the log, return its exit code.
function Run($file, $argList) {
    & $file @argList 2>&1 | ForEach-Object { Add-Content -Path $log -Value "$_" }
    return $LASTEXITCODE
}

Log '=== pipeline start ==='

# 1) Fetch + immutable archive
$code = Run 'python' @('src\fetch.py')
Log "fetch exit = $code"
if ($code -eq 1) { Log 'fetch FAILED (timeout or pin mismatch), abort'; exit 1 }
if ($code -eq 2) { Log 'no new data, done'; exit 0 }
if ($code -ne 0) { Log "unexpected fetch exit ($code), abort"; exit 1 }

# 2) Consolidate (only when there is new data)
if ((Run 'python' @('src\consolidate.py')) -ne 0) { Log 'consolidate FAILED'; exit 1 }

# 3) Push to GitHub (Power BI reads from GitHub raw)
Run 'git' @('add', 'data') | Out-Null
$changed = & git status --porcelain -- data 2>$null
if ([string]::IsNullOrWhiteSpace(($changed | Out-String))) { Log 'no output changes, skip push'; exit 0 }
if ((Run 'git' @('commit', '-m', ("data update {0}" -f (Get-Date -Format 'yyyy-MM-dd')))) -ne 0) { Log 'commit FAILED'; exit 1 }
if ((Run 'git' @('push')) -ne 0) { Log 'push FAILED'; exit 1 }
Log 'pushed to GitHub'
exit 0
