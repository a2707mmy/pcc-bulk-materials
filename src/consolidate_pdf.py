# -*- coding: utf-8 -*-
"""將 6 條個別『價格趨勢』PDF（data/raw/pdf/）解析為歷史價格 CSV。

依「表頭簽章」自動辨識版面，容忍 12 年間格式演變：
  wide         寬表：調查項目 | (計價單位) | 北區 | 中區 | 南區 [| 不分區]
               —— 鋼筋 / H型鋼 / 鋼板；舊格式為 北 | 中 | 南 | 花蓮 | 台東（無單位欄）
  region_stat  分區統計長表：調查項目(合併) | 區域 | 價格 | 標準差 | 樣本數
               —— 預拌 / 瀝青（每項 5 區列）
  county_range 縣市區間：縣市 | 砂價格區間 | 石價格區間 —— 砂石

同一檔內若同 (品項,地區) 出現多次（舊格式一月多筆週調查），取最後一週（日期最新）為當月值。
期別取自檔名（民國 YYYMMDD）。輸出 data/history/material_price_history.csv（長格式）。

用法：
  python consolidate_pdf.py                # 解析 data/raw/pdf/
  python consolidate_pdf.py <dir> [--dry]  # 解析指定目錄；--dry 不寫檔只印報告
"""
from __future__ import annotations

import glob
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import pdfplumber

from common import ROOT, load_config, split_range

PDF_RAW_DIR = ROOT / "data" / "raw" / "pdf"
OUT_DIR = ROOT / "data" / "history"
OUT_CSV = OUT_DIR / "material_price_history.csv"

# 地區標籤 -> (region_group, region_level, 正規化地區名)
REGION_INFO = {
    "北區": ("北", "region", "北區"), "中區": ("中", "region", "中區"), "南區": ("南", "region", "南區"),
    "北": ("北", "region", "北區"), "中": ("中", "region", "中區"), "南": ("南", "region", "南區"),
    "花蓮": ("東", "region", "花蓮"), "台東": ("東", "region", "台東"),
    "花": ("東", "region", "花蓮"), "東": ("東", "region", "東區"), "東區": ("東", "region", "東區"),
    "不分區": ("全國", "national", "不分區"),
}
REGION_TOKENS = set(REGION_INFO)
UNIT_RE = re.compile(r"單位[:：]\s*(元\s*/\s*[^\s，,]+)")


def cell(v) -> str:
    # NFKC：PDF 抽出的地區/品名可能為 CJK 相容字元（U+F9xx，如 北/蓮/泥），
    # 外觀相同但碼位不同，未正規化會使地區/表頭比對失敗。
    if v is None:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(v)))


def norm_item(item: str) -> str:
    s = unicodedata.normalize("NFKC", item or "")
    s = re.sub(r"\s+", "", s)
    return s.replace("SD420w", "SD420W").replace("舖面", "鋪面")


def parse_period_from_name(name: str):
    digits = re.sub(r"[^\d]", "", name)
    m = re.search(r"(\d{3})(\d{2})(\d{2})$", digits) or re.search(r"(\d{3})(\d{2})(\d{2})", digits)
    if not m:
        return None, None, None
    roc, mm = int(m.group(1)), int(m.group(2))
    if not (1 <= mm <= 12) or not (90 <= roc <= 999):
        return None, None, None
    return f"{roc:03d}{mm:02d}", f"{roc:03d}-{mm:02d}", date(roc + 1911, mm, 1)


def material_of(name: str, series) -> str | None:
    for s in series:
        if name.startswith(s["name_prefix"]):
            return s["material"]
    return None


def table_unit(table) -> str | None:
    for row in table[:2]:
        for c in row:
            if c and (m := UNIT_RE.search(str(c).replace("\n", ""))):
                return m.group(1).replace(" ", "").replace("M³", "M3")
    return None


def find_header_row(table) -> int | None:
    for i, row in enumerate(table):
        if any(cell(c) == "調查項目" for c in row):
            return i
    return None


# ---- 三種抽取器 ---------------------------------------------------------
def extract_wide(table, material, unit) -> list[dict]:
    """寬表：地區為欄。回傳 (item, region, price) 明細列。"""
    hi = find_header_row(table)
    if hi is None:
        return []
    header = table[hi]
    region_cols = {i: cell(c) for i, c in enumerate(header) if cell(c) in REGION_TOKENS}
    if not region_cols:
        return []
    item_col = next((i for i, c in enumerate(header) if cell(c) == "調查項目"), 0)
    out, last_item = [], None
    for row in table[hi + 1:]:
        it = cell(row[item_col]) if item_col < len(row) else ""
        if it and it != "調查項目":
            last_item = it
        if last_item is None:
            continue
        for ci, label in region_cols.items():
            if ci >= len(row):
                continue
            price = _num(row[ci])
            if price is None:
                continue
            out.append(_row(material, last_item, label, unit, price))
    return out


def extract_region_stat(table, material, unit) -> list[dict]:
    """分區統計長表：地區為列、每項 5 區。價格欄含『價格』；地區欄以資料判定。"""
    hi = find_header_row(table)
    if hi is None:
        return []
    header = table[hi]
    price_col = next((i for i, c in enumerate(header) if "價格" in cell(c)), None)
    item_col = next((i for i, c in enumerate(header) if cell(c) == "調查項目"), 1)
    body = table[hi + 1:]
    # 地區欄 = 資料列中最常出現地區詞的欄
    counts = {}
    for row in body:
        for i, c in enumerate(row):
            if cell(c) in REGION_TOKENS:
                counts[i] = counts.get(i, 0) + 1
    if not counts:
        return []
    region_col = max(counts, key=counts.get)
    if price_col is None:
        # 無『價格』欄（舊格式多為多個週調查日期欄）→ 取最右側數值欄（最新一期）
        numcols = {i for row in body for i, c in enumerate(row)
                   if i != region_col and _num(c) is not None}
        price_col = max(numcols) if numcols else region_col + 1
    out, group, seen = [], [], set()

    def flush():
        if not group:
            return
        frags = [cell(r[item_col]) for r in group if item_col < len(r) and cell(r[item_col])]
        item = norm_item("".join(frags)) or "(未命名)"
        for r in group:
            label = cell(r[region_col]) if region_col < len(r) else ""
            if label not in REGION_TOKENS:
                continue
            price = _num(r[price_col]) if price_col < len(r) else None
            if price is None:
                continue
            out.append(_row(material, item, label, unit, price))

    for row in body:
        label = cell(row[region_col]) if region_col < len(row) else ""
        if label in REGION_TOKENS and label in seen:
            flush(); group, seen = [], set()
        group.append(row)
        if label in REGION_TOKENS:
            seen.add(label)
    flush()
    return out


def extract_county_range(table, cfg) -> list[dict]:
    """縣市區間：縣市 | 砂價格區間 | 石價格區間 —— 砂石。"""
    hi = next((i for i, r in enumerate(table) if any(cell(c) == "縣市" for c in r)), None)
    if hi is None:
        return []
    header = table[hi]
    # 每個「價格區間」欄對應一種細料（砂/石）
    subcols = {i: cell(c).replace("價格區間", "") for i, c in enumerate(header) if "價格區間" in cell(c)}
    county_col = next((i for i, c in enumerate(header) if cell(c) == "縣市"), 0)
    if not subcols:
        return []
    cg = cfg.get("county_group", {})
    price_idxs = sorted(subcols)
    out = []
    for row in table[hi + 1:]:
        county = cell(row[county_col]) if county_col < len(row) else ""
        if not county or county == "縣市":
            continue
        grp = cg.get(county, "其他")
        for k, ci in enumerate(price_idxs):
            sub = subcols[ci]
            # 資料可能不在表頭欄，而在其後的「本月」子欄；掃到下一個價格欄之前，
            # 取第一個可解析的區間（=本月/當期）。兼容簡單格式與本月/上月格式。
            end = price_idxs[k + 1] if k + 1 < len(price_idxs) else len(row)
            lo = hi_ = mid = None
            for j in range(ci, min(end, len(row))):
                lo, hi_, mid = split_range(row[j])
                if mid is not None:
                    break
            if mid is None:
                continue
            out.append({
                "material": "砂石", "item_name": norm_item(sub or "砂石"),
                "unit": "元/M3", "region": county, "region_group": grp,
                "region_level": "county", "price": mid, "price_min": lo,
                "price_max": hi_, "price_basis": "range",
            })
    return out


def _num(v):
    if v is None:
        return None
    s = unicodedata.normalize("NFKC", str(v)).replace(",", "").replace("，", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return None
    f = float(s)
    return f if f > 0 else None  # 排除 0/負（多為漲跌%或空欄殘值）


def _row(material, item, label, unit, price) -> dict:
    grp, lvl, region = REGION_INFO[label]
    return {
        "material": material, "item_name": norm_item(item), "unit": unit or "",
        "region": region, "region_group": grp, "region_level": lvl,
        "price": price, "price_min": None, "price_max": None, "price_basis": "exact",
    }


def parse_pdf(path: Path, material: str, cfg) -> list[dict]:
    period_roc, period, period_date = parse_period_from_name(path.name)
    if period is None:
        return []
    rows = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table or len(table) < 2:
                    continue
                unit = table_unit(table)
                guard = " ".join(cell(c) for r in table[:2] for c in r)
                if "漲跌" in guard or "比較" in guard:
                    continue  # 跳過「最近一週之漲跌幅比較」表
                flat = " ".join(cell(c) for r in table[:3] for c in r)
                if "縣市" in flat and "價格區間" in flat:
                    rows += extract_county_range(table, cfg)
                elif "調查項目" in flat or any(cell(c) in REGION_TOKENS for r in table for c in r):
                    # 先試寬表（地區為欄）；抽不到則當地區為列（分區統計/舊砂石週調查）
                    got = extract_wide(table, material, unit)
                    if not got:
                        got = extract_region_stat(table, material, unit)
                    rows += got
    # 補期別欄
    for r in rows:
        r.update({"period": period, "period_roc": period_roc,
                  "period_date": period_date.isoformat(),
                  "source_dataset": "價格趨勢PDF", "source_file": path.name})
    return rows


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    src_dir = Path(args[0]) if args else PDF_RAW_DIR
    cfg = load_config()
    series = cfg["pdf_series"]

    pdfs = sorted(glob.glob(str(src_dir / "**" / "*.pdf"), recursive=True))
    all_rows, ok, empty, bad, skipped = [], [], [], [], []
    for p in pdfs:
        path = Path(p)
        mat = material_of(path.name, series)
        if mat is None:
            skipped.append(path.name); continue
        try:
            rows = parse_pdf(path, mat, cfg)
        except Exception as e:
            bad.append((path.name, str(e))); continue
        (ok if rows else empty).append(path.name)
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    if not df.empty:
        # 同檔多筆週調查 -> 取最後一週（同 period,material,item,region）。
        # parse_pdf 依 extract_tables() 由上而下（即週調查時間先後）附加各列，
        # 且此處 groupby 前未重排，故每組最後一筆即為最後一週；"last" 亦會略過空值。
        keys = ["period", "period_roc", "period_date", "material", "item_name",
                "unit", "region", "region_group", "region_level", "price_basis",
                "source_dataset"]
        agg = df.groupby(keys, as_index=False, dropna=False).agg(
            price=("price", "last"), price_min=("price_min", "min"),
            price_max=("price_max", "max"),
            source_file=("source_file", "first"))
        df = agg.sort_values(["period_date", "material", "item_name", "region"])

    months = sorted(df["period"].unique()) if not df.empty else []
    print(f"PDF {len(pdfs)}：成功 {len(ok)}、空 {len(empty)}、錯誤 {len(bad)}、非系列略過 {len(skipped)}")
    print(f"輸出 {len(df)} 列；涵蓋 {len(months)} 月 {months[0] if months else '-'} ~ {months[-1] if months else '-'}")
    if not df.empty:
        print("\n各品項 列數 / 月數 / 地區數：")
        for mat, g in df.groupby("material"):
            print(f"  {mat}: {len(g)} 列, {g['period'].nunique()} 月, 區:{sorted(g['region_group'].unique())}")
    if empty:
        print(f"\n[空表 {len(empty)}]（前10）:", empty[:10])
    if bad:
        print(f"\n[錯誤 {len(bad)}]（前10）:")
        for n, e in bad[:10]:
            print("  -", n, "::", e[:80])

    if not dry:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        print(f"\n已寫入 {OUT_CSV}")
    else:
        print("\n(--dry：未寫檔)")


if __name__ == "__main__":
    main()
