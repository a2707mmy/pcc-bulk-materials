# Local pipeline: fetch PDFs -> parse to history CSV -> git push
# Source: 工程會 tec0404 price-trend PDFs (JSON API). Triggered weekly by Windows
# Task Scheduler (source blocks overseas cloud IPs, so must run from a Taiwan machine).
# Exit codes: 0 = done (maybe no new data), 1 = failure.
# NOTE: ASCII-only so Windows PowerShell 5.1 parses it regardless of file BOM.
# NOTE: git/python write normal progress to stderr; with ErrorActionPreference='Continue'
#       plus 2>&1 capture, that never turns into a terminating error. We gate on $LASTEXITCODE.

$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
# Decode python's UTF-8 stdout correctly so the log isn't mojibake.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path $repo)) { Write-Error "repo not found: $repo"; exit 1 }
Set-Location $repo
$log = Join-Path $repo 'run.log'

function Log($m) { Add-Content -Path $log -Encoding UTF8 -Value ("{0}  {1}" -f (Get-Date -Format 'o'), $m) }

# Run a native command, tee combined output to the log, return its exit code.
function Run($file, $argList) {
    & $file @argList 2>&1 | ForEach-Object { Add-Content -Path $log -Encoding UTF8 -Value "$_" }
    return $LASTEXITCODE
}

Log '=== pipeline start ==='

# 1) Fetch price-trend PDFs into immutable raw layer (data\raw\pdf)
$code = Run 'python' @('src\fetch_pdf.py')
Log "fetch_pdf exit = $code"
if ($code -eq 1) { Log 'fetch_pdf FAILED (timeout or pin mismatch), abort'; exit 1 }
if ($code -eq 2) { Log 'no new PDF, done'; exit 0 }
if ($code -ne 0) { Log "unexpected fetch_pdf exit ($code), abort"; exit 1 }

# 2) Parse PDFs -> data\history\material_price_history.csv (only when there is new data)
if ((Run 'python' @('src\consolidate_pdf.py')) -ne 0) { Log 'consolidate_pdf FAILED'; exit 1 }

# 3) Push to GitHub (Power BI reads from GitHub raw)
Run 'git' @('add', 'data') | Out-Null
$changed = & git status --porcelain -- data 2>$null
if ([string]::IsNullOrWhiteSpace(($changed | Out-String))) { Log 'no output changes, skip push'; exit 0 }
if ((Run 'git' @('commit', '-m', ("data update {0}" -f (Get-Date -Format 'yyyy-MM-dd')))) -ne 0) { Log 'commit FAILED'; exit 1 }
if ((Run 'git' @('push')) -ne 0) { Log 'push FAILED'; exit 1 }
Log 'pushed to GitHub'
exit 0
