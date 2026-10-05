"""看一眼政府資料開放平臺的資料集：說明、檔案清單、每個檔案的前幾行。不寫資料庫、不存檔。

用來決定某個資料集值不值得加進 config/sources.yaml 的 open_data.datasets（例如找市占率要的公司別資料）。
沿用 open_data 來源的 User-Agent、限速、robots.txt 與 TLS 設定（D7／D15），不關 TLS 驗證。

用法：
    python scripts/probe_open_data.py 7191 13517
    python scripts/probe_open_data.py --robots https://ins-info.ib.gov.tw/customer/life3-2.aspx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.open_data import API, parse_dataset            # noqa: E402
from core.config import load_config, resolve, source_config  # noqa: E402
from core.http import PoliteClient, RetryPolicy, RobotsDisallowed  # noqa: E402
from core.ratelimit import DomainRateLimiter                 # noqa: E402

PREVIEW_LINES = 8
MAX_FILES = 4


def preview(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp950"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return f"（二進位檔，{len(data)} bytes）"
    lines = text.splitlines()
    shown = [ln[:200] for ln in lines[:PREVIEW_LINES]]
    return "\n".join(shown) + (f"\n…（共 {len(lines)} 行）" if len(lines) > PREVIEW_LINES else "")


def probe_dataset(http: PoliteClient, ds_id: str) -> None:
    print(f"\n======== 資料集 {ds_id}  https://data.gov.tw/dataset/{ds_id}")
    resp = http.get(API.format(id=ds_id))
    raw = json.loads(resp.content.decode("utf-8-sig"))
    result = raw.get("result", raw) if isinstance(raw, dict) else {}
    for k in ("title", "datasetName", "description", "updateFrequency", "modifiedDate", "publisher", "fieldDescription"):
        if result.get(k):
            print(f"{k}: {str(result[k])[:300]}")
    info = parse_dataset(resp.content, ds_id)
    for f in info["files"][:MAX_FILES]:
        print(f"\n-- 檔案 {f['id']}｜{f['format']}｜{f['description']}\n   {f['url']}")
        try:
            data = http.get(f["url"]).content
            print(preview(data))
        except Exception as e:                       # 一個檔案失敗不影響其他
            print(f"   下載失敗：{e}")
    if len(info["files"]) > MAX_FILES:
        print(f"\n（另有 {len(info['files']) - MAX_FILES} 個檔案未預覽）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("datasets", nargs="*", help="data.gov.tw 資料集編號")
    ap.add_argument("--robots", nargs="*", default=[], metavar="URL", help="只檢查這些網址的 robots.txt 是否允許")
    args = ap.parse_args()
    cfg = load_config()
    scfg = source_config(cfg, "open_data")
    d = cfg["defaults"]
    http = PoliteClient(scfg["user_agent"], DomainRateLimiter(d.get("min_interval_sec", 3), 1), timeout=60,
                        extra_ca_files=[resolve(f) for f in scfg.get("extra_ca_files", [])],
                        x509_strict=bool(scfg.get("x509_strict", True)),
                        retry=RetryPolicy(max=0))
    try:
        for url in args.robots:
            print(f"robots.txt {'允許' if http.allowed(url) else '不允許'}：{url}")
        for ds in args.datasets:
            try:
                probe_dataset(http, ds)
            except RobotsDisallowed as e:
                print(f"robots.txt 不允許：{e}")
            except Exception as e:
                print(f"失敗：{e}")
    finally:
        http.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
