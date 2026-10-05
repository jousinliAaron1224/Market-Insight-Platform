"""M3 取樣：下載 5 家壽險公司的「保險商品名稱、日期及文號」清單與契約條款頁，存成測試 fixture。

在專案根目錄執行（需要能連到各公司官網）：
    python -m scripts.sample_m3

輸出到 tests/fixtures/companies/<公司>/，並印出每個檔案的狀態、大小、ETag/Last-Modified，
以及 TLS 是否需要放寬 X.509 嚴格模式（D15）。只做低頻、一次性的抓取，遵守 robots.txt。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from core.config import load_config, resolve
from core.http import PoliteClient, RetryPolicy
from core.ratelimit import DomainRateLimiter

OUT = resolve("tests/fixtures/companies")

TARGETS: dict[str, list[tuple[str, str]]] = {
    "cardif": [
        ("product_list.pdf", "https://life.cardif.com.tw/documents/418719/461728/VIE_Info_1.pdf/a7b1fe95-cfbb-4cec-a4dc-c618ff9ee336"),
        ("clause_page.html", "https://life.cardif.com.tw/a59"),
        ("clause_UA0020.pdf", "https://life.cardif.com.tw/documents/418719/462100/UA0020_Provision.pdf/09841c50-7153-42c7-aacd-bc9d8ad908fe"),
    ],
    "cathay": [
        ("product_list.pdf", "https://www.cathaylife.com.tw/official/content/dam/cathaylife-official/files/laws-policies/public-info/insurance/product/Investment.pdf"),
        ("public_info_page.html", "https://www.cathaylife.com.tw/official/laws-policies/public-info/info-insurance"),
        ("investing_page.html", "https://www.cathaylife.com.tw/cathaylife/products/insurance/investing"),
    ],
    "fubon": [
        ("product_list.pdf", "https://www.fubon.com/life/cms/B57613402C4F4DDAB6AF8CAB547399A5/2026-09/202609211516302819701100.pdf"),
        ("public_info_page.html", "https://www.fubon.com/life/Investors/public-info/fubon/D3B4A1896E0046EB844E43A98E2FC83B/"),
        ("investing_page.html", "https://www.fubon.com/life/product/personal/investing/"),
    ],
    "taiwanlife": [
        ("product_list.pdf", "https://www.taiwanlife.com/portal-api/File/10904"),
        ("clause_list.pdf", "https://www.taiwanlife.com/portal-api/File/1792"),
        ("public_info_page.json", "https://www.taiwanlife.com/portal-api/Page/AboutUs-public-info-info-insurance"),
    ],
    "kgi": [
        ("product_list.pdf", "https://www.kgilife.com.tw/zh-tw/-/media/files/kgil/footer/corp/publicinfo/piinsurance-products/date_ilp.pdf"),
        ("clause_page.html", "https://www.kgilife.com.tw/zh-tw/footer/corp/publicinfo/piinsurance-products/202412-insurance-products-clause"),
    ],
}


def make_client(cfg, limiter, strict: bool) -> PoliteClient:
    d = cfg["defaults"]
    return PoliteClient(d["user_agent"], limiter, timeout=60, x509_strict=strict,
                        retry=RetryPolicy(max=1, backoff_sec=[10]))


def main() -> int:
    cfg = load_config()
    limiter = DomainRateLimiter(cfg["defaults"]["min_interval_sec"], 1)
    strict, relaxed = make_client(cfg, limiter, True), make_client(cfg, limiter, False)
    report = []
    try:
        for company, files in TARGETS.items():
            (OUT / company).mkdir(parents=True, exist_ok=True)
            for name, url in files:
                row = {"company": company, "file": name, "url": url}
                for client, mode in ((strict, "strict"), (relaxed, "x509_relaxed")):
                    try:
                        r = client.get(url, respect_robots=True)
                        (OUT / company / name).write_bytes(r.content)
                        row.update(status=r.status_code, bytes=len(r.content), tls=mode,
                                   content_type=r.headers.get("Content-Type"),
                                   etag=r.headers.get("ETag"), last_modified=r.headers.get("Last-Modified"))
                        break
                    except httpx.ConnectError as e:
                        if mode == "strict" and ("CERTIFICATE" in str(e) or "SSL" in str(e)):
                            row["strict_error"] = str(e)[:160]  # 只有嚴格格式檢查失敗才改用放寬模式（D15）
                            continue
                        row["error"] = repr(e)[:200]
                        break
                    except Exception as e:  # noqa: BLE001 — 取樣腳本：記錄後繼續下一個
                        row["error"] = repr(e)[:200]
                        break
                report.append(row)
                ok = "OK " if "bytes" in row else "ERR"
                print(f"{ok} {company:<10} {name:<24} {row.get('status', '-')} {row.get('bytes', 0):>9,} B  "
                      f"tls={row.get('tls', '-'):<12} etag={'有' if row.get('etag') else '無'} "
                      f"lm={'有' if row.get('last_modified') else '無'}  {row.get('error', '')}")
    finally:
        strict.close()
        relaxed.close()
    (OUT / "sample_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n報告：{OUT / 'sample_report.json'}")
    return 0 if all("bytes" in r for r in report) else 1


if __name__ == "__main__":
    sys.exit(main())
