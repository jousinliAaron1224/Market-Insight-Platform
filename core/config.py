"""讀取 config/sources.yaml，合併 defaults 與單一來源設定。"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "sources.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("INTEL_CONFIG", DEFAULT_CONFIG))
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def source_config(cfg: dict[str, Any], source_id: str) -> dict[str, Any]:
    for s in cfg.get("sources", []):
        if s["id"] == source_id:
            return {**cfg.get("defaults", {}), **s}
    raise KeyError(f"source not in config: {source_id}")


def resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_adapter_class(spec: str):
    """'adapters.tii_law_rss:TiiLawRssAdapter' -> class"""
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)
