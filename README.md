# 大宗資材價格資料下載與彙整

自動從行政院公共工程委員會（工程會）tec0404「大宗資材價格」的**價格趨勢 PDF** 下載 6 條個別材料趨勢檔，封存為不可變 raw 層，辨識彙整為單一歷史價格表，供 Power BI 讀取。

**執行架構**：來源主機 `pcic.pcc.gov.tw` 封鎖境外雲端 IP（GitHub Actions runner 一律連線逾時，實測確認），故下載必須從**台灣本機**執行。因此：本機以 Windows 工作排程器每週執行 `fetch_pdf → consolidate_pdf → git push`，把產出推回 GitHub；Power BI 再從 `raw.githubusercontent.com` 讀取。GitHub repo 擔任「資料寄放 + 交付層」。

## 專案結構

```
config/sources.yaml           來源登錄（主機、憑證指紋、PDF API、6 系列、地區/縣市對照）
src/common.py                 共用工具：設定載入、數值/區間清洗、地區對照、雜湊
src/fetch_pdf.py              從 tec0404 JSON API 下載 6 系列趨勢 PDF（憑證指紋釘選、退出碼驅動）
src/consolidate_pdf.py        三種版面辨識 → 標準化 → 彙整單一歷史表
scripts/run_pipeline.ps1      本機管線：fetch_pdf → consolidate_pdf → 記錄歷程 → git push（供工作排程器呼叫）
data/raw/pdf/<材料>/          6 系列 PDF 原始檔封存（納入版控作完整備份，遺失可用 fetch_pdf 重抓）
data/history/material_price_history.csv   彙整輸出（供 Power BI 讀取）
data/run_history.csv          雲端執行歷程（每次排程執行追加一列）
```

## 輸出檔（data/history/material_price_history.csv，UTF-8-BOM）

各期×品項×地區的報價，長格式。主要欄位：

| 欄位 | 說明 |
|---|---|
| `period` / `period_roc` / `period_date` | 期別（民國 YYY-MM / YYYMM / 西元日期） |
| `material` / `item_name` / `unit` | 材料 / 調查品項 / 計價單位 |
| `region` / `region_group` / `region_level` | 地區 / 北中南東·全國群組 / 粒度層級 |
| `price` / `price_min` / `price_max` / `price_basis` | 報價（砂石為區間，取中點並標 `price_basis=range`；其餘 `exact`） |

> 同一份月報若含當月多次週調查（民國 103～108 舊格式），以**最後一週（日期最新）**為當月值，非取平均。

## 本機執行

```bash
pip install -r requirements.txt
python src/fetch_pdf.py            # 下載 6 系列趨勢 PDF 到 data/raw/pdf/<材料>/
python src/consolidate_pdf.py      # 辨識彙整 → data/history/material_price_history.csv
python src/consolidate_pdf.py --dry  # 只印報告、不寫檔（驗證用）
```

`fetch_pdf.py` 退出碼：`0`=有新檔寫入、`2`=清單內全部已存在（無新檔）、`1`=有下載失敗（告警）。

## 每週自動執行（Windows 工作排程器）

`scripts/run_pipeline.ps1` 會依序跑 `fetch_pdf → consolidate_pdf`，把產出 commit/push 回 GitHub，並在 `data/run_history.csv` 追加一列執行紀錄。排程採**每週一 08:00**：腳本冪等（來源內容未變即 fetch_pdf 退出碼 2、秒退、不重算），發布日不固定但一週內即會抓到更新。已註冊的任務名為 `PCC_BulkMaterials`。

重建/調整任務（PowerShell，含「錯過即補跑」StartWhenAvailable）：

```powershell
$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "D:\Power BI\scripts\run_pipeline.ps1"'
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 8am
$set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName 'PCC_BulkMaterials' -Action $action -Trigger $trigger -Settings $set -Force
```

常用操作：
```powershell
Start-ScheduledTask -TaskName 'PCC_BulkMaterials'          # 立即手動執行
Get-ScheduledTaskInfo -TaskName 'PCC_BulkMaterials'        # 看 LastRunTime / LastTaskResult(0x0=成功)
Get-Content 'D:\Power BI\run.log' -Tail 20 -Encoding UTF8  # 看本機執行日誌
```

> `git push` 使用你既有的 GitHub 登入（gh/認證管理員），無需再輸入密碼。
> 任務設為「僅在登入時執行」，不需儲存密碼；電腦關機錯過的排程會於下次登入後補跑。

## 執行歷程（雲端可回溯）

`data/run_history.csv` 為 **append-only** 的精簡執行歷程，每次排程執行都追加一列（含「有新資料／無新資料／失敗」三種情形），可隨時在 GitHub 上回溯查詢自動更新是否持續、正常運作。欄位僅含安全內容（執行時間、結果分類、fetch 退出碼、目前最新期別、總列數），**不含**本機路徑、認證權杖或原始錯誤輸出；完整輸出與錯誤堆疊只留在本機 `run.log`（不上傳）。commit 訊息形如 `run 2026-07-28 no_new_data`，故 GitHub 提交列表本身即為一份可快速掃視的執行概覽。

## Power BI 連線（由使用者處理）

以「Web」連接器讀取歷史表的 raw 網址：
`https://raw.githubusercontent.com/<帳號>/<repo>/main/data/history/material_price_history.csv`
設定 Power BI 服務排程重新整理即可定時取得最新資料。

## 維護

- **調整地區歸屬**（如宜蘭縣改歸東區）：改 `config/sources.yaml` 的 `county_group`。
- **更新憑證指紋**（來源憑證輪替導致下載報「指紋不符」時）：
  ```bash
  python -c "import ssl,socket,hashlib; ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE; s=ctx.wrap_socket(socket.create_connection(('pcic.pcc.gov.tw',443)),server_hostname='pcic.pcc.gov.tw'); print(hashlib.sha256(s.getpeercert(True)).hexdigest())"
  ```
  將輸出填入 `sources.yaml` 的 `tls_pin_sha256`。

## 資料來源與授權

資料為行政院公共工程委員會之大宗資材價格趨勢，屬公開資訊，使用請標示來源。本專案初期曾嘗試以政府資料開放平臺（data.gov.tw）之開放資料檔作為即時來源，惟該平臺資料更新頻率過慢且不穩定，已改為直接下載工程會「價格趨勢」PDF 辨識彙整。
