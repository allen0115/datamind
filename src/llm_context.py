"""LLM 上下文交接层:pm4py 与 LLM 之间唯一的数据契约。

这一层解决三件事:

1. **压缩**:pm4py 的原始结果(上万行事件、上百条变体)不可能塞进上下文。
   这里只保留聚合指标 + 分层采样后的代表迹 + 证据锚点,单份 payload 控制在数 KB。
2. **采样**:变体按**分层**采样,而不是截断 Top-N。高频变体各取代表,
   低频(异常)变体优先保留 —— 截断会让 LLM 只看到一种流程形态。
3. **溯源**:`assert_numbers_from_facts()` 校验 LLM 输出里出现的每个数字
   都能在 facts 中找到。找不到即判定为编造,直接拒绝该次输出。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)%")

# 序号/枚举容忍:LLM 说"第 3 点""三个建议"这类数字不算编造指标
_ORDINAL_TOLERANCE = range(0, 21)


class NumberProvenanceError(ValueError):
    """LLM 输出中出现了 facts 里不存在的数字 —— 判定为编造。"""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(f"数字无法溯源(疑似编造): {', '.join(violations[:8])}")


# --------------------------------------------------------------------------
# 变体分层采样
# --------------------------------------------------------------------------

def stratified_variants(variants: list[dict], max_n: int = 8) -> list[dict]:
    """分层采样代表迹:高频取头部,低频(异常)优先保留若干条。

    variants 为 get_variants() 的返回:[{"variant": str, "count": int}]。
    """
    if not variants or max_n <= 0:
        return []

    seen: set[str] = set()
    rows: list[dict] = []
    for v in variants:
        key = str(v.get("variant", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({"variant": key, "count": int(v.get("count", 0) or 0)})

    if len(rows) <= max_n:
        return rows

    counts = sorted(r["count"] for r in rows)
    median = counts[len(counts) // 2]
    high = sorted([r for r in rows if r["count"] >= median],
                  key=lambda r: -r["count"])
    rare = sorted([r for r in rows if r["count"] < median],
                  key=lambda r: r["count"])

    # 罕见变体至少占 1/3 名额(异常模式往往藏在这里)
    rare_quota = max(1, max_n // 3)
    picked = rare[:rare_quota] + high[: max_n - rare_quota]
    # 补齐:名额未用满时用剩余高频填补
    picked_keys = {r["variant"] for r in picked}
    for r in high:
        if len(picked) >= max_n:
            break
        if r["variant"] not in picked_keys:
            picked.append(r)
            picked_keys.add(r["variant"])
    return picked[:max_n]


# --------------------------------------------------------------------------
# 事实契约
# --------------------------------------------------------------------------

class MiningFacts(BaseModel):
    """喂给 LLM 的全部事实。每个数字都由 pm4py 计算产生。"""

    dataset: str
    scope: str = Field(default="single", description="single | ocel")
    metrics: dict[str, Any] = Field(default_factory=dict)
    top_variants: list[dict] = Field(default_factory=list)
    bottlenecks: list[dict] = Field(default_factory=list)
    cross_triggers: list[dict] = Field(default_factory=list)
    object_types: list[dict] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump()

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1)

    def to_prompt_block(self) -> str:
        """给 LLM 看的紧凑文本(比 JSON 省 token,且数字更易对齐)。"""
        lines = [f"数据集: {self.dataset}（视角: {self.scope}）", "", "【指标】"]
        lines += [f"  {k} = {v}" for k, v in self.metrics.items()]
        if self.top_variants:
            lines.append("")
            lines.append("【代表迹(分层采样)】")
            lines += [f"  {v['count']} 次: {v['variant']}" for v in self.top_variants]
        if self.bottlenecks:
            lines.append("")
            lines.append("【瓶颈活动】")
            lines += [f"  {b.get('concept:name') or b.get('activity', '?')}: "
                      f"均值 {b.get('mean', 0)} 小时 / p90 {b.get('p90', 0)} 小时 / "
                      f"impact {b.get('impact_score', 0)}"
                      for b in self.bottlenecks]
        if self.cross_triggers:
            lines.append("")
            lines.append("【跨流程触发】")
            lines += [f"  {t.get('source_activity')} → {t.get('target_activity')}: "
                      f"{t.get('count')} 次" for t in self.cross_triggers]
        if self.object_types:
            lines.append("")
            lines.append("【对象类型】")
            lines += [f"  {o.get('object_type')}: {o.get('num_objects')} 个对象 / "
                      f"{o.get('num_events')} 事件" for o in self.object_types]
        if self.notes:
            lines.append("")
            lines += [f"【说明】 {n}" for n in self.notes]
        return "\n".join(lines)

    # ---- 数字溯源 ----

    def allowed_numbers(self) -> set[float]:
        """facts 中出现过的所有数字(含 ×100 的百分比形式)。"""
        vals: set[float] = set()
        for v in _iter_numbers(self.to_dict()):
            vals.add(round(v, 4))
            vals.add(round(v, 2))
            vals.add(round(v * 100, 2))
        vals.update(float(i) for i in _ORDINAL_TOLERANCE)
        return vals

    def _known_tokens(self) -> list[str]:
        """需要从待校验文本中抹掉的已知词(指标名、活动名、变体名等)。"""
        tokens: list[str] = [self.dataset, self.scope]
        for k in self.metrics:
            tokens.append(k)
            tokens.extend(p for p in str(k).split("_") if len(p) > 1)
        for row in self.top_variants:
            tokens.append(str(row.get("variant", "")))
        for row in self.bottlenecks + self.cross_triggers + self.object_types:
            tokens.extend(str(v) for v in row.values())
        for v in self.evidence.values():
            tokens.append(str(v))
        return [t for t in tokens if t]


def _iter_numbers(obj: Any):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
    elif isinstance(obj, str):
        for m in _NUM_RE.findall(obj):
            yield float(m)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_numbers(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_numbers(v)


def assert_numbers_from_facts(text: str, facts: MiningFacts) -> None:
    """数字溯源:文本中出现的数字必须能在 facts 中找到,否则抛错。

    先抹掉已知词(指标名、活动名、变体名),避免 "p90_days" 里的 90
    被误判成编造数字。
    """
    # 大小写不敏感:文本里写作 "P90" 而指标名是 "p90_days" 也要能抹掉
    cleaned = text
    for token in sorted(facts._known_tokens(), key=len, reverse=True):
        cleaned = re.sub(re.escape(token), " ", cleaned, flags=re.IGNORECASE)

    # 百分比先单独处理:92% 视为 0.92 的另一种写法
    cleaned = _PCT_RE.sub(lambda m: m.group(1), cleaned)

    allowed = facts.allowed_numbers()
    violations: list[str] = []
    for m in _NUM_RE.findall(cleaned):
        v = float(m)
        if round(v, 4) in allowed or round(v, 2) in allowed:
            continue
        violations.append(m)
    if violations:
        raise NumberProvenanceError(violations)


# --------------------------------------------------------------------------
# 构造器:单流程视角
# --------------------------------------------------------------------------

def build_single_facts(
    *,
    dataset: str,
    summary: dict,
    process_stats: dict,
    conformance: dict,
    performance: dict,
    variants: list[dict],
    max_variants: int = 8,
    sla_days: int = 14,
    report_path: str | None = None,
) -> MiningFacts:
    """把 src.main 流水线的结果压缩成 MiningFacts。"""
    case_dur = performance.get("case_durations", {}) or {}
    sla = performance.get("sla", {}) or {}
    metrics: dict[str, Any] = {
        "num_events": int(summary.get("num_events", 0)),
        "num_cases": int(summary.get("num_cases", 0)),
        "num_activities": int(summary.get("num_activities", 0)),
        "num_variants_sampled": len(variants),
        "fitness": round(float(conformance.get("fitness", 0)), 4),
        "fitting_cases": int(conformance.get("num_fitting", 0)),
        "non_fitting_cases": int(conformance.get("num_non_fitting", 0)),
        "case_duration_mean_days": float(case_dur.get("mean_days", 0)),
        "case_duration_median_days": float(case_dur.get("median_days", 0)),
        "case_duration_p90_days": float(case_dur.get("p90_days", 0)),
        "case_duration_max_days": float(case_dur.get("max_days", 0)),
        "sla_days": int(sla.get("sla_days", sla_days)),
        "sla_breach_count": int(sla.get("breach_count", 0)),
        "sla_breach_rate": float(sla.get("breach_rate", 0)),
        "num_transitions": int(process_stats.get("num_transitions", 0)),
        "num_places": int(process_stats.get("num_places", 0)),
    }
    if summary.get("num_resources") is not None:
        metrics["num_resources"] = int(summary["num_resources"])

    # evidence 始终填充:有报告文件就给文件锚点,否则给逻辑章节名,
    # 保证 LLM 每条结论都能挂上证据。
    anchor = f"{report_path}#" if report_path else "本报告 §"
    evidence = {
        "fitness": anchor + "合规检查",
        "sla_breach_rate": anchor + "性能与瓶颈",
        "case_duration_p90_days": anchor + "性能与瓶颈",
        "top_variants": anchor + "流程变体",
    }

    notes = [
        "所有数字均由 pm4py 计算,禁止推算或补充新的数值。",
        "引用指标时必须同时给出 evidence 中的章节锚点。",
    ]
    return MiningFacts(
        dataset=dataset,
        scope="single",
        metrics=metrics,
        top_variants=stratified_variants(variants, max_variants),
        bottlenecks=list((performance.get("bottleneck") or [])[:5]),
        evidence=evidence,
        notes=notes,
    )


# --------------------------------------------------------------------------
# 构造器:OCEL 对象中心视角
# --------------------------------------------------------------------------

def build_ocel_facts(
    *,
    dataset: str,
    summary: dict,
    cross: dict,
    object_types: list[dict],
    max_triggers: int = 10,
    report_path: str | None = None,
) -> MiningFacts:
    """把 src.ocel_main 流水线的结果压缩成 MiningFacts。"""
    assoc = cross.get("association_summary", {}) or {}
    metrics: dict[str, Any] = {
        "num_events": int(summary.get("num_events", 0)),
        "num_objects": int(summary.get("num_objects", 0)),
        "num_object_types": int(summary.get("num_object_types", 0)),
        "num_relations": int(summary.get("num_relations", 0)),
        "avg_objects_per_event": float(summary.get("avg_objects_per_event", 0)),
        "isolated_events": int(summary.get("isolated_events", 0)),
        "isolated_pct": float(summary.get("isolated_pct", 0)),
        "association_rules_tested": int(assoc.get("tested", 0)),
        "association_rules_significant": int(assoc.get("significant", 0)),
        "association_rules_cross_domain": int(assoc.get("cross_domain", 0)),
    }
    triggers = []
    for t in (cross.get("cross_process_triggers") or [])[:max_triggers]:
        triggers.append({
            "source_activity": t.get("source_activity"),
            "target_activity": t.get("target_activity"),
            "count": int(t.get("count", 0)),
            "median_delay_h": round(float(t.get("median_delay_h", 0) or 0), 2),
        })

    anchor = f"{report_path}#" if report_path else "本报告 §"
    evidence = {
        "cross_process_triggers": anchor + "跨流程触发关系",
        "association_rules_significant": anchor + "统计显著性验证",
    }

    return MiningFacts(
        dataset=dataset,
        scope="ocel",
        metrics=metrics,
        cross_triggers=triggers,
        object_types=[
            {"object_type": o.get("object_type"),
             "num_objects": int(o.get("num_objects", 0)),
             "num_events": int(o.get("num_events", 0))}
            for o in object_types[:10]
        ],
        evidence=evidence,
        notes=[
            "所有数字均由 pm4py 计算,禁止推算或补充新的数值。",
            "触发次数高不代表异常,需结合显著性与业务语义判断。",
        ],
    )
