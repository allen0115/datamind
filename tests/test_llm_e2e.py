"""真实 LLM 端到端验证(需要 .env 里配置可用的 API key)。

默认不跑:`pytest -q` 会跳过(见 pyproject 的 markers)。
手动跑:
    在 .env 填入真实 key 后
    .venv/bin/python -m pytest -m e2e -s

验证三件事:
1. 真实模型返回的结构能被 pydantic schema 接住
2. 结论里的数字全部能溯源到 pm4py 的 facts(即模型没有编造)
3. 假设闭环在真实模型下也能跑通,判定权始终在 pm4py
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src.llm_client import build_llm_client
from src.llm_context import assert_numbers_from_facts, build_ocel_facts

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"

_PLACEHOLDERS = ("在这里", "xxxx", "your", "changeme")


def _real_client_or_skip(cfg):
    from src.llm_client import llm_settings

    s = llm_settings(cfg)
    if not s.get("enabled"):
        pytest.skip("llm.enabled=false(或 DATAMIND_LLM_ENABLED 未开启)")
    key = os.environ.get(s.get("api_key_env", "DATAMIND_LLM_API_KEY"), "")
    if not key or any(p in key for p in _PLACEHOLDERS):
        pytest.skip("未配置真实 API key(仍是 .env.example 里的占位值)")
    try:
        import openai  # noqa: F401
    except ImportError:
        pytest.skip("未安装 openai SDK:uv sync --extra llm")
    return build_llm_client(cfg, root=ROOT)


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ocel_facts():
    from src.ocel_cross_process import full_cross_process_report
    from src.ocel_loader import load_ocel, object_type_stats, summarize_ocel

    ocel = load_ocel(ROOT / "data/sim/p2p_sim.jsonocel")
    cross = full_cross_process_report(ocel, min_trigger_count=5)
    return ocel, build_ocel_facts(
        dataset="p2p_sim", summary=summarize_ocel(ocel), cross=cross,
        object_types=object_type_stats(ocel).to_dict(orient="records"))


@pytest.mark.e2e
def test_real_insight_is_traceable(cfg, ocel_facts, tmp_path):
    from src.insight import generate_insight

    client = _real_client_or_skip(cfg)
    client.cache_dir = tmp_path / "cache"      # 用临时缓存,确保真的调模型
    _, facts = ocel_facts

    result = generate_insight(facts, client)
    print(f"\n[模型 {result.model}] {result.summary}")
    for f in result.findings:
        print(f"  [{f.severity}] {f.claim} | 依据 {f.metric_refs}")

    assert result.source == "llm", "真实调用不应退回规则化结论"
    assert result.findings, "至少应给出一条结论"
    assert_numbers_from_facts(result.all_text(), facts)   # 编造数字会在这里抛错


@pytest.mark.e2e
def test_real_hypothesis_loop(cfg, ocel_facts, tmp_path):
    from src.hypothesis import run_hypothesis_loop

    client = _real_client_or_skip(cfg)
    client.cache_dir = tmp_path / "cache2"
    ocel, facts = ocel_facts

    out = run_hypothesis_loop(facts, ocel, client)
    print(f"\n假设来源: {out['source']},成立 {out['supported_count']} 条,拦截 {len(out['dropped'])} 条")
    for v in out["verdicts"]:
        print(f"  {v['hypothesis']}: {v['supported']} · {v['evidence']}")

    assert out["hypotheses"], "模型应提出至少一条假设"
    # 判定结果必须带统计证据,而不是模型的"意见"
    for v in out["verdicts"]:
        assert v["evidence"]
