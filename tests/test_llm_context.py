"""交接层测试:分层采样、payload 压缩、数字溯源。

数字溯源是本层的核心:它决定"LLM 能不能编数字"。
"""
from __future__ import annotations

import pytest

from src.llm_context import (
    MiningFacts,
    NumberProvenanceError,
    assert_numbers_from_facts,
    build_ocel_facts,
    build_single_facts,
    stratified_variants,
)


# --------------------------------------------------------------------------
# 分层采样
# --------------------------------------------------------------------------

def test_stratified_sampling_keeps_rare_variants():
    variants = [{"variant": f"A{i}", "count": 100 - i} for i in range(20)]
    picked = stratified_variants(variants, 8)
    assert len(picked) == 8
    counts = [p["count"] for p in picked]
    assert min(counts) < 90, "必须保留低频(异常)变体,不能只留头部"


def test_stratified_sampling_respects_quota():
    variants = [{"variant": f"A{i}", "count": i} for i in range(50)]
    assert len(stratified_variants(variants, 5)) == 5
    assert stratified_variants(variants, 0) == []
    assert stratified_variants([], 5) == []


def test_stratified_sampling_returns_all_when_fewer_than_quota():
    variants = [{"variant": "A", "count": 10}, {"variant": "B", "count": 3}]
    assert len(stratified_variants(variants, 8)) == 2


def test_stratified_sampling_deduplicates():
    variants = [{"variant": "A", "count": 10}, {"variant": "A", "count": 9},
                {"variant": "B", "count": 5}]
    picked = stratified_variants(variants, 5)
    assert [p["variant"] for p in picked].count("A") == 1


# --------------------------------------------------------------------------
# 构造器
# --------------------------------------------------------------------------

def _single_facts() -> MiningFacts:
    return build_single_facts(
        dataset="p2p_sim",
        summary={"num_events": 3415, "num_cases": 924, "num_activities": 21,
                 "num_resources": 90},
        process_stats={"num_transitions": 27, "num_places": 18},
        conformance={"fitness": 0.9201, "num_fitting": 850, "num_non_fitting": 74},
        performance={
            "case_durations": {"mean_days": 12.5, "median_days": 9.0,
                               "p90_days": 30.2, "max_days": 61.0},
            "sla": {"sla_days": 14, "breach_count": 210, "breach_rate": 0.227},
            "bottleneck": [{"concept:name": "Approve by Director", "mean": 91.4,
                            "p90": 220.5, "impact_score": 120000.0}],
        },
        variants=[{"variant": "A,B,C", "count": 500}, {"variant": "A,B,B,C", "count": 30},
                  {"variant": "A,C", "count": 3}],
        max_variants=3,
        report_path="reports/report_p2p_sim_x.html",
    )


def test_single_facts_metrics_are_rounded_and_typed():
    f = _single_facts()
    assert f.scope == "single"
    assert f.metrics["fitness"] == 0.9201
    assert isinstance(f.metrics["num_cases"], int)
    assert f.metrics["num_resources"] == 90


def test_single_facts_evidence_anchor():
    f = _single_facts()
    assert f.evidence["fitness"].endswith("#合规检查")
    assert "reports/report_p2p_sim_x.html" in f.evidence["fitness"]


def test_single_facts_evidence_falls_back_to_logical_section():
    f = build_single_facts(dataset="x", summary={}, process_stats={}, conformance={},
                           performance={}, variants=[])
    assert f.evidence["fitness"] == "本报告 §合规检查"


def test_single_facts_prompt_block_contains_metrics():
    block = _single_facts().to_prompt_block()
    assert "fitness = 0.9201" in block
    assert "Approve by Director" in block


def test_ocel_facts_from_pipeline_shape():
    f = build_ocel_facts(
        dataset="p2p_sim",
        summary={"num_events": 3415, "num_objects": 2083, "num_object_types": 9,
                 "num_relations": 13277, "avg_objects_per_event": 3.89,
                 "isolated_events": 0, "isolated_pct": 0.0},
        cross={
            "cross_process_triggers": [
                {"source_activity": "Send Order to Supplier",
                 "target_activity": "Post Goods Receipt", "count": 211,
                 "median_delay_h": 33.99},
            ],
            "association_summary": {"tested": 75, "significant": 69, "cross_domain": 14},
        },
        object_types=[{"object_type": "purchase_order", "num_objects": 213,
                       "num_events": 2773}],
        report_path="reports/ocel_p2p_sim_x.html",
    )
    assert f.scope == "ocel"
    assert f.metrics["association_rules_significant"] == 69
    assert f.cross_triggers[0]["count"] == 211
    assert f.object_types[0]["object_type"] == "purchase_order"


# --------------------------------------------------------------------------
# 数字溯源
# --------------------------------------------------------------------------

def test_numbers_from_facts_pass():
    f = _single_facts()
    assert_numbers_from_facts(
        "fitness 为 0.9201,924 个 case 中 74 个不拟合,SLA 偏离率 0.227。", f)


def test_percent_form_is_accepted():
    f = _single_facts()
    assert_numbers_from_facts("拟合率约 92.01%。", f)


def test_metric_name_digits_are_not_violations():
    """p90_days 里的 90 不能被当成编造数字。"""
    f = _single_facts()
    assert_numbers_from_facts("case_duration_p90_days 达到 30.2 天。", f)


def test_fabricated_number_is_rejected():
    f = _single_facts()
    with pytest.raises(NumberProvenanceError) as e:
        assert_numbers_from_facts("fitness 只有 0.75,共 1000 个 case。", f)
    assert "0.75" in e.value.violations


def test_ordinal_numbers_tolerated():
    f = _single_facts()
    assert_numbers_from_facts("结论有 3 点:第一,fitness 0.9201。", f)


def test_activity_name_with_digits_is_ignored():
    f = _single_facts()
    f.top_variants.append({"variant": "Step2,Step10", "count": 7})
    assert_numbers_from_facts("变体 Step2,Step10 出现 7 次。", f)
