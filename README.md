# 大宗資材價格資料下載與彙整

自動從政府資料開放平臺（data.gov.tw）下載工程會 7 組大宗資材價格 CSV，封存每月快照，並彙整為單一乾淨資料表，供 Power BI 讀取。

排程採**方案 B**：跑在 GitHub Actions 雲端，不依賴本機開機；產出 commit 回 repo，Power BI 從 `raw.githubusercontent.com` 讀取。

## 專案結構

```
config/sources.yaml           來源登錄檔（7 條連結、結構家族、地區/單位對照、憑證指紋）
src/common.py                 共用工具：編碼、期別、數值/區間清洗、地區對照、雜湊
src/fetch.py                  下載 + 每月不可變封存（憑證指紋釘選、退出碼驅動）
src/consolidate.py            四種結構解析 → 標準化 → 彙整單一表 + 維度表 + 品質報告
.github/workflows/monthly.yml GitHub Actions 每月排程（方案 B）
data/raw/<YYYY-MM>/           每月原始檔封存（不可變，勿手改）
data/curated/                 輸出資料表（供 Power BI 讀取）
```

## 輸出檔（data/curated/，UTF-8-BOM）

| 檔案 | 內容 |
|---|---|
| `fact_material_price.csv` | 各期×品項×地區的報價（長格式）；砂石為區間，取中點並標 `price_basis=range_mid` |
| `fact_price_change.csv` | 彙整表獨有的跨期漲跌（半年/一年/兩年） |
| `dim_material.csv` / `dim_region.csv` / `dim_date.csv` | 維度表 |
| `quality_report.json` | 每個資料集的解析筆數與狀態（ok/empty/missing/error） |

歷史由多個月快照累積；以 `row_key`（期別｜品項｜品名｜地區）去重，同期別重複不會灌爆事實表。

## 本機執行

```bash
pip install -r requirements.txt
python src/fetch.py         # 下載並封存到 data/raw/<本月>/
python src/consolidate.py   # 產出 data/curated/
```

`fetch.py` 退出碼：`0`=有新資料、`2`=內容未變（正常）、`1`=下載失敗（告警）。

## 部署到 GitHub（方案 B）

1. 建立 GitHub repo，將本專案推上去。
2. `.github/workflows/monthly.yml` 已設定每月 12/16/20/24 日 00:00 UTC（= 08:00 台北）自動執行，並可在 Actions 頁面手動觸發（Run workflow）。
3. 工作流會下載、彙整並把 `data/` 變更 commit 回 repo。需在 repo Settings → Actions → General → Workflow permissions 選 **Read and write permissions**。

## Power BI 連線（由使用者處理）

以「Web」連接器讀取 curated 檔的 raw 網址，例如：
`https://raw.githubusercontent.com/<帳號>/<repo>/main/data/curated/fact_material_price.csv`
設定 Power BI 服務排程重新整理即可定時取得最新資料。星狀關聯建議：
`fact_material_price[region] → dim_region[region]`、`[item_name] → dim_material[item_name]`、`[period] → dim_date[period]`。

## 維護

- **新增/調整資料集**：改 `config/sources.yaml` 的 `datasets`，不需改程式。
- **調整地區歸屬**（如宜蘭縣改歸東區）：改 `config/sources.yaml` 的 `county_group`。
- **更新憑證指紋**（來源憑證輪替導致 fetch 報「指紋不符」時）：
  ```bash
  python -c "import ssl,socket,hashlib; ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE; s=ctx.wrap_socket(socket.create_connection(('pcic.pcc.gov.tw',443)),server_hostname='pcic.pcc.gov.tw'); print(hashlib.sha256(s.getpeercert(True)).hexdigest())"
  ```
  將輸出填入 `sources.yaml` 的 `tls_pin_sha256`。

## 資料來源與授權

行政院公共工程委員會，經 data.gov.tw 發布，適用「政府資料開放授權條款第 1 版」（免費、可加值、需標示來源）。官方網站 robots.txt 禁止爬取，本專案僅使用開放資料檔直連網址。
