# -*- coding: utf-8 -*-
"""從工程會 tec0404 JSON API 下載『大宗資材價格』各類趨勢 PDF 至不可變 raw 層。

背景 API（未設限、無需登入）：
  列目錄  GET  {list_path}                        -> 全檔案清單 JSON
  下載檔  POST {download_path}  {"id": <fileNo>}  -> PDF blob（注意 id 放 fileNo token，非數字 id）
主機憑證缺 Subject Key Identifier，沿用與 fetch.py 相同的 SHA-256 指紋釘選
（仍驗證伺服器身分，只繞過壞掉的鏈驗證；見 config/sources.yaml 註解）。

退出碼：0=有新檔寫入、2=清單內全部已存在（無新檔）、1=有下載失敗（告警）。
"""
from __future__ import annotations

import hashlib
import json
import ssl
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from http.client import HTTPSConnection
from pathlib import Path

from common import ROOT, load_config, sha256_bytes

PDF_RAW_DIR = ROOT / "data" / "raw" / "pdf"
MAX_BYTES = 50 * 1024 * 1024  # 單檔上限；PDF 實際 <1MB，僅防病態回應


def _pinned_conn(host: str, pin: str, timeout: int = 90) -> HTTPSConnection:
    """建立經指紋比對的連線（比對失敗即關閉並拋錯）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 由下方指紋比對取代鏈驗證
    conn = HTTPSConnection(host, 443, timeout=timeout, context=ctx)
    conn.connect()
    der = conn.sock.getpeercert(binary_form=True)
    if not der:
        conn.close()
        raise ssl.SSLError("無法取得伺服器憑證，拒絕連線")
    actual = hashlib.sha256(der).hexdigest()
    if actual.lower() != pin.lower():
        conn.close()
        raise ssl.SSLError(f"憑證指紋不符（可能遭中間人或憑證已輪替）：預期 {pin}，實得 {actual}")
    return conn


def _read_capped(resp) -> bytes:
    body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise RuntimeError("回應超過大小上限，拒絕處理")
    return body


def get_json(host: str, path: str, pin: str) -> object:
    conn = _pinned_conn(host, pin)
    try:
        conn.request("GET", path, headers={
            "User-Agent": "pcc-bulk-materials/2.0", "Accept": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} {resp.reason}")
        return json.loads(_read_capped(resp).decode("utf-8"))
    finally:
        conn.close()


def download_pdf(host: str, path: str, pin: str, file_no: str) -> bytes:
    conn = _pinned_conn(host, pin)
    try:
        body = json.dumps({"id": file_no}).encode("utf-8")
        conn.request("POST", path, body=body, headers={
            "User-Agent": "pcc-bulk-materials/2.0",
            "Content-Type": "application/json;charset=utf-8", "Accept": "*/*"})
        resp = conn.getresponse()
        data = _read_capped(resp)
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} {resp.reason}：{data[:160]!r}")
        if data[:5] != b"%PDF-":
            raise RuntimeError(f"回應非 PDF（前 16 位元組 {data[:16]!r}）")
        return data
    finally:
        conn.close()


def select_wanted(catalog: list, prefixes: list[tuple[str, str]], btype: str | None) -> list[tuple[str, str, str]]:
    """從目錄挑出 6 系列 PDF：回傳 (material, fileNameOri, fileNo)。"""
    wanted = []
    for d in catalog:
        if not isinstance(d, dict):
            continue
        if btype and str(d.get("businessType", "")).strip() != btype:
            continue
        name = str(d.get("fileNameOri", ""))
        if not name.lower().endswith(".pdf"):
            continue
        mat = next((m for p, m in prefixes if name.startswith(p)), None)
        if mat is None:
            continue
        file_no = d.get("fileNo")
        if not file_no:
            continue
        wanted.append((mat, name, file_no))
    return wanted


def main() -> int:
    cfg = load_config()
    host = urllib.parse.urlsplit(cfg["host"]).netloc
    pin = cfg["tls_pin_sha256"]
    api = cfg["pdf_api"]
    prefixes = [(s["name_prefix"], s["material"]) for s in cfg["pdf_series"]]

    print(f"取檔案目錄 {api['list_path']} …")
    catalog = get_json(host, api["list_path"], pin)
    if not isinstance(catalog, list):
        print("[FAIL] 目錄回應非陣列", file=sys.stderr)
        return 1
    wanted = select_wanted(catalog, prefixes, api.get("business_type"))
    print(f"目錄 {len(catalog)} 筆；符合 6 系列趨勢 PDF：{len(wanted)} 筆")

    PDF_RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = PDF_RAW_DIR / "_manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}

    ingest_ts = datetime.now(timezone.utc).isoformat()
    new = skip = fail = 0
    for mat, name, file_no in wanted:
        target = PDF_RAW_DIR / mat / Path(name).name  # basename 化，防路徑穿越
        if target.exists():
            skip += 1
            continue
        try:
            data = download_pdf(host, api["download_path"], pin, file_no)
        except Exception as e:  # 單檔失敗不中斷其他
            fail += 1
            print(f"[FAIL] {name}: {e}", file=sys.stderr)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest[name] = {
            "material": mat, "fileNo": file_no,
            "sha256": sha256_bytes(data), "bytes": len(data), "ingest_ts": ingest_ts,
        }
        new += 1
        if new % 25 == 0:
            print(f"  …已下載 {new} 檔")
        time.sleep(0.15)  # 禮貌延遲，避免壓垮政府主機

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成：新增 {new}、已存在 {skip}、失敗 {fail}（清單 {len(wanted)}）")
    if fail:
        return 1
    return 0 if new else 2


if __name__ == "__main__":
    sys.exit(main())
