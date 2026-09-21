"""L3 假设验证闭环:LLM 提假设,pm4py 判定。

这是 pm4py × LLM 里价值最高的一层:

    LLM 提出候选异常 → 白名单过滤 → pm4py 算 lift + 卡方 + BH FDR → 判定成立与否

安全边界(重要):LLM 只能**选参数**,不能写代码。假设表达式被限制为
「活动名 + 对象类型 + 时间区间」的白名单组合,超出白名单的假设直接丢弃,
绝不做任意过滤表达式求值。

判定口径沿用 ocel_cross_process.activity_association_rules:
以对象上的**相邻转移**为事务单位算 lift,卡方检验后用 BH FDR 校正。
"""
from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from .llm_client import LLMClient, LLMUnavailableError
from .llm_context import MiningFacts, NumberProvenanceError, assert_numbers_from_facts
from .ocel_loader import ACT, EID, OID, OTYPE, TS

logger = logging.getLogger(__name__)

HYPOTHESIS_SYSTEM_PROMPT = """你是流程挖掘假设生成助手,负责提出"可能被忽视的异常链路"候选。

铁律:
1. source_activity 与 target_activity 必须是【已知活动】清单中的原样名称,不得自造。
2. object_types 必须是【已知对象类型】清单中的名称,可以为空。
3. rationale 中只能出现 FACTS 里的数字,禁止推算新数值。
4. 优先提"业务上说不通"的链路(例如已退货却仍开票),而不是频次最高的主干链路。
5. 只输出一个 JSON 对象,不要输出推理过程。"""


class Hypothesis(BaseModel):
    """一条可执行假设。字段即白名单参数,不含任何可执行表达式。"""

    name: str
    rationale: str
    source_activity: str = ""
    target_activity: str = ""
    object_types: list[str] = Field(default_factory=list)
    time_range: list[str] = Field(default_factory=list)


class HypothesisSet(BaseModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    source: str = Field(default="llm", description="llm | rule")
    model: str = ""


class HypothesisVerdict(BaseModel):
    hypothesis: str
    supported: bool
    lift: float = 0.0
    p_value: float = Field(default=1.0, description="BH 校正后的 p")
    fdr_significant: bool = False
    sample_size: int = 0
    evidence: str = ""

    def to_dict(self) -> dict:
        return self.model_dump()


# --------------------------------------------------------------------------
# 白名单与过滤
# --------------------------------------------------------------------------

def known_activities(ocel) -> list[str]:
    return sorted(set(ocel.relations[ACT].astype(str)))


def known_object_types(ocel) -> list[str]:
    return sorted(set(ocel.objects[OTYPE].astype(str)))


def filter_ocel(ocel, *, object_types: list[str] | None = None,
                activities: list[str] | None = None,
                time_range: list[str] | None = None):
    """按白名单参数切出一个子 OCEL。这是假设唯一能施加的作用。"""
    import pandas as pd
    from pm4py.objects.ocel.obj import OCEL

    rel = ocel.relations
    if object_types:
        rel = rel[rel[OTYPE].astype(str).isin(set(object_types))]
    if activities:
        rel = rel[rel[ACT].astype(str).isin(set(activities))]

    ev = ocel.events[ocel.events[EID].isin(set(rel[EID]))]

    if time_range and len(time_range) == 2:
        # 统一转 UTC:日志时间戳可能带时区,入参通常不带,直接比较会抛 TypeError
        start = pd.to_datetime(time_range[0], utc=True)
        end = pd.to_datetime(time_range[1], utc=True)
        ts = pd.to_datetime(ev[TS], utc=True)
        ev = ev[(ts >= start) & (ts <= end)]
        rel = rel[rel[EID].isin(set(ev[EID]))]

    ob = ocel.objects[ocel.objects[OID].isin(set(rel[OID]))]
    return OCEL(events=ev.reset_index(drop=True),
                objects=ob.reset_index(drop=True),
                relations=rel.reset_index(drop=True))


def sanitize(hset: HypothesisSet, ocel) -> tuple[list[Hypothesis], list[str]]:
    """白名单校验:活动名/对象类型不存在的假设直接丢弃,返回被丢弃原因。"""
    acts = set(known_activities(ocel))
    otypes = set(known_object_types(ocel))
    kept: list[Hypothesis] = []
    dropped: list[str] = []

    for h in hset.hypotheses:
        if h.source_activity not in acts or h.target_activity not in acts:
            dropped.append(f"{h.name}: 活动名不在白名单内"
                           f"({h.source_activity} → {h.target_activity})")
            continue
        unknown_ot = [t for t in h.object_types if t not in otypes]
        if unknown_ot:
            dropped.append(f"{h.name}: 对象类型不在白名单内({unknown_ot})")
            continue
        if h.time_range and len(h.time_range) != 2:
            dropped.append(f"{h.name}: 时间区间必须为 [起, 止]")
            continue
        kept.append(h)
    return kept, dropped


# --------------------------------------------------------------------------
# 判定:全部由 pm4py 计算
# --------------------------------------------------------------------------

def _rules_for(h: Hypothesis, ocel, rules=None, *, alpha: float, min_lift: float):
    from .ocel_cross_process import activity_association_rules

    if rules is not None:
        return rules
    scoped = h.object_types or h.time_range
    subset = filter_ocel(ocel, object_types=h.object_types or None,
                         time_range=h.time_range or None) if scoped else ocel
    return activity_association_rules(subset, min_lift=min_lift, alpha=alpha)


def verify_hypothesis(h: Hypothesis, ocel, *, rules=None,
                      alpha: float = 0.05, min_lift: float = 1.1) -> HypothesisVerdict:
    """用 pm4py 的统计检验判定一条假设。未进入检验 → 判为 inconclusive。"""
    rules = _rules_for(h, ocel, rules=rules, alpha=alpha, min_lift=min_lift)

    if rules is None or rules.empty:
        return HypothesisVerdict(hypothesis=h.name, supported=False,
                                 evidence="过滤后无可用转移,无法判定")

    hit = rules[(rules["source_activity"].astype(str) == h.source_activity)
                & (rules["target_activity"].astype(str) == h.target_activity)]
    if hit.empty:
        return HypothesisVerdict(
            hypothesis=h.name, supported=False,
            evidence=(f"{h.source_activity} → {h.target_activity} 未进入检验"
                      f"(lift < {min_lift} 或频次不足),判定为不成立"))

    r = hit.iloc[0]
    lift = float(r["lift"])
    p_adj = float(r["p_adjusted"])
    significant = bool(r["significant"])
    return HypothesisVerdict(
        hypothesis=h.name,
        supported=bool(significant and lift > 1.0),
        lift=round(lift, 3),
        p_value=p_adj,
        fdr_significant=significant,
        sample_size=int(r["n_ab"]),
        evidence=f"lift={lift} · p(校正后)={p_adj:.3g} · n={int(r['n_ab'])}",
    )


def verify_all(hset: HypothesisSet, ocel, *, alpha: float = 0.05,
               min_lift: float = 1.1) -> tuple[list[HypothesisVerdict], list[str]]:
    """批量判定。无作用域过滤的假设共用一份基础规则表,避免重复计算。"""
    from .ocel_cross_process import activity_association_rules

    kept, dropped = sanitize(hset, ocel)
    base_rules = None
    verdicts: list[HypothesisVerdict] = []

    for h in kept:
        if not (h.object_types or h.time_range):
            if base_rules is None:
                base_rules = activity_association_rules(ocel, min_lift=min_lift, alpha=alpha)
            verdicts.append(verify_hypothesis(h, ocel, rules=base_rules,
                                              alpha=alpha, min_lift=min_lift))
        else:
            verdicts.append(verify_hypothesis(h, ocel, alpha=alpha, min_lift=min_lift))
    return verdicts, dropped


# --------------------------------------------------------------------------
# 假设生成
# --------------------------------------------------------------------------

def build_user_prompt(facts: MiningFacts, ocel) -> str:
    activities = known_activities(ocel)
    otypes = known_object_types(ocel)
    schema = json.dumps(HypothesisSet.model_json_schema(), ensure_ascii=False)
    return (
        f"{facts.to_prompt_block()}\n\n"
        f"【已知活动】{json.dumps(activities, ensure_ascii=False)}\n"
        f"【已知对象类型】{json.dumps(otypes, ensure_ascii=False)}\n\n"
        "【输出要求】\n"
        f"输出 JSON,结构:{schema}\n"
        "- hypotheses 3~5 条,每条必须填 source_activity 与 target_activity。\n"
        "- 优先挑业务上说不通的低频链路,不要重复 FACTS 里已列出的主干链路。\n"
        "- rationale 只能使用 FACTS 中的数字。"
    )


def rule_based_hypotheses(facts: MiningFacts, n: int = 3) -> HypothesisSet:
    """确定性候选:低频跨流程路径往往对应异常或越权。"""
    triggers = sorted(facts.cross_triggers, key=lambda t: t.get("count", 0))
    picked = triggers[:n]
    hs = [
        Hypothesis(
            name=f"低频链路 {t.get('source_activity')} → {t.get('target_activity')}",
            rationale=(f"该跨流程路径仅出现 {t.get('count')} 次,"
                       f"中位时延 {t.get('median_delay_h')} 小时,可能是异常或越权路径。"),
            source_activity=str(t.get("source_activity")),
            target_activity=str(t.get("target_activity")),
        ) for t in picked
    ]
    return HypothesisSet(hypotheses=hs, source="rule")


def propose_hypotheses(facts: MiningFacts, ocel, client: LLMClient | None) -> HypothesisSet:
    """LLM 提假设。输出经 schema + 白名单 + 数字溯源三重校验,失败则退回规则候选。"""
    if client is None or not client.available:
        return rule_based_hypotheses(facts)

    user = build_user_prompt(facts, ocel)
    try:
        comp = client.complete(system=HYPOTHESIS_SYSTEM_PROMPT, user=user,
                               schema=HypothesisSet)
    except (LLMUnavailableError, Exception) as e:  # noqa: B014
        logger.warning("假设生成失败,退回规则候选: %s", e)
        return rule_based_hypotheses(facts)

    hset: HypothesisSet = comp.obj
    hset.model = comp.model
    hset.source = "llm"

    text = "\n".join([h.name + " " + h.rationale for h in hset.hypotheses])
    try:
        assert_numbers_from_facts(text, facts)
    except NumberProvenanceError as e:
        logger.warning("假设文本数字无法溯源,退回规则候选: %s", e)
        return rule_based_hypotheses(facts)
    return hset


def render_hypothesis_section(result: dict | None,
                              title: str = "异常假设验证(LLM 提出 · pm4py 判定)") -> str:
    """把闭环结果渲染成报告章节。result 为 None 时不渲染。"""
    import html as _html

    if not result:
        return ""
    esc = lambda v: _html.escape(str(v)) if v is not None else ""

    rows = ""
    for h, v in zip(result.get("hypotheses", []), result.get("verdicts", [])):
        badge = "badge-bad" if v.get("supported") else "badge-ok"
        label = "成立" if v.get("supported") else "不成立"
        rows += (
            f"<tr><td>{esc(h.get('name'))}</td>"
            f"<td class='muted'>{esc(h.get('source_activity'))} → "
            f"{esc(h.get('target_activity'))}</td>"
            f"<td class='muted'>{esc(h.get('rationale'))}</td>"
            f"<td><span class='badge {badge}'>{label}</span></td>"
            f"<td class='num'>{esc(v.get('lift'))}</td>"
            f"<td class='muted'>{esc(v.get('evidence'))}</td></tr>")
    if not rows:
        rows = '<tr><td colspan="6" class="muted">未产生可判定的假设</td></tr>'

    dropped = "".join(f"<li>{esc(d)}</li>" for d in (result.get("dropped") or []))
    dropped_block = (f"<h3>被白名单拦截</h3><ul>{dropped}</ul>" if dropped else "")
    src_note = ("假设由 LLM 提出,判定全部由 pm4py 完成(lift + 卡方 + BH FDR 校正)"
                if result.get("source") == "llm" else "假设与判定均由规则化降级路径产生")

    return f"""<section>
  <h2>{esc(title)}</h2>
  <p class="muted">{src_note} · 成立 {result.get('supported_count', 0)} 条</p>
  <table>
    <thead><tr><th>假设</th><th>链路</th><th>提出理由</th><th>判定</th>
      <th>lift</th><th>统计证据</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {dropped_block}
</section>
"""


def run_hypothesis_loop(facts: MiningFacts, ocel, client: LLMClient | None,
                        *, alpha: float = 0.05, min_lift: float = 1.1) -> dict:
    """完整闭环:提假设 → 白名单 → 判定。返回可直接渲染进报告的结构。"""
    hset = propose_hypotheses(facts, ocel, client)
    verdicts, dropped = verify_all(hset, ocel, alpha=alpha, min_lift=min_lift)
    return {
        "source": hset.source,
        "model": hset.model,
        "hypotheses": [{"name": h.name, "rationale": h.rationale,
                        "source_activity": h.source_activity,
                        "target_activity": h.target_activity}
                       for h in hset.hypotheses],
        "verdicts": [v.to_dict() for v in verdicts],
        "dropped": dropped,
        "supported_count": sum(1 for v in verdicts if v.supported),
    }
