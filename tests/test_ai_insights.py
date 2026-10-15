import json
import sqlite3
from types import SimpleNamespace

from scripts.ai_insights import PROMPT_VERSION, ensure_table, validate
from scripts.export_radar import apply_ai


def test_validate_keeps_only_allowed_values():
    ok = validate({"impact_dir": "neg", "priority": "7", "lines": ["inv", "xyz"], "why": "原因", "impact_text": "影響",
                   "owner": "精算", "actions": ["a", "", "b", "c"]})
    assert ok["priority"] == 5 and ok["lines"] == ["inv"] and ok["actions"] == ["a", "b"]
    assert validate({"impact_dir": "bad", "priority": 3, "why": "x", "impact_text": "y", "owner": "精算"}) is None
    assert validate({"impact_dir": "neu", "priority": 3, "why": "x", "impact_text": "y", "owner": "不存在的部門"}) is None
    assert validate({"impact_dir": "neu", "priority": "高", "why": "x", "impact_text": "y", "owner": "精算"}) is None


def test_apply_ai_overrides_rule_reading(tmp_path):
    db = tmp_path / "intel.db"
    ensure_table(str(db))
    c = sqlite3.connect(db)
    c.execute("INSERT INTO ai_insights VALUES (?,?,?,?,?)", (7, PROMPT_VERSION, "m", "2026-10-15T00:00:00+00:00", json.dumps(
        {"impact_dir": "neg", "priority": 5, "lines": ["usd"], "why": "AI 原因", "impact_text": "AI 影響", "owner": "精算", "actions": ["做 A"]}, ensure_ascii=False)))
    c.commit()
    news = [{"_rid": 7, "lines": [], "imp": 2, "why": "規則", "impact": {"lines": [], "dir": "neu", "text": "規則判讀"}},
            {"_rid": 8, "lines": [], "imp": 2, "why": "規則", "impact": {"lines": [], "dir": "neu", "text": "規則判讀"}}]
    regs = [{"_rid": 7, "lines": [], "imp": 3, "why": "", "analysis": "規則", "design": ["人工判讀"], "affected": ["公司層級"]}]
    apply_ai(news, regs, SimpleNamespace(db_path=str(db)))
    assert news[0]["impact"] == {"lines": ["usd"], "dir": "neg", "text": "AI 影響"} and news[0]["imp"] == 5 and news[0]["ai"]["owner"] == "精算"
    assert "ai" not in news[1] and news[1]["why"] == "規則" and "_rid" not in news[1]
    assert regs[0]["analysis"] == "AI 影響" and regs[0]["design"] == ["做 A"] and regs[0]["affected"] == ["美元利變・年金"]
