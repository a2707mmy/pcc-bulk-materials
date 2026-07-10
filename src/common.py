"""共用工具：設定載入、編碼偵測、數值/期別/區間清洗、地區對照、雜湊。"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

import yaml

# ---- 路徑 ----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
RAW_DIR = ROOT / "data" / "raw"
CURATED_DIR = ROOT / "data" / "curated"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---- 編碼 ----------------------------------------------------------------
# 來源檔實測為 cp950；仍先試 UTF-8 以防日後改版。
_ENCODINGS = ["utf-8-sig", "utf-8", "cp950", "big5"]


def read_text(raw: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # 最後手段：以 cp950 容錯解碼，不讓整批中斷
    return raw.decode("cp950", errors="replace")


# ---- 數值清洗 ------------------------------------------------------------
# 九種以上空值/無效表示法
_NULL_TOKENS = {"", "-", "－", "—", "N/A", "NA", "無", "未調查", "#DIV/0!", "#N/A", "#VALUE!"}


def clean_number(value) -> float | None:
    """去除千分位、前後空白、全形符號，轉為 float；無效值回傳 None。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("，", "")
    s = s.replace("　", "").strip()  # 全形空白
    if s in _NULL_TOKENS:
        return None
    # 全形數字轉半形
    s = s.translate(str.maketrans("０１２３４５６７８９．＋－", "0123456789.+-"))
    try:
        return float(s)
    except ValueError:
        return None


# ---- 期別（民國）--------------------------------------------------------
def parse_roc_period(value) -> tuple[str, str, date] | tuple[None, None, None]:
    """接受 '11504'（YYYMM）或 '114.8'（YYY.M）等民國期別。
    回傳 (period_roc 'YYYMM', period 'YYY-MM', period_date)。無效回傳三個 None。"""
    if value is None:
        return None, None, None
    s = str(value).strip()
    if not s:
        return None, None, None
    roc_y = mon = None
    if "." in s:  # 114.8 / 114.08
        parts = s.split(".")
        try:
            roc_y, mon = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return None, None, None
    else:  # 純數字 11504
        digits = re.sub(r"\D", "", s)
        if len(digits) == 5:
            roc_y, mon = int(digits[:3]), int(digits[3:])
        elif len(digits) == 6:  # 防 YYYYMM 誤填
            roc_y, mon = int(digits[:4]) - 1911, int(digits[4:])
        else:
            return None, None, None
    # 合理界限：民國 1~999 年（西元 1912~2910），避免異常輸入導致 date() 溢位
    if not (1 <= mon <= 12) or not (1 <= roc_y <= 999):
        return None, None, None
    try:
        period_date = date(roc_y + 1911, mon, 1)
    except (ValueError, OverflowError):
        return None, None, None
    period_roc = f"{roc_y:03d}{mon:02d}"
    period = f"{roc_y:03d}-{mon:02d}"
    return period_roc, period, period_date


# ---- 價格區間（砂石）----------------------------------------------------
_RANGE_SEP = re.compile(r"\s*[-~－—〜～]\s*")


def split_range(value) -> tuple[float | None, float | None, float | None]:
    """'450-480' / '700~780'（全半形分隔混用）-> (min, max, mid)。
    單一數值 -> (v, v, v)。無效 -> (None, None, None)。"""
    if value is None:
        return None, None, None
    s = str(value).strip()
    if not s:
        return None, None, None
    parts = [p for p in _RANGE_SEP.split(s) if p != ""]
    nums = [clean_number(p) for p in parts]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None, None, None
    lo, hi = min(nums), max(nums)
    return lo, hi, (lo + hi) / 2


# ---- 地區對照 ------------------------------------------------------------
def region_group(label: str, cfg: dict) -> str:
    label = (label or "").strip()
    if label in cfg.get("region_group", {}):
        return cfg["region_group"][label]
    if label in cfg.get("county_group", {}):
        return cfg["county_group"][label]
    return "其他"


def region_level(label: str, cfg: dict) -> str:
    label = (label or "").strip()
    if label in cfg.get("region_level", {}):
        return cfg["region_level"][label]
    if label in cfg.get("county_group", {}):
        return "county"
    return "unknown"


# ---- 雜湊 ----------------------------------------------------------------
def row_key(period: str, material: str, item_name: str, region: str) -> str:
    raw = "｜".join([str(period or ""), str(material or ""),
                     str(item_name or ""), str(region or "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
