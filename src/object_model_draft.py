"""L1 对象模型抽取:从 BPMN 与业务表结构生成对象注册表候选。

流程:

    BPMN 活动名 + 业务表结构 → LLM 产出候选注册表 → 白名单校验(表/列必须存在)
        → 回灌 bpmn_converter 做一次真实转换 → 用质量指标判定是否达标
        → 输出 diff,**不自动改写 registry**(人工审核后再落地)

为什么必须回灌验证:LLM 产出的注册表"看起来合理"没有意义,
唯一有效的证据是转换后的孤立事件率与平均对象/事件。
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, Field

from .bpmn_registry import (
    DEFAULT_REGISTRY,
    ExpansionRule,
    O2ORule,
    ObjectRegistry,
    ObjectSpec,
)
from .llm_client import LLMClient, LLMUnavailableError

logger = logging.getLogger(__name__)

_NS = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
_TASK_TAGS = ("userTask", "serviceTask", "callActivity", "task", "manualTask",
              "sendTask", "receiveTask", "scriptTask", "businessRuleTask")

DRAFT_SYSTEM_PROMPT = """你是对象中心流程挖掘的建模助手,负责从 BPMN 活动与业务表结构设计 OCEL 对象模型。

铁律:
1. 每个对象类型的 table 必须是【可用业务表】中的表名,id_cols/attrs 必须是该表真实存在的列。
2. o2o_rules 的 table 必须是可用表,join_cols 必须是该表真实存在的列。
3. 不要臆造表名或列名;不确定就少提,宁可少一个对象类型。
4. 对象类型数量控制在 4~12 个,过多说明抽象不够。
5. 只输出一个 JSON 对象。"""


class ObjectSpecDraft(BaseModel):
    name: str
    table: str
    id_cols: list[str]
    attrs: list[str] = Field(default_factory=list)
    label: str = ""


class O2ORuleDraft(BaseModel):
    child_type: str
    parent_type: str
    join_cols: list[str]
    table: str
    qualifier: str = "composes"


class ExpansionRuleDraft(BaseModel):
    parent_type: str
    child_type: str
    table: str
    join_cols: list[str]
    activities: list[str] = Field(default_factory=list)
    qualifier: str = "contains"


class ObjectModelDraft(BaseModel):
    objects: list[ObjectSpecDraft] = Field(default_factory=list)
    o2o_rules: list[O2ORuleDraft] = Field(default_factory=list)
    expansion_rules: list[ExpansionRuleDraft] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source: str = Field(default="llm", description="llm | rule")
    model: str = ""


# --------------------------------------------------------------------------
# 输入摘要
# --------------------------------------------------------------------------

def bpmn_activity_names(bpmn_path: str | Path) -> list[str]:
    """从 BPMN 2.0 XML 抽取活动名(网关与开始/结束事件不算活动)。"""
    root = ET.parse(Path(bpmn_path)).getroot()
    names: list[str] = []
    for tag in _TASK_TAGS:
        for el in root.iter(f"{_NS}{tag}"):
            n = el.get("name")
            if n:
                names.append(n)
    return sorted(set(names))


def table_summaries(tables) -> dict[str, list[str]]:
    """业务表名 → 列名。给 LLM 的白名单依据。"""
    return {name: list(df.columns) for name, df in tables.business.items()}


def build_user_prompt(activities: list[str], schemas: dict[str, list[str]],
                      current: ObjectRegistry = DEFAULT_REGISTRY) -> str:
    schema = json.dumps(ObjectModelDraft.model_json_schema(), ensure_ascii=False)
    current_desc = [{"name": s.name, "table": s.table, "id_cols": list(s.id_cols)}
                    for s in current.objects]
    return (
        "【BPMN 活动】\n" + json.dumps(activities, ensure_ascii=False, indent=1) + "\n\n"
        "【可用业务表】\n" + json.dumps(schemas, ensure_ascii=False, indent=1) + "\n\n"
        "【当前注册表】\n" + json.dumps(current_desc, ensure_ascii=False, indent=1) + "\n\n"
        "【输出要求】\n"
        f"输出 JSON,结构:{schema}\n"
        "- table / id_cols / join_cols 只能取【可用业务表】中的真实表名与列名。\n"
        "- 目标是让每个活动都能关联到至少一个业务对象(孤立事件率为 0)。\n"
        "- 在 notes 中说明与当前注册表相比的关键改动。"
    )


# --------------------------------------------------------------------------
# 校验:白名单 + 回灌转换
# --------------------------------------------------------------------------

def draft_to_registry(draft: ObjectModelDraft,
                      base: ObjectRegistry = DEFAULT_REGISTRY) -> ObjectRegistry:
    """把候选转成可直接喂给转换器的 ObjectRegistry。"""
    return ObjectRegistry(
        objects=tuple(ObjectSpec(name=o.name, table=o.table, id_cols=tuple(o.id_cols),
                                 attrs=tuple(o.attrs), label=o.label)
                      for o in draft.objects),
        o2o_rules=tuple(O2ORule(child_type=r.child_type, parent_type=r.parent_type,
                                join_cols=tuple(r.join_cols), table=r.table,
                                qualifier=r.qualifier) for r in draft.o2o_rules),
        expansion_rules=tuple(ExpansionRule(parent_type=r.parent_type,
                                            child_type=r.child_type, table=r.table,
                                            join_cols=tuple(r.join_cols),
                                            activities=tuple(r.activities),
                                            qualifier=r.qualifier)
                              for r in draft.expansion_rules),
        variable_map=dict(base.variable_map),
        resource_object=base.resource_object,
    )


def _check_columns(draft: ObjectModelDraft, schemas: dict[str, list[str]]) -> list[str]:
    """白名单校验:表名与列名必须真实存在。"""
    issues: list[str] = []
    for o in draft.objects:
        cols = schemas.get(o.table)
        if cols is None:
            issues.append(f"对象类型 {o.name}: 表 {o.table} 不存在")
            continue
        bad = [c for c in list(o.id_cols) + list(o.attrs) if c not in cols]
        if bad:
            issues.append(f"对象类型 {o.name}: 列 {bad} 不在表 {o.table} 中")
    for r in list(draft.o2o_rules) + list(draft.expansion_rules):
        cols = schemas.get(r.table)
        if cols is None:
            issues.append(f"规则 {getattr(r, 'child_type', '')}: 表 {r.table} 不存在")
            continue
        bad = [c for c in r.join_cols if c not in cols]
        if bad:
            issues.append(f"规则 表 {r.table}: 列 {bad} 不存在")
    names = {o.name for o in draft.objects}
    for r in draft.o2o_rules + draft.expansion_rules:
        if r.parent_type not in names:
            issues.append(f"规则引用了未定义的对象类型: {r.parent_type}")
        if r.child_type not in names:
            issues.append(f"规则引用了未定义的对象类型: {r.child_type}")
    return issues


def validate_draft(draft: ObjectModelDraft, tables,
                   base: ObjectRegistry = DEFAULT_REGISTRY,
                   *, min_avg_objects: float = 1.5) -> dict:
    """回灌 bpmn_converter 做一次真实转换,用质量指标判定候选是否可用。"""
    from .bpmn_converter import convert

    schemas = table_summaries(tables)
    issues = _check_columns(draft, schemas)

    quality = None
    if not issues:
        try:
            quality = convert(tables, draft_to_registry(draft, base)).quality
            if quality["isolated_pct"] > 0:
                issues.append(f"孤立事件率 {quality['isolated_pct']}% > 0,关联未覆盖全部活动")
            if quality["avg_objects_per_event"] < min_avg_objects:
                issues.append(f"平均对象/事件 {quality['avg_objects_per_event']} "
                              f"< {min_avg_objects},多对象关联不足")
        except Exception as e:  # noqa: BLE001 - 候选模型本身可能不可转换
            issues.append(f"回灌转换失败: {e}")

    baseline = convert(tables, base).quality
    return {
        "ok": not issues,
        "issues": issues,
        "quality": quality,
        "baseline": {
            "num_objects": baseline["num_objects"],
            "num_object_types": baseline["num_object_types"],
            "avg_objects_per_event": baseline["avg_objects_per_event"],
            "isolated_pct": baseline["isolated_pct"],
        },
        "diff": {
            "added": sorted({o.name for o in draft.objects} - set(base.type_names)),
            "removed": sorted(set(base.type_names) - {o.name for o in draft.objects}),
        },
    }


def rule_based_draft(base: ObjectRegistry = DEFAULT_REGISTRY) -> ObjectModelDraft:
    """规则化降级:沿用当前注册表,不做任何改动建议。"""
    return ObjectModelDraft(
        objects=[ObjectSpecDraft(name=s.name, table=s.table, id_cols=list(s.id_cols),
                                 attrs=list(s.attrs), label=s.label)
                 for s in base.objects],
        o2o_rules=[O2ORuleDraft(child_type=r.child_type, parent_type=r.parent_type,
                                join_cols=list(r.join_cols), table=r.table,
                                qualifier=r.qualifier) for r in base.o2o_rules],
        expansion_rules=[ExpansionRuleDraft(parent_type=r.parent_type,
                                            child_type=r.child_type, table=r.table,
                                            join_cols=list(r.join_cols),
                                            activities=list(r.activities),
                                            qualifier=r.qualifier)
                         for r in base.expansion_rules],
        notes=["规则化降级:沿用当前注册表,未生成新候选。"],
        source="rule",
    )


def propose_object_model(bpmn_path: str | Path, tables, client: LLMClient | None,
                         base: ObjectRegistry = DEFAULT_REGISTRY) -> ObjectModelDraft:
    """LLM 产出候选;不可用 / 不合法一律退回现有注册表。"""
    if client is None or not client.available:
        return rule_based_draft(base)

    user = build_user_prompt(bpmn_activity_names(bpmn_path), table_summaries(tables), base)
    try:
        comp = client.complete(system=DRAFT_SYSTEM_PROMPT, user=user,
                               schema=ObjectModelDraft)
    except (LLMUnavailableError, Exception) as e:  # noqa: B014
        logger.warning("对象模型抽取失败,沿用当前注册表: %s", e)
        return rule_based_draft(base)

    draft: ObjectModelDraft = comp.obj
    draft.model = comp.model
    draft.source = "llm"

    # 数字溯源对这一层不适用(输出里基本无数字),但白名单必须过
    if _check_columns(draft, table_summaries(tables)):
        logger.warning("候选引用了不存在的表/列,沿用当前注册表")
        return rule_based_draft(base)
    return draft
