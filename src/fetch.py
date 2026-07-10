"""下載各資料集 CSV，封存至不可變的每月 raw 層。

退出碼：
  0 = 有新資料寫入
  2 = 全部下載成功但內容與現有快照相同（正常，無新資料）
  1 = 有資料集下載失敗（告警）
"""
from __future__ import annotations

import hashlib
import json
import ssl
import sys
import urllib.parse
from datetime import datetime, timezone
from http.client import HTTPSConnection
from pathlib import Path

from common import RAW_DIR, load_config, sha256_bytes


def _pinned_get(host: str, path: str, pin_sha256: str, timeout: int = 60) -> bytes:
    """以憑證指紋釘選下載。仍驗證伺服器身分（比對憑證 SHA-256），
    但繞過因缺 Subject Key Identifier 而失敗的鏈驗證。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 由下方指紋比對取代鏈驗證
    max_bytes = 50 * 1024 * 1024  # 檔案實際 <5KB；上限僅防病態回應
    conn = HTTPSConnection(host, 443, timeout=timeout, context=ctx)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        if not der:
            raise ssl.SSLError("無法取得伺服器憑證，拒絕下載")
        actual = hashlib.sha256(der).hexdigest()
        if actual.lower() != pin_sha256.lower():
            raise ssl.SSLError(
                f"憑證指紋不符（可能遭中間人攻擊或憑證已輪替）：預期 {pin_sha256}，實得 {actual}"
            )
        conn.request("GET", path, headers={"User-Agent": "pcc-bulk-materials/1.0"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} {resp.reason}")
        clen = resp.getheader("Content-Length")
        if clen and clen.isdigit() and int(clen) > max_bytes:
            raise RuntimeError("回應超過大小上限，拒絕處理")
        # read(max_bytes+1) 將記憶體綁在上限內；超量即報錯，不默默截斷。
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise RuntimeError("回應超過大小上限，拒絕處理")
        return body
    finally:
        conn.close()


def main() -> int:
    cfg = load_config()
    host_url = urllib.parse.urlsplit(cfg["host"])
    host = host_url.netloc
    base_path = host_url.path
    pin = cfg["tls_pin_sha256"]

    snapshot = datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    out_dir = RAW_DIR / snapshot
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / "_manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}

    new_count = failed = unchanged = 0
    ingest_ts = datetime.now(timezone.utc).isoformat()

    for ds in cfg["datasets"]:
        fname = ds["file"]
        if not Path(fname).name:  # 防設定檔誤填空/純路徑
            failed += 1
            print(f"[FAIL] {ds.get('name','?')}: 無效檔名 {fname!r}", file=sys.stderr)
            continue
        path = base_path + urllib.parse.quote(fname)
        try:
            body = _pinned_get(host, path, pin)
        except Exception as e:  # 單一資料集失敗不中斷其他
            failed += 1
            print(f"[FAIL] {ds['name']}: {e}", file=sys.stderr)
            continue

        digest = sha256_bytes(body)
        target = out_dir / Path(fname).name  # basename 化，避免路徑穿越
        # 不可變原始層：以「現有檔案的實際內容雜湊」判斷，不依賴 manifest，
        # 即使 manifest 遺失亦不會覆寫既有原始檔。
        if target.exists():
            existing = target.read_bytes()
            existing_digest = sha256_bytes(existing)
            if existing_digest == digest:
                unchanged += 1
                print(f"[SAME] {ds['name']} 內容未變")
                continue
            # 同月內容變更：先以舊內容雜湊前綴保存歷史副本，再寫新檔
            hist = out_dir / f"{target.stem}.{existing_digest[:8]}{target.suffix}"
            if not hist.exists():
                hist.write_bytes(existing)
        target.write_bytes(body)
        manifest[fname] = {
            "dataset_id": ds["dataset_id"],
            "family": ds["family"],
            "material": ds["material"],
            "sha256": digest,
            "bytes": len(body),
            "ingest_ts": ingest_ts,
        }
        new_count += 1
        print(f"[NEW ] {ds['name']} ({len(body)} bytes, sha256={digest[:12]}…)")

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n快照 {snapshot}：新增 {new_count}、未變 {unchanged}、失敗 {failed}")
    if failed:
        return 1
    if new_count == 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
