"""L4 交互闭环测试:意图路由、离线降级、回归集。

意图路由必须是确定性的(不经过 LLM),否则同一问题可能走不同分析路径。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.llm_client import build_llm_client
from src.llm_main import route_intent, run_hypothesis, run_insight, run_regression

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_route_intent_is_deterministic():
    assert route_intent("有哪些可疑的异常链路?") == "hypothesis"
    assert route_intent("帮我看看有没有违规风险") == "hypothesis"
    assert route_intent("对象模型该怎么设计?") == "object-model"
    assert route_intent("哪些环节最慢?") == "insight"
    assert route_intent("") == "insight"
    # 同一问题多次路由结果一致
    assert route_intent("异常链路") == route_intent("异常链路")


def test_insight_disabled_returns_none(cfg):
    client = build_llm_client(cfg, root=ROOT)
    assert client.available is False
    out = run_insight("p2p_sim_xes", cfg, ROOT, client)
    assert out["result"] is None, "LLM 未启用时报告不应出现解读章节"
    assert "note" in out


def test_insight_force_rule_fallback(cfg):
    client = build_llm_client(cfg, root=ROOT)
    out = run_insight("p2p_sim_xes", cfg, ROOT, client, force_rule_fallback=True)
    assert out["result"] is not None
    assert out["result"]["source"] == "rule"
    assert out["result"]["findings"]


def test_hypothesis_requires_ocel_dataset(cfg):
    client = build_llm_client(cfg, root=ROOT)
    out = run_hypothesis("p2p_sim_xes", cfg, ROOT, client)
    assert "error" in out


def test_regression_suite_passes_offline(cfg):
    client = build_llm_client(cfg, root=ROOT)
    report = run_regression(ROOT / "eval" / "llm_regression.yaml", cfg, ROOT, client)
    assert report["total"] >= 5
    assert report["failed"] == 0, [c for c in report["cases"] if not c["ok"]]
