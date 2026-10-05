"""靜態快照匯出（scripts/export_site.py）：檔案齊全、主頁不帶外框、連結改成相對路徑、/raw/ 換成來源網址。"""
import json

from scripts.export_site import main as export_main, relink
from tests.test_web import site  # noqa: F401  (共用測試資料庫)


def test_relink():
    m = {"/raw/a.pdf": "https://x/a.pdf"}
    assert relink({"pdf": "/raw/a.pdf#page=3", "n": ["/raw/b.pdf"], "k": 1}, m) == \
        {"pdf": "https://x/a.pdf#page=3", "n": [None], "k": 1}


def test_export(site, tmp_path):  # noqa: F811
    store, _, data = site
    out = tmp_path / "site"
    assert export_main(["--db", str(data / "intel.db"), "--out", str(out)]) == 0
    idx = (out / "index.html").read_text(encoding="utf-8")
    assert "<!doctype" not in idx.lower() and "<body" not in idx and '<header class="page-h" id="head">' in idx
    assert 'src="staticapi.js"' in idx and 'id="srcFrame"' in idx and 'href="style.css"' in idx
    wall = (out / "wall.html").read_text(encoding="utf-8")
    assert wall.lower().startswith("<!doctype html>") and '"/compare.html' not in wall
    common = (out / "common.js").read_text(encoding="utf-8")
    assert '["lab", "index.html", "商品工作台"]' in common and '"/wall.html"' not in common
    for f in ("meta.json", "labels.json", "products.json", "products_detail.json", "lab_modules.json",
              "lab_features.json", "ctx_fees_1.json", "week_7.json", "market_supply.json"):
        assert (out / "data" / f).is_file(), f
    feats = json.loads((out / "data" / "lab_features.json").read_text(encoding="utf-8"))
    assert feats and all(not (x["pdf"] or "").startswith("/raw/") for x in feats)
    assert json.loads((out / "data" / "meta.json").read_text(encoding="utf-8"))["snapshot_at"]
