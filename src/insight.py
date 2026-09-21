"""L0 结果解读:把 pm4py 的统计结果翻译成业务语言。

这是"pm4py × LLM"最稳妥的一层:LLM 不动任何一个数字,只做三件事
  1. 把聚合指标解释成业务含义
  2. 指出该去哪里查(证据锚点)
  3. 给行动建议

三道防幻觉闸门:
  - 输出必须符合 InsightResult schema,否则视为调用失败
  - 每个数字必须能在 MiningFacts 中溯源,否则先带修正意见重试一次
  - 重试仍不通过或 LLM 不可用 → 退回 rule_based_insight(纯确定性规则)
"""
from __future__ import annotations

import html
import json
import logging

from pydantic import BaseModel, Field

from .llm_client import LLMClient, LLMUnavailableError
from .llm_context import MiningFacts, NumberProvenanceError, assert_numbers_from_facts

logger = logging.getLogger(__name__)

# 静态系统提示:不含任何动态变量,便于 prompt 缓存与复用
INSIGHT_SYSTEM_PROMPT = """你是流程挖掘分析助手,负责把 pm4py 的统计结果翻译成业务语言。

铁律:
1. 只能使用 FACTS 中出现过的数字,禁止推算、估算或补充任何新数值。
2. 每条结论必须给出 metric_refs(FACTS 中的指标名)与 evidence(FACTS 中的锚点)。
3. 结论要落到"这意味着什么、该查什么",不要复述数字。
4. 只输出一个 JSON 对象,不要输出推理过程或任何解释文字。"""


class Finding(BaseModel):
    claim: str
    metric_refs: list[str] = Field(default_factory=list)
    severity: str = Field(default="info", description="info | warn | critical")
    evidence: str = ""


class InsightResult(BaseModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    source: str = Field(default="llm", description="llm | rule")
    model: str = ""

    def to_dict(self) -> dict:
        return self.model_dump()

    def all_text(self) -> str:
        parts = [self.summary, *(f.claim for f in self.findings),
                 *(f.evidence for f in self.findings), *self.recommendations]
        return "\n".join(p for p in parts if p)


def build_user_prompt(facts: MiningFacts) -> str:
    schema = json.dumps(InsightResult.model_json_schema(), ensure_ascii=False)
    return (
        f"{facts.to_prompt_block()}\n\n"
        "【输出要求】\n"
        f"输出 JSON,结构:{schema}\n"
        "- findings 3~6 条,severity 取 info / warn / critical。\n"
        "- metric_refs 只能填【指标】中出现过的名字。\n"
        "- evidence 只能填【指标】对应的 evidence 锚点。\n"
        "- 禁止出现 FACTS 之外的任何数字。"
    )


# --------------------------------------------------------------------------
# 规则化降级:LLM 不可用时的确定性解读
# --------------------------------------------------------------------------

def rule_based_insight(facts: MiningFacts, reason: str = "") -> InsightResult:
    """不经过 LLM 的确定性解读。所有阈值判断只依赖 facts 中的数字。"""
    m = facts.metrics
    findings: list[Finding] = []

    fitness = m.get("fitness")
    if fitness is not None:
        sev = "info" if fitness >= 0.9 else ("warn" if fitness >= 0.75 else "critical")
        findings.append(Finding(
            claim=(f"模型拟合度 {fitness}:"
                   f"{'多数实例符合主流程' if fitness >= 0.9 else '存在较多偏离主流程的实例'}。"
                   f"不拟合实例 {m.get('non_fitting_cases', 0)} 个。"),
            metric_refs=["fitness", "non_fitting_cases"],
            severity=sev,
            evidence=facts.evidence.get("fitness", ""),
        ))

    breach = m.get("sla_breach_rate")
    if breach is not None:
        sev = "info" if breach < 0.1 else ("warn" if breach < 0.3 else "critical")
        findings.append(Finding(
            claim=(f"SLA 偏离率 {breach}(阈值 {m.get('sla_days')} 天),"
                   f"偏离实例 {m.get('sla_breach_count', 0)} 个。"),
            metric_refs=["sla_breach_rate", "sla_breach_count", "sla_days"],
            severity=sev,
            evidence=facts.evidence.get("sla_breach_rate", ""),
        ))

    p90 = m.get("case_duration_p90_days")
    if p90 is not None:
        findings.append(Finding(
            claim=f"端到端周期 P90 为 {p90} 天,中位数 {m.get('case_duration_median_days')} 天,"
                  f"长尾明显时优先查最慢的瓶颈活动。",
            metric_refs=["case_duration_p90_days", "case_duration_median_days"],
            severity="info",
            evidence=facts.evidence.get("case_duration_p90_days", ""),
        ))

    if facts.bottlenecks:
        b = facts.bottlenecks[0]
        findings.append(Finding(
            claim=f"首要瓶颈活动为 {b.get('concept:name') or b.get('activity', '未知')},"
                  f"平均等待 {b.get('mean', 0)} 小时,P90 {b.get('p90', 0)} 小时。",
            metric_refs=["bottleneck"], severity="warn",
            evidence=facts.evidence.get("top_variants", ""),
        ))

    if facts.scope == "ocel":
        iso = m.get("isolated_pct")
        if iso is not None:
            findings.append(Finding(
                claim=f"孤立事件占比 {iso}%,"
                      f"{'对象关联完整,跨流程分析成立' if iso == 0 else '存在无对象关联的事件,需修关联规则'}。",
                metric_refs=["isolated_pct"],
                severity="info" if iso == 0 else "critical",
                evidence="",
            ))
        avg = m.get("avg_objects_per_event")
        if avg is not None and avg < 1.5:
            findings.append(Finding(
                claim=f"平均对象/事件仅 {avg},多对象关联未建立,跨流程分析价值有限。",
                metric_refs=["avg_objects_per_event"], severity="warn", evidence=""))

    if not findings:
        findings.append(Finding(claim="指标均在正常区间,未见显著异常。",
                                metric_refs=[], severity="info", evidence=""))

    summary = f"规则化解读(LLM 不可用{'：' + reason if reason else ''}),共 {len(findings)} 条结论。"
    return InsightResult(summary=summary, findings=findings, recommendations=[], source="rule")


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def generate_insight(facts: MiningFacts, client: LLMClient) -> InsightResult:
    """生成解读。任何环节失败都退回规则化结果,绝不抛给调用方。"""
    system = INSIGHT_SYSTEM_PROMPT
    user = build_user_prompt(facts)

    try:
        comp = client.complete(system=system, user=user, schema=InsightResult)
    except LLMUnavailableError as e:
        logger.warning("LLM 不可用,退回规则化解读: %s", e)
        return rule_based_insight(facts, reason=str(e))
    except Exception as e:  # pragma: no cover - 兜底
        logger.warning("LLM 调用异常,退回规则化解读: %s", e)
        return rule_based_insight(facts, reason=str(e))

    result: InsightResult = comp.obj
    result.model = comp.model
    result.source = "llm"

    try:
        assert_numbers_from_facts(result.all_text(), facts)
        return result
    except NumberProvenanceError as e:
        logger.warning("首次输出数字无法溯源,带修正意见重试: %s", e)
        violations = list(e.violations)

    fix_user = (f"{user}\n\n【修正】上一次输出中的这些数字不在 FACTS 中,禁止使用:"
                f"{', '.join(violations[:10])}。请只用 FACTS 中的数字重写。")
    try:
        comp2 = client.complete(system=system, user=fix_user, schema=InsightResult)
        retry: InsightResult = comp2.obj
        retry.model = comp2.model
        retry.source = "llm"
        assert_numbers_from_facts(retry.all_text(), facts)
        return retry
    except (LLMUnavailableError, NumberProvenanceError) as e:
        logger.warning("重试后仍不合规,退回规则化解读: %s", e)
        return rule_based_insight(facts, reason=str(e))


def maybe_generate_insight(facts: MiningFacts, client: LLMClient | None) -> InsightResult | None:
    """LLM 未启用时返回 None(报告不渲染该章节),启用时才生成解读。"""
    if client is None or not client.available:
        return None
    return generate_insight(facts, client)


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------

_BADGE = {"info": "badge-ok", "warn": "badge-warn", "critical": "badge-bad"}
_LABEL = {"info": "提示", "warn": "关注", "critical": "严重"}


def render_insight_section(insight, title: str = "智能解读") -> str:
    """把 InsightResult 渲染成报告章节。insight 为 None 时返回空串(报告不变)。"""
    if insight is None:
        return ""
    data = insight.to_dict() if isinstance(insight, InsightResult) else dict(insight)
    esc = lambda v: html.escape(str(v)) if v is not None else ""

    findings = data.get("findings") or []
    rows = ""
    for f in findings:
        badge = _BADGE.get(f.get("severity", "info"), "badge-ok")
        label = _LABEL.get(f.get("severity", "info"), "提示")
        refs = ", ".join(f.get("metric_refs") or []) or "—"
        rows += (f"<tr><td>{esc(f.get('claim'))}</td><td class='muted'>{esc(refs)}</td>"
                 f"<td><span class='badge {badge}'>{esc(label)}</span></td>"
                 f"<td class='muted'>{esc(f.get('evidence'))}</td></tr>")
    if not rows:
        rows = '<tr><td colspan="4" class="muted">无结论</td></tr>'

    recs = "".join(f"<li>{esc(r)}</li>" for r in (data.get("recommendations") or []))
    rec_block = f"<h3>行动建议</h3><ul>{recs}</ul>" if recs else ""

    source_note = ("由 LLM 生成,所有数字取自本报告上方 pm4py 的计算结果"
                   if data.get("source") == "llm"
                   else "规则化降级生成(LLM 未启用或调用失败)")
    model_note = f" · 模型 {esc(data.get('model'))}" if data.get("model") else ""

    return f"""<section>
  <h2>{esc(title)}</h2>
  <p class="muted">{source_note}{model_note}</p>
  <div class="info-box">{esc(data.get('summary'))}</div>
  <table>
    <thead><tr><th>结论</th><th>依据指标</th><th>级别</th><th>证据锚点</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {rec_block}
</section>
"""
