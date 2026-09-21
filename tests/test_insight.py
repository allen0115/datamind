"""L0 解读测试。

重点是三道防幻觉闸门:schema 校验、数字溯源、不可用时降级。
"""
from __future__ import annotations

import pytest

from src.insight import (
    Finding,
    InsightResult,
    build_user_prompt,
    generate_insight,
    render_insight_section,
    rule_based_insight,
)
from src.llm_client import (
    LLMClient,
    LLMUnavailableError,
    NullProvider,
    SchemaError,
    ScriptedProvider,
)
from src.llm_context import MiningFacts, NumberProvenanceError


def _facts() -> MiningFacts:
    return MiningFacts(
        dataset="p2p_sim",
        scope="single",
        metrics={"num_cases": 924, "fitness": 0.9201, "non_fitting_cases": 74,
                 "sla_breach_rate": 0.227, "sla_breach_count": 210, "sla_days": 14,
                 "case_duration_p90_days": 30.2, "case_duration_median_days": 9.0},
        top_variants=[{"variant": "A,B,C", "count": 500}],
        bottlenecks=[{"concept:name": "Approve by Director", "mean": 91.4,
                      "p90": 220.5}],
        evidence={"fitness": "r.html#conformance",
                  "sla_breach_rate": "r.html#performance",
                  "case_duration_p90_days": "r.html#performance"},
    )


def _ok_payload() -> dict:
    return {
        "summary": "整体健康,但 SLA 偏离偏多。",
        "findings": [{
            "claim": "拟合度 0.9201,不拟合 74 个。",
            "metric_refs": ["fitness", "non_fitting_cases"],
            "severity": "info",
            "evidence": "r.html#conformance",
        }],
        "recommendations": ["优先治理 Approve by Director。"],
    }


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------

def test_generate_insight_success():
    client = LLMClient(provider=ScriptedProvider(responses=[_ok_payload()]))
    r = generate_insight(_facts(), client)
    assert r.source == "llm"
    assert "0.9201" in r.findings[0].claim


def test_generate_insight_fabricated_number_triggers_retry_then_fallback():
    bad = {"summary": "很差", "findings": [
        {"claim": "拟合度只有 0.75,共 1000 个 case。", "metric_refs": ["fitness"],
         "severity": "critical", "evidence": ""}], "recommendations": []}
    provider = ScriptedProvider(responses=[bad, bad])
    client = LLMClient(provider=provider)
    r = generate_insight(_facts(), client)
    assert len(provider.calls) == 2, "首次不合规应重试一次"
    assert r.source == "rule", "重试仍不合规应退回规则化结果"


def test_generate_insight_retry_success():
    bad = {"summary": "差", "findings": [
        {"claim": "拟合度 0.75", "metric_refs": ["fitness"], "severity": "warn",
         "evidence": ""}], "recommendations": []}
    provider = ScriptedProvider(responses=[bad, _ok_payload()])
    client = LLMClient(provider=provider)
    r = generate_insight(_facts(), client)
    assert r.source == "llm"
    assert r.findings[0].claim.count("0.75") == 0


def test_generate_insight_unavailable_falls_back():
    client = LLMClient(provider=NullProvider())
    r = generate_insight(_facts(), client)
    assert r.source == "rule"
    assert r.findings, "降级也必须给出结论"


def test_generate_insight_schema_error_falls_back():
    provider = ScriptedProvider(responses=[["不是对象"]])   # from_dict 直接抛 SchemaError
    client = LLMClient(provider=provider)
    r = generate_insight(_facts(), client)
    assert r.source == "rule"


def test_generate_insight_network_error_falls_back():
    provider = ScriptedProvider(responses=[LLMUnavailableError("网络不可达")])
    client = LLMClient(provider=provider)
    r = generate_insight(_facts(), client)
    assert r.source == "rule"


# --------------------------------------------------------------------------
# 规则化降级
# --------------------------------------------------------------------------

def test_rule_based_insight_uses_only_facts_numbers():
    r = rule_based_insight(_facts())
    from src.llm_context import assert_numbers_from_facts
    assert_numbers_from_facts(r.all_text(), _facts())   # 不应抛错
    assert any(f.severity == "warn" for f in r.findings), "SLA 偏离 0.227 应判为关注"


def test_rule_based_insight_flags_low_fitness():
    facts = _facts()
    facts.metrics["fitness"] = 0.6
    r = rule_based_insight(facts)
    assert any(f.severity == "critical" for f in r.findings)


def test_rule_based_insight_ocel_isolated_events():
    facts = MiningFacts(dataset="x", scope="ocel",
                        metrics={"isolated_pct": 12.5, "avg_objects_per_event": 1.1})
    r = rule_based_insight(facts)
    assert any(f.severity == "critical" for f in r.findings)
    assert any("平均对象/事件" in f.claim for f in r.findings)


# --------------------------------------------------------------------------
# prompt 与渲染
# --------------------------------------------------------------------------

def test_user_prompt_contains_facts_and_schema():
    prompt = build_user_prompt(_facts())
    assert "fitness = 0.9201" in prompt
    assert "InsightResult" in prompt or "required" in prompt


def test_render_insight_section_none_is_empty():
    assert render_insight_section(None) == ""


def test_render_insight_section_contains_claim_and_badge():
    html = render_insight_section(InsightResult(
        summary="s", findings=[Finding(claim="c1", metric_refs=["fitness"],
                                       severity="critical", evidence="e")],
        recommendations=["r1"], source="llm", model="m1"))
    assert "c1" in html and "badge-bad" in html and "r1" in html
    assert "<section>" in html


def test_render_insight_section_escapes_html():
    html = render_insight_section(InsightResult(
        summary="<script>x</script>", findings=[], recommendations=[]))
    assert "<script>" not in html


def test_insight_result_to_dict_shape():
    d = InsightResult(summary="s", findings=[Finding(claim="c")]).to_dict()
    assert d["findings"][0]["claim"] == "c"
    assert d["source"] == "llm"
