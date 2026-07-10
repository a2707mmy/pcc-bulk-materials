"""讀取所有 raw 快照 -> 依結構家族解析 -> 標準化 -> 彙整為單一資料表。

輸出（data/curated/，UTF-8-BOM，供 Power BI 直接讀取）：
  fact_material_price.csv  各期各品項各地區的報價（長格式）
  fact_price_change.csv    彙整表獨有的跨期漲跌
  dim_material.csv / dim_region.csv / dim_date.csv
  quality_report.json      每個資料集的解析筆數與狀態
歷史由多個月快照累積；以 row_key 去重，同期別重複不會灌爆事實表。
"""
from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone

import pandas as pd

from common import (
    CURATED_DIR,
    RAW_DIR,
    clean_number,
    load_config,
    parse_roc_period,
    read_text,
    region_group,
    region_level,
    row_key,
    split_range,
)

FACT_COLS = [
    "period", "period_roc", "period_date", "snapshot",
    "material", "item_name", "unit", "unit_assumed",
    "region", "region_group", "region_level",
    "price", "price_min", "price_max", "price_basis",
    "std_dev", "sample_size",
    "source_dataset", "source_file", "ingest_ts", "row_key",
]

CHANGE_COLS = [
    "period", "period_roc", "period_date", "item_name",
    "region", "region_group", "horizon",
    "base_period", "base_reference", "price_current", "price_base", "pct_change",
    "source_dataset", "source_file", "ingest_ts",
]

import re

_PRICE_COL = re.compile(r"^\s*([\d.]+)價格\(([A-Z])\)\s*$")
_CHANGE_COL = re.compile(r"(半年|一年|兩年)漲跌")
_CHANGE_LETTER = re.compile(r"A-([A-Z])\)\s*/\s*A")
_BASE_REF = re.compile(r"基準([\d.]+)")


def _read_csv(raw_bytes: bytes) -> pd.DataFrame:
    text = read_text(raw_bytes)
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def _base_fact(ds: dict, snapshot: str, ingest_ts: str) -> dict:
    return {
        "material": ds["material"],
        "unit_assumed": False,
        "price_min": None, "price_max": None, "price_basis": "exact",
        "std_dev": None, "sample_size": None,
        "snapshot": snapshot,
        "source_dataset": ds["name"], "source_file": ds["file"], "ingest_ts": ingest_ts,
    }


def parse_wide_region(df, ds, snapshot, ingest_ts, cfg) -> list[dict]:
    """鋼筋/H型鋼/鋼板：地區為欄（北區…台東，或不分區價格）。"""
    rows = []
    id_cols = ["調查時間", "調查項目", "單位"]
    region_cols = [c for c in df.columns if c not in id_cols]
    for _, r in df.iterrows():
        period_roc, period, period_date = parse_roc_period(r.get("調查時間"))
        item = (r.get("調查項目") or "").strip()
        unit = (r.get("單位") or "").strip()
        if not item:
            continue
        for rc in region_cols:
            region = rc.replace("價格", "").strip()  # 不分區價格 -> 不分區
            price = clean_number(r.get(rc))
            if price is None:
                continue
            f = _base_fact(ds, snapshot, ingest_ts)
            f.update(
                period=period, period_roc=period_roc, period_date=period_date,
                item_name=item, unit=unit, price=price,
                region=region, region_group=region_group(region, cfg),
                region_level=region_level(region, cfg),
                row_key=row_key(period, ds["material"], item, region),
            )
            rows.append(f)
    return rows


def parse_region_stat(df, ds, snapshot, ingest_ts, cfg) -> list[dict]:
    """預拌/瀝青混凝土：已是長格式，含標準差與樣本數。"""
    rows = []
    for _, r in df.iterrows():
        period_roc, period, period_date = parse_roc_period(r.get("調查時間"))
        item = (r.get("調查項目") or "").strip()
        region = (r.get("調查地區") or "").strip()
        price = clean_number(r.get("價格"))
        if not item or price is None:
            continue
        f = _base_fact(ds, snapshot, ingest_ts)
        f.update(
            period=period, period_roc=period_roc, period_date=period_date,
            item_name=item, unit=(r.get("單位") or "").strip(), price=price,
            region=region, region_group=region_group(region, cfg),
            region_level=region_level(region, cfg),
            std_dev=clean_number(r.get("標準差")),
            sample_size=clean_number(r.get("樣本數量")),
            row_key=row_key(period, ds["material"], item, region),
        )
        rows.append(f)
    return rows


def parse_county_range(df, ds, snapshot, ingest_ts, cfg) -> list[dict]:
    """砂石：第一欄髒（縣市+區間），但有乾淨的砂/石價格區間欄；一列拆兩品項。"""
    rows = []
    products = [("砂", "砂價格區間"), ("石", "石價格區間")]
    for _, r in df.iterrows():
        period_roc, period, period_date = parse_roc_period(r.get("更新時間"))
        county_raw = (r.get("縣市") or "").strip()
        county = county_raw.split()[0] if county_raw else ""
        unit = (r.get("單位") or "").strip()
        if not county:
            continue
        for item, col in products:
            lo, hi, mid = split_range(r.get(col))
            if mid is None:
                continue
            f = _base_fact(ds, snapshot, ingest_ts)
            f.update(
                period=period, period_roc=period_roc, period_date=period_date,
                item_name=item, unit=unit,
                price=mid, price_min=lo, price_max=hi, price_basis="range_mid",
                region=county, region_group=region_group(county, cfg),
                region_level=region_level(county, cfg),
                row_key=row_key(period, ds["material"], item, county),
            )
            rows.append(f)
    return rows


def parse_summary(df, ds, snapshot, ingest_ts, cfg) -> list[dict]:
    """彙整表 -> fact_price_change。欄名內嵌民國年月，動態解析。"""
    price_letters, change_specs = {}, []
    for c in df.columns:
        m = _PRICE_COL.match(c)
        if m:
            proc, letter = m.group(1), m.group(2)
            pr_roc, pr, pr_date = parse_roc_period(proc)
            price_letters[letter] = {"col": c, "period_roc": pr_roc,
                                     "period": pr, "period_date": pr_date}
        elif _CHANGE_COL.search(c):
            horizon = _CHANGE_COL.search(c).group(1)
            lm = _CHANGE_LETTER.search(c)
            bm = _BASE_REF.search(c)
            if lm:
                change_specs.append({"col": c, "horizon": horizon,
                                     "base_letter": lm.group(1),
                                     "base_reference": bm.group(1) if bm else None})
    if "A" not in price_letters:
        return []
    a = price_letters["A"]
    rows = []
    for _, r in df.iterrows():
        item = (r.get("調查項目") or "").strip()
        region = (r.get("調查地區") or "").strip()
        if not item:
            continue
        price_current = clean_number(r.get(a["col"]))
        for spec in change_specs:
            base = price_letters.get(spec["base_letter"])
            if base is None:
                continue
            price_base = clean_number(r.get(base["col"]))
            given = clean_number(r.get(spec["col"]))
            if price_current is not None and price_base is not None and price_current != 0:
                pct = round((price_current - price_base) / price_current * 100, 1)
            else:
                pct = given
            if pct is None and price_base is None:
                continue
            rows.append({
                "period": a["period"], "period_roc": a["period_roc"],
                "period_date": a["period_date"], "item_name": item,
                "region": region, "region_group": region_group(region, cfg),
                "horizon": spec["horizon"], "base_period": base["period"],
                "base_reference": spec["base_reference"],
                "price_current": price_current, "price_base": price_base,
                "pct_change": pct,
                "source_dataset": ds["name"], "source_file": ds["file"],
                "ingest_ts": ingest_ts,
            })
    return rows


PARSERS = {
    "wide_region": parse_wide_region,
    "region_stat": parse_region_stat,
    "county_range": parse_county_range,
    "summary": parse_summary,
}


def _write_csv(df: pd.DataFrame, name: str):
    CURATED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(CURATED_DIR / name, index=False, encoding="utf-8-sig")


def main() -> int:
    cfg = load_config()
    by_file = {ds["file"]: ds for ds in cfg["datasets"]}
    ingest_ts = datetime.now(timezone.utc).isoformat()

    fact_rows, change_rows, quality = [], [], []
    snapshots = sorted(p for p in RAW_DIR.glob("*") if p.is_dir())
    if not snapshots:
        print("找不到任何 raw 快照，請先執行 fetch.py", file=sys.stderr)
        return 1

    for snap in snapshots:
        snapshot = snap.name
        for fname, ds in by_file.items():
            path = snap / fname
            if not path.exists():
                quality.append({"snapshot": snapshot, "dataset": ds["name"],
                                "status": "missing", "rows": 0})
                continue
            try:
                df = _read_csv(path.read_bytes())
                out = PARSERS[ds["family"]](df, ds, snapshot, ingest_ts, cfg)
            except Exception as e:
                quality.append({"snapshot": snapshot, "dataset": ds["name"],
                                "status": "error", "rows": 0, "detail": str(e)})
                print(f"[ERROR] {snapshot}/{ds['name']}: {e}", file=sys.stderr)
                continue
            status = "empty" if not out else "ok"
            quality.append({"snapshot": snapshot, "dataset": ds["name"],
                            "status": status, "rows": len(out)})
            if ds["family"] == "summary":
                change_rows.extend(out)
            else:
                fact_rows.extend(out)

    fact = pd.DataFrame(fact_rows, columns=FACT_COLS)
    change = pd.DataFrame(change_rows, columns=CHANGE_COLS)
    # 去重：同 row_key（期別｜品項｜品名｜地區）跨快照只留一筆
    if not fact.empty:
        fact = fact.drop_duplicates(subset="row_key", keep="first")
        fact = fact.sort_values(["period_date", "material", "item_name", "region"])
    if not change.empty:
        change = change.drop_duplicates(
            subset=["period", "item_name", "region", "horizon"], keep="first"
        ).sort_values(["period_date", "item_name", "region", "horizon"])

    _write_csv(fact, "fact_material_price.csv")
    _write_csv(change, "fact_price_change.csv")

    # 維度表
    if not fact.empty:
        _write_csv(
            fact[["material", "item_name", "unit", "unit_assumed"]]
            .drop_duplicates().sort_values(["material", "item_name"]),
            "dim_material.csv",
        )
        _write_csv(
            fact[["region", "region_group", "region_level"]]
            .drop_duplicates().sort_values(["region_group", "region"]),
            "dim_region.csv",
        )
        d = fact[["period", "period_roc", "period_date"]].drop_duplicates().copy()
        d["period_date"] = pd.to_datetime(d["period_date"], errors="coerce")
        d = d.dropna(subset=["period_date"])  # 丟棄無法解析期別的列
        d["year"] = d["period_date"].dt.year
        d["month"] = d["period_date"].dt.month
        d["roc_year"] = d["year"] - 1911
        d["quarter"] = d["period_date"].dt.quarter
        _write_csv(d.sort_values("period_date"), "dim_date.csv")

    CURATED_DIR.mkdir(parents=True, exist_ok=True)
    (CURATED_DIR / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"fact_material_price: {len(fact)} 列；fact_price_change: {len(change)} 列")
    errors = [q for q in quality if q["status"] == "error"]
    print(f"品質：ok={sum(q['status']=='ok' for q in quality)}, "
          f"empty={sum(q['status']=='empty' for q in quality)}, "
          f"missing={sum(q['status']=='missing' for q in quality)}, "
          f"error={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
