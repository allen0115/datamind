"""L3 假设闭环测试。

判定的正确性用 p2p_sim 的已知事实断言:
  - Send Order to Supplier → Post Goods Receipt 是真主干,应判成立
  - 编造的活动名必须被白名单拦下
  - 反向链路(目标→源)因 lift 口径不同,结果由 pm4py 决定,不由 LLM 决定
"""
from __future__ import annotations

import pytest

from src.hypothesis import (
    Hypothesis,
    HypothesisSet,
    filter_ocel,
    known_activities,
    known_object_types,
    propose_hypotheses,
    rule_based_hypotheses,
    run_hypothesis_loop,
    sanitize,
    verify_all,
    verify_hypothesis,
)
from src.llm_client import LLMClient, NullProvider, ScriptedProvider
from src.llm_context import MiningFacts
from src.ocel_loader import load_ocel

DATASET = "data/sim/p2p_sim.jsonocel"


@pytest.fixture(scope="module")
def ocel():
    return load_ocel(DATASET)


@pytest.fixture(scope="module")
def facts(ocel):
    from src.llm_context import build_ocel_facts
    from src.ocel_cross_process import full_cross_process_report
    from src.ocel_loader import object_type_stats, summarize_ocel

    cross = full_cross_process_report(ocel, min_trigger_count=5)
    return build_ocel_facts(
        dataset="p2p_sim", summary=summarize_ocel(ocel), cross=cross,
        object_types=object_type_stats(ocel).to_dict(orient="records"))


# --------------------------------------------------------------------------
# 白名单
# --------------------------------------------------------------------------

def test_known_activities_and_types(ocel):
    acts = known_activities(ocel)
    otypes = known_object_types(ocel)
    assert "Send Order to Supplier" in acts
    assert "purchase_order" in otypes


def test_sanitize_drops_fabricated_activity(ocel):
    hset = HypothesisSet(hypotheses=[
        Hypothesis(name="真链路", rationale="r",
                   source_activity="Send Order to Supplier",
                   target_activity="Post Goods Receipt"),
        Hypothesis(name="编造链路", rationale="r",
                   source_activity="Hack The Mainframe",
                   target_activity="Post Goods Receipt"),
    ])
    kept, dropped = sanitize(hset, ocel)
    assert len(kept) == 1 and kept[0].name == "真链路"
    assert any("白名单" in d for d in dropped)


def test_sanitize_drops_unknown_object_type(ocel):
    hset = HypothesisSet(hypotheses=[Hypothesis(
        name="x", rationale="r", source_activity="Send Order to Supplier",
        target_activity="Post Goods Receipt", object_types=["not_a_type"])])
    kept, dropped = sanitize(hset, ocel)
    assert kept == [] and dropped


def test_sanitize_rejects_bad_time_range(ocel):
    hset = HypothesisSet(hypotheses=[Hypothesis(
        name="x", rationale="r", source_activity="Send Order to Supplier",
        target_activity="Post Goods Receipt", time_range=["2024-01-01"])])
    kept, _ = sanitize(hset, ocel)
    assert kept == []


# --------------------------------------------------------------------------
# 过滤
# --------------------------------------------------------------------------

def test_filter_ocel_by_object_type_narrows_events(ocel):
    sub = filter_ocel(ocel, object_types=["invoice"])
    assert len(sub.events) < len(ocel.events)
    assert set(sub.objects["ocel:type"]) == {"invoice"}


def test_filter_ocel_by_time_range(ocel):
    sub = filter_ocel(ocel, time_range=["2024-01-01", "2024-01-15"])
    assert len(sub.events) <= len(ocel.events)


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

def test_verify_known_main_path_is_supported(ocel):
    h = Hypothesis(name="订单→收货", rationale="r",
                   source_activity="Send Order to Supplier",
                   target_activity="Post Goods Receipt")
    v = verify_hypothesis(h, ocel)
    assert v.supported is True, f"主干链路应判成立: {v.evidence}"
    assert v.lift > 1.0 and v.sample_size > 0
    assert v.fdr_significant is True


def test_verify_fabricated_pair_is_not_supported(ocel):
    h = Hypothesis(name="反向链路", rationale="r",
                   source_activity="Clear Invoice",
                   target_activity="Create Purchase Order")
    v = verify_hypothesis(h, ocel)
    assert v.supported is False


def test_verify_all_skips_dropped_hypotheses(ocel):
    hset = HypothesisSet(hypotheses=[
        Hypothesis(name="ok", rationale="r",
                   source_activity="Send Order to Supplier",
                   target_activity="Post Goods Receipt"),
        Hypothesis(name="bad", rationale="r",
                   source_activity="Nope", target_activity="Nope"),
    ])
    verdicts, dropped = verify_all(hset, ocel)
    assert len(verdicts) == 1 and len(dropped) == 1


def test_verdict_is_deterministic(ocel):
    h = Hypothesis(name="x", rationale="r",
                   source_activity="Send Order to Supplier",
                   target_activity="Post Goods Receipt")
    a = verify_hypothesis(h, ocel)
    b = verify_hypothesis(h, ocel)
    assert (a.lift, a.p_value, a.supported) == (b.lift, b.p_value, b.supported)


# --------------------------------------------------------------------------
# 假设生成
# --------------------------------------------------------------------------

def test_rule_based_hypotheses_picks_low_frequency_paths(facts):
    hset = rule_based_hypotheses(facts, n=3)
    assert hset.source == "rule"
    assert len(hset.hypotheses) <= 3
    counts = [t["count"] for t in facts.cross_triggers]
    if counts:
        assert min(counts) <= min(
            next(t["count"] for t in facts.cross_triggers
                 if t["source_activity"] == h.source_activity
                 and t["target_activity"] == h.target_activity)
            for h in hset.hypotheses)


def test_propose_with_null_provider_falls_back(ocel, facts):
    hset = propose_hypotheses(facts, ocel, LLMClient(provider=NullProvider()))
    assert hset.source == "rule" and hset.hypotheses


def test_propose_rejects_fabricated_numbers(ocel, facts):
    payload = {"hypotheses": [{"name": "x", "rationale": "出现了 99999 次",
                               "source_activity": "Send Order to Supplier",
                               "target_activity": "Post Goods Receipt"}]}
    hset = propose_hypotheses(facts, ocel,
                              LLMClient(provider=ScriptedProvider(responses=[payload])))
    assert hset.source == "rule", "数字无法溯源应退回规则候选"


def test_propose_accepts_valid_llm_hypotheses(ocel, facts):
    payload = {"hypotheses": [{"name": "退货后仍开票",
                               "rationale": "该链路业务上说不通",
                               "source_activity": "Return to Supplier",
                               "target_activity": "Enter Incoming Invoice"}]}
    hset = propose_hypotheses(facts, ocel,
                              LLMClient(provider=ScriptedProvider(responses=[payload])))
    assert hset.source == "llm"
    assert hset.hypotheses[0].source_activity == "Return to Supplier"


# --------------------------------------------------------------------------
# 端到端闭环
# --------------------------------------------------------------------------

def test_run_hypothesis_loop_offline(ocel, facts):
    out = run_hypothesis_loop(facts, ocel, None)
    assert out["source"] == "rule"
    assert len(out["verdicts"]) == len([h for h in out["hypotheses"]])
    assert isinstance(out["supported_count"], int)
    assert out["dropped"] == []


def test_run_hypothesis_loop_with_llm(ocel, facts):
    payload = {"hypotheses": [
        {"name": "订单→收货", "rationale": "主干链路",
         "source_activity": "Send Order to Supplier",
         "target_activity": "Post Goods Receipt"},
        {"name": "越权链路", "rationale": "r",
         "source_activity": "Not An Activity", "target_activity": "Post Goods Receipt"},
    ]}
    client = LLMClient(provider=ScriptedProvider(responses=[payload]))
    out = run_hypothesis_loop(facts, ocel, client)
    assert out["source"] == "llm"
    assert len(out["verdicts"]) == 1, "白名单外的假设不进入判定"
    assert out["dropped"]
    assert out["verdicts"][0]["supported"] is True
