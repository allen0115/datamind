"""L1 对象模型抽取测试。

核心是「回灌验证」:候选注册表必须真的能跑出达标的转换质量,
否则即使 LLM 说得头头是道也必须判为不可用。
"""
from __future__ import annotations

import pytest

from src.bpmn_registry import DEFAULT_REGISTRY
from src.bpmn_simulator import SimConfig, simulate
from src.llm_client import LLMClient, NullProvider, ScriptedProvider
from src.object_model_draft import (
    bpmn_activity_names,
    draft_to_registry,
    propose_object_model,
    rule_based_draft,
    table_summaries,
    validate_draft,
)

BPMN = "data/sim/p2p_sim.bpmn"


@pytest.fixture(scope="module")
def tables():
    return simulate(SimConfig(n_requisitions=30, seed=7))


def test_bpmn_activity_names_extracts_tasks_only():
    names = bpmn_activity_names(BPMN)
    assert "Create Purchase Order" in names
    assert "是否需审批" not in names, "网关不是活动"
    assert "采购需求提出" not in names, "开始事件不是活动"


def test_table_summaries_lists_business_tables(tables):
    s = table_summaries(tables)
    assert "purchase_order" in s and "po_no" in s["purchase_order"]
    assert "employee" in s


def test_rule_based_draft_equals_current_registry():
    d = rule_based_draft()
    assert d.source == "rule"
    assert {o.name for o in d.objects} == set(DEFAULT_REGISTRY.type_names)


def test_draft_to_registry_is_convertible(tables):
    reg = draft_to_registry(rule_based_draft())
    assert set(reg.type_names) == set(DEFAULT_REGISTRY.type_names)
    assert reg.variable_map == DEFAULT_REGISTRY.variable_map


def test_validate_baseline_draft_is_ok(tables):
    result = validate_draft(rule_based_draft(), tables)
    assert result["ok"] is True, result["issues"]
    assert result["diff"]["added"] == [] and result["diff"]["removed"] == []
    assert result["quality"]["isolated_pct"] == 0


def test_validate_rejects_unknown_table(tables):
    d = rule_based_draft()
    d.objects[0].table = "no_such_table"
    result = validate_draft(d, tables)
    assert result["ok"] is False
    assert any("不存在" in i for i in result["issues"])


def test_validate_rejects_unknown_column(tables):
    d = rule_based_draft()
    d.objects[0].id_cols = ["not_a_column"]
    result = validate_draft(d, tables)
    assert result["ok"] is False
    assert any("不在表" in i for i in result["issues"])


def test_validate_detects_dropped_object_type(tables):
    d = rule_based_draft()
    d.objects = [o for o in d.objects if o.name != "invoice"]
    # 发票对象类型被去掉后,引用它的规则也要一起去掉,否则先报类型未定义
    d.o2o_rules = [r for r in d.o2o_rules
                   if r.child_type != "invoice" and r.parent_type != "invoice"]
    d.expansion_rules = [r for r in d.expansion_rules
                         if r.child_type != "invoice" and r.parent_type != "invoice"]
    result = validate_draft(d, tables)
    assert result["diff"]["removed"] == ["invoice"]
    assert "invoice" in result["diff"]["removed"]


def test_propose_with_null_provider_returns_rule_draft(tables):
    d = propose_object_model(BPMN, tables, LLMClient(provider=NullProvider()))
    assert d.source == "rule"


def test_propose_rejects_hallucinated_table(tables):
    payload = {"objects": [{"name": "ghost", "table": "ghost_table",
                            "id_cols": ["ghost_id"]}],
               "o2o_rules": [], "expansion_rules": [], "notes": []}
    d = propose_object_model(BPMN, tables,
                             LLMClient(provider=ScriptedProvider(responses=[payload])))
    assert d.source == "rule", "引用不存在的表必须退回现有注册表"


def test_propose_accepts_valid_draft(tables):
    payload = {
        "objects": [
            {"name": "purchase_order", "table": "purchase_order", "id_cols": ["po_no"],
             "attrs": ["amount"], "label": "采购订单"},
            {"name": "invoice", "table": "invoice", "id_cols": ["invoice_no"],
             "attrs": ["amount"], "label": "发票"},
            {"name": "employee", "table": "employee", "id_cols": ["employee_id"],
             "attrs": ["role"], "label": "员工"},
        ],
        "o2o_rules": [{"child_type": "invoice", "parent_type": "purchase_order",
                       "join_cols": ["po_no"], "table": "invoice",
                       "qualifier": "billed_for"}],
        "expansion_rules": [],
        "notes": ["精简版模型"],
    }
    d = propose_object_model(BPMN, tables,
                             LLMClient(provider=ScriptedProvider(responses=[payload])))
    assert d.source == "llm"
    assert {o.name for o in d.objects} == {"purchase_order", "invoice", "employee"}


def test_simplified_draft_is_scored_by_quality(tables):
    """精简模型(只留 3 类对象)仍要跑真实转换,质量由数据说话。"""
    d = rule_based_draft()
    keep = {"purchase_order", "invoice", "employee"}
    d.objects = [o for o in d.objects if o.name in keep]
    d.o2o_rules = [r for r in d.o2o_rules
                   if r.child_type in keep and r.parent_type in keep]
    d.expansion_rules = []
    result = validate_draft(d, tables)
    assert result["quality"] is not None
    assert result["diff"]["removed"], "应有对象类型被移除"
