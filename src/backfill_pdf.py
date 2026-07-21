# -*- coding: utf-8 -*-
"""將歷月「大宗資材價格彙整表」PDF 轉為歷史價格 CSV（供趨勢分析）。

兩種版面皆處理：
- 新版「大宗資材價格彙整表」（109.04 起）：欄= 調查項目 | 地區 | 單位 | 當月價(A)
- 舊版「大宗資材價格及其漲跌幅彙整表」（~109.03 以前）：多了 B/C/D/E 與漲跌欄，
  但「單位」欄後第一欄仍是當月價(A)。

作法：偵測「單位」欄（值形如 元/M3、元/T、元/包）→ 其左為地區、其右為當月價。
調查項目為合併儲存格，向下填補。期別取自檔名（民國 YYYMMDD）。
輸出 data/history/material_price_history.csv（長格式，與 consolidate_pdf.py 同一份歷史表）。
"""
from __future__ import annotations

import glob
import re
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "history"
PDF_DIR = OUT_DIR / "pdf"            # 已收進專案的歷月彙整表 PDF
OUT_CSV = OUT_DIR / "material_price_history.csv"

UNIT_RE = re.compile(r"^\s*元\s*/\s*(M3|M³|T|包|公噸|kg|KG|噸)\s*$")
NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
REGION_MAP = {"北": "北", "中": "中", "南": "南", "花": "東", "東": "東", "": "全國"}


def parse_period_from_name(name: str):
    """檔名民國日期 YYYMMDD（可含 . 或 -）-> (period_roc 'YYYMM', period 'YYY-MM', date)。"""
    digits = re.sub(r"[^\d]", "", name)
    m = re.search(r"(\d{3})(\d{2})(\d{2})$", digits)  # 取末 7 碼 YYYMMDD
    if not m:
        m = re.search(r"(\d{3})(\d{2})(\d{2})", digits)
    if not m:
        return None, None, None
    roc, mm, _dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mm <= 12) or not (90 <= roc <= 999):
        return None, None, None
    return f"{roc:03d}{mm:02d}", f"{roc:03d}-{mm:02d}", date(roc + 1911, mm, 1)


def clean_price(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace(" ", "")
    if not NUM_RE.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def infer_material(item: str) -> str:
    it = item or ""
    if "預拌混凝土" in it or "CLSM" in it:
        return "預拌混凝土"
    if "瀝青混凝土" in it or "瀝青混凝土舖面" in it:
        return "瀝青混凝土"
    if it.startswith("瀝青") or "黏度AC" in it:
        return "瀝青"
    if "鋼筋" in it:
        return "鋼筋"
    if "型鋼" in it:
        return "H型鋼"
    if "軋鋼料" in it or "鋼板" in it:
        return "鋼板"
    if "粒料" in it or "砂" in it:
        return "砂石"
    if "水泥" in it:
        return "水泥"
    return "其他"


def norm_item(item: str) -> str:
    """跨版面標準化品名，讓歷史序列不因細微差異斷裂。
    NFKC 將 PDF 抽出的 CJK 相容字元（U+F9xx，如 泥/蘭）還原為標準字，
    否則品名無法與現有歷史資料對應。"""
    s = unicodedata.normalize("NFKC", item or "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("SD420w", "SD420W").replace("舖面", "鋪面")
    return s


def detect_unit_col(rows) -> int | None:
    counts = {}
    for r in rows:
        for i, c in enumerate(r):
            if c and UNIT_RE.match(str(c).replace("\n", "")):
                counts[i] = counts.get(i, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def parse_pdf(path: Path) -> list[dict]:
    period_roc, period, period_date = parse_period_from_name(path.name)
    if period is None:
        return []
    rows_out = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table or len(table) < 2:
                    continue
                ucol = detect_unit_col(table)
                if ucol is None or ucol < 1:
                    continue
                item_col, region_col, price_col = 0, ucol - 1, ucol + 1
                last_item = None
                for r in table:
                    if price_col >= len(r):
                        continue
                    cell_item = (r[item_col] or "").replace("\n", "").strip() if item_col < len(r) else ""
                    if cell_item:
                        last_item = cell_item
                    unit_cell = str(r[ucol] or "").replace("\n", "").strip()
                    if not UNIT_RE.match(unit_cell):
                        continue  # 略過表頭/備註列
                    price = clean_price(r[price_col])
                    if price is None or last_item is None:
                        continue
                    region_raw = str(r[region_col] or "").replace("\n", "").strip() if 0 <= region_col < len(r) else ""
                    region = region_raw if region_raw in REGION_MAP else ("" if region_raw == "" else region_raw)
                    item = norm_item(last_item)
                    rows_out.append({
                        "period": period, "period_roc": period_roc,
                        "period_date": period_date.isoformat(),
                        "material": infer_material(item),
                        "item_name": item,
                        "unit": unit_cell.replace("M³", "M3"),
                        "region": region or "不分區",
                        "region_group": REGION_MAP.get(region, "全國"),
                        "region_level": "national" if region == "" else "region",
                        "price": price, "price_min": None, "price_max": None,
                        "price_basis": "exact",
                        "source_dataset": "歷史彙整表PDF", "source_file": path.name,
                    })
    return rows_out


def main():
    pdfs = sorted(glob.glob(str(PDF_DIR / "大宗資材*彙整表*.pdf")))
    all_rows, ok, empty, bad = [], [], [], []
    for p in pdfs:
        path = Path(p)
        try:
            rows = parse_pdf(path)
        except Exception as e:
            bad.append((path.name, str(e)))
            continue
        if rows:
            ok.append(path.name)
            all_rows.extend(rows)
        else:
            empty.append(path.name)

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["period", "material", "item_name", "region"])
        df = df.sort_values(["period_date", "material", "item_name", "region"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    # 涵蓋報告
    months = sorted(df["period"].unique()) if not df.empty else []
    print(f"PDF 檔數: {len(pdfs)}；成功 {len(ok)}、空表 {len(empty)}、錯誤 {len(bad)}")
    print(f"輸出 {len(df)} 列，涵蓋 {len(months)} 個月：{months[0] if months else '-'} ~ {months[-1] if months else '-'}")
    if empty:
        print(f"\n[空表/未解析出資料，建議重新下載] {len(empty)} 檔：")
        for n in empty:
            print("  -", n)
    if bad:
        print(f"\n[讀取錯誤] {len(bad)} 檔：")
        for n, e in bad:
            print("  -", n, "::", e[:80])


if __name__ == "__main__":
    main()
