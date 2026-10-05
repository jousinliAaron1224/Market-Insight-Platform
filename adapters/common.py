"""adapter 共用小工具：時區、正規化內容指紋、HTML 轉純文字。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser, Node

TPE = ZoneInfo("Asia/Taipei")


def content_fingerprint(obj: Any) -> str:
    """變動偵測用的內容指紋（Handbook 決策 D11）。

    頁面上有每次都會變的雜訊（如「瀏覽人次」）時，adapter 以抽出的正文算指紋，
    raw 仍保存原始位元組。預設情況（未呼叫此函式）content_hash 即原始位元組的 sha256。
    """
    canon = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


_WS = re.compile(r"[ \t 　]+")


def node_text(node: Node | None) -> str:
    """保留換行的純文字：<br> 與區塊元素轉成換行，壓縮多餘空白。"""
    if node is None:
        return ""
    html = node.html or ""
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h\d)>", "\n", html)
    text = HTMLParser(html).text(separator="")
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def html_to_text(html: str) -> str:
    return node_text(HTMLParser(f"<div>{html}</div>").css_first("div"))
