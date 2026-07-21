# Local pipeline: fetch PDFs -> parse -> record run -> git push
# 本機詳細日誌 run.log（gitignored，不上傳）；雲端精簡歷程 data\run_history.csv（上傳）
# 每次執行都追加一列歷程並上傳（有新資料／無新資料／失敗皆記錄），作為排程「心跳」。
$ErrorActionPreference = 'Continue'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$repo = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path $repo)) { Write-Error "repo not found: $repo"; exit 1 }
Set-Location $repo
$log = Join-Path $repo 'run.log'

function Log($m) { Add-Content -Path $log -Encoding UTF8 -Value (("{0}  {1}" -f (Get-Date -Format 'o'), $m)) }
function Run($file, $argList) {
    & $file @argList 2>&1 | ForEach-Object { Add-Content -Path $log -Encoding UTF8 -Value "$_" }
    return $LASTEXITCODE
}

Log '=== pipeline start ==='

# 1) 下載
$fetchCode = Run 'python' @('src\fetch_pdf.py')
Log "fetch_pdf exit = $fetchCode"

# 2) 決定結果；有新資料才整理。不提前 exit，確保後面一定會記錄並上傳。
$result = ''
if     ($fetchCode -eq 1) { $result = 'fetch_failed' }
elseif ($fetchCode -eq 2) { $result = 'no_new_data' }
elseif ($fetchCode -eq 0) {
    if ((Run 'python' @('src\consolidate_pdf.py')) -ne 0) { $result = 'consolidate_failed' }
    else { $result = 'new_data' }
}
else { $result = "fetch_unexpected_$fetchCode" }
Log "result = $result"

# 3) 計算安全欄位（只讀公開資料，不含任何敏感內容）
$latestPeriod = ''; $rowsTotal = ''
$csvPath = Join-Path $repo 'data\history\material_price_history.csv'
if (Test-Path $csvPath) {
    try {
        $rows = Import-Csv -Path $csvPath
        $rowsTotal = $rows.Count
        $latestPeriod = ($rows | Sort-Object period | Select-Object -Last 1).period
    } catch { }
}

# 4) 追加一列到雲端執行歷程（append-only）
$histCsv = Join-Path $repo 'data\run_history.csv'
if (-not (Test-Path $histCsv)) {
    Add-Content -Path $histCsv -Encoding UTF8 -Value 'run_utc,result,fetch_exit,latest_period,rows_total'
}
$runUtc = (Get-Date).ToUniversalTime().ToString('o')
Add-Content -Path $histCsv -Encoding UTF8 -Value ("{0},{1},{2},{3},{4}" -f $runUtc, $result, $fetchCode, $latestPeriod, $rowsTotal)

# 5) 上傳（歷程檔每次都變動，故每次執行都會留下一筆雲端紀錄）
Run 'git' @('add', 'data') | Out-Null
if ((Run 'git' @('commit', '-m', ("run {0} {1}" -f (Get-Date -Format 'yyyy-MM-dd'), $result))) -ne 0) { Log 'commit FAILED'; exit 1 }
if ((Run 'git' @('push')) -ne 0) { Log 'push FAILED'; exit 1 }
Log 'pushed to GitHub'

# 6) 退出碼：失敗類回傳 1，其餘 0
if ($result -eq 'fetch_failed' -or $result -eq 'consolidate_failed' -or $result -like 'fetch_unexpected*') { exit 1 }
exit 0
