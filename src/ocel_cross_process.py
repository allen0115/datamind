"""跨流程关系挖掘:发现"流程 A 触发流程 B/C"。

三条分析路径:

1. **对象交互图** —— 一个事件同时关联哪些对象类型。
   这是流程之间的"接缝":比如"收货"事件同时碰采购订单和物料凭证,
   说明采购流程和库存流程在此处耦合。

2. **对象内活动转移** —— 沿单个对象的时间线,统计活动 A 之后接活动 B
   的频次与时延。这是最直接的"谁触发谁"证据。

3. **跨流程触发关系** —— 在(2)的基础上,按活动的"主导对象类型"给活动
   归类到子流程,筛出源活动与目标活动属于不同子流程的转移。
   这就是用户要的"流程 A 触发流程 B"。
"""
from __future__ import annotations

import logging
from itertools import combinations
from typing import Any

import pandas as pd

from .ocel_loader import ACT, EID, OID, OTYPE, TS

logger = logging.getLogger(__name__)

# 资源类关联的限定符:它们标识"谁做的",不标识"属于哪个业务子流程",
# 因此在计算活动的主导对象类型时必须排除,否则会把同一流程内的相邻活动
# 误判为跨流程(见 activity_object_profile 的说明)。
_RESOURCE_QUALIFIERS = ("performer",)


# ---------------------------------------------------------------- 对象交互图

def object_interaction_graph(ocel: Any) -> pd.DataFrame:
    """
    统计对象类型两两在同一个事件中共现的次数。

    返回列:type_a, type_b, co_events, shared_activities
    co_events 越高,说明两个子流程耦合越紧。
    """
    rel = ocel.relations[[EID, OTYPE, ACT]].drop_duplicates()
    # 每个事件涉及的对象类型集合
    ev_types = rel.groupby(EID)[OTYPE].apply(lambda s: sorted(set(s)))

    pair_count: dict[tuple[str, str], int] = {}
    pair_acts: dict[tuple[str, str], set] = {}

    for eid, types in ev_types.items():
        if len(types) < 2:
            continue
        for a, b in combinations(types, 2):
            key = (a, b)
            pair_count[key] = pair_count.get(key, 0) + 1

    # 补充每个类型对涉及的公共活动
    rel2 = rel.copy()
    ev_to_acts = rel2.groupby(EID)[ACT].apply(lambda s: set(s))
    for eid, types in ev_types.items():
        if len(types) < 2:
            continue
        for a, b in combinations(types, 2):
            pair_acts.setdefault((a, b), set()).update(ev_to_acts.get(eid, set()))

    rows = [
        {
            "type_a": a,
            "type_b": b,
            "co_events": c,
            "shared_activities": len(pair_acts.get((a, b), set())),
        }
        for (a, b), c in pair_count.items()
    ]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("co_events", ascending=False).reset_index(drop=True)


# ------------------------------------------------------- 对象内活动转移(核心)

def object_transitions(ocel: Any, min_delay_zero: bool = True) -> pd.DataFrame:
    """
    沿每个对象的时间线,统计"活动 A → 活动 B"的转移。

    做法:按对象分组、按时间排序,取相邻事件对。
    对每个转移记录:出现次数、中位时延(小时)、平均时延。

    注意:同一对象上时间戳相同的多个事件排序是不确定的,
    这类转移会在结果里以较低置信度体现。
    """
    rel = ocel.relations[[EID, OID, OTYPE, ACT, TS]].copy()
    rel[TS] = pd.to_datetime(rel[TS], utc=True, errors="coerce")
    rel = rel.sort_values([OID, TS, EID], kind="mergesort")

    grp = rel.groupby(OID, sort=False)
    rel["next_act"] = grp[ACT].shift(-1)
    rel["next_ts"] = grp[TS].shift(-1)

    trans = rel.dropna(subset=["next_act"]).copy()
    trans["delay_hours"] = (trans["next_ts"] - trans[TS]).dt.total_seconds() / 3600
    if min_delay_zero:
        # 负时延说明同一对象上时间戳乱序,剔除以免污染统计
        trans = trans[trans["delay_hours"] >= 0]

    out = trans.groupby([ACT, "next_act", OTYPE]).agg(
        count=(EID, "size"),
        median_delay_h=( "delay_hours", "median"),
        mean_delay_h=("delay_hours", "mean"),
    ).reset_index()
    out = out.rename(columns={ACT: "source_activity", "next_act": "target_activity",
                              OTYPE: "object_type"})
    return out.sort_values("count", ascending=False).reset_index(drop=True)


# ------------------------------------------------ 关联规则与统计显著性检验

def activity_association_rules(
    ocel: Any,
    min_support: float = 0.0002,
    min_lift: float = 1.1,
    alpha: float = 0.05,
    max_rules: int = 300,
) -> pd.DataFrame:
    """
    触发关系的统计显著性检验。

    **为什么以「转移」而不是「对象共现」为事务单位**

    直觉上容易把「对象共现」当成关联分析的事务:统计活动 a、b 出现在
    同一对象上的比例。但这条口径对「触发」问题是错的 ——

    一个事件可以关联多个对象(本数据集平均 4.23 个),因此高频活动会
    出现在极大量对象上(例如 "Create Purchase Order" 覆盖 74,489 个对象
    中的 47,318 个,占比 64%)。边际概率被撑大后,lift 公式
    n_ab·N/(n_a·n_b) 会被严重稀释,导致真正的主干链路反而算出 lift < 1
    的"负相关"结果 —— 实测该数据集上 "Create Purchase Order" 与
    "Enter Incoming Invoice" 对象共现 14,577 次,lift 却只有 0.77。

    "流程 A 触发流程 B" 问的是**顺序**问题:A 发生后 B 是否更可能出现。
    因此正确的事务单位是「对象上的相邻事件对(转移)」,这也是序列规则
    挖掘(sequential rule mining)的标准口径。

    度量定义(以转移为事务):

        N_trans      = 转移总数(所有对象上的相邻事件对)
        n_a          = 源活动为 a 的转移数
        n_b          = 目标活动为 b 的转移数
        n_ab         = a → b 的转移数

        support(a→b) = n_ab / N_trans
        confidence   = P(b 紧随 a | a 出现) = n_ab / n_a
        baseline     = P(b 作为后继)        = n_b / N_trans
        lift         = confidence / baseline

    lift > 1 表示"a 之后接 b"比随机基线更常见,即 a 对 b 有预测力。
    与共现口径不同,**这里的 lift 是有方向的**:lift(a→b) ≠ lift(b→a)。

    显著性用卡方检验(2×2 列联表,df=1),并用 Benjamini-Hochberg
    做多重检验校正(FDR) —— 同时对上百条规则检验时不做校正会产生
    大量假阳性。
    """
    from itertools import combinations  # noqa: F401  (保留以兼容旧调用)

    import numpy as np
    from scipy.stats import chi2 as chi2_dist

    # 构造转移表:每个对象上按时间排序的相邻事件对
    rel = ocel.relations[[EID, OID, ACT, TS]].copy()
    rel[TS] = pd.to_datetime(rel[TS], utc=True, errors="coerce")
    rel = rel.sort_values([OID, TS, EID], kind="mergesort")

    g = rel.groupby(OID, sort=False)
    rel["next_act"] = g[ACT].shift(-1)
    rel["next_ts"] = g[TS].shift(-1)

    trans = rel.dropna(subset=["next_act"]).copy()
    dt = (trans["next_ts"] - trans[TS]).dt.total_seconds()
    trans = trans[dt >= 0]           # 剔除时间戳乱序造成的负时延
    if trans.empty:
        return pd.DataFrame()

    N_trans = len(trans)
    n_src = trans[ACT].value_counts()             # 源活动计数
    n_tgt = trans["next_act"].value_counts()      # 目标活动计数
    pair_counts = trans.groupby([ACT, "next_act"]).size()

    # 活动的主导对象类型,用于标注是否跨流程(排除资源类关联)
    from .ocel_loader import activity_object_profile
    prof = activity_object_profile(ocel, exclude_qualifiers=_RESOURCE_QUALIFIERS)
    dom = dict(zip(prof[ACT].astype(str), prof["dominant_object_type"]))

    rows = []
    for (a, b), n_ab in pair_counts.items():
        a, b = str(a), str(b)
        n_a = int(n_src.get(a, 0))
        n_b = int(n_tgt.get(b, 0))
        if n_a == 0 or n_b == 0:
            continue

        support = n_ab / N_trans
        if support < min_support:
            continue

        conf = n_ab / n_a                     # P(b 紧随 a | a 出现)
        baseline = n_b / N_trans              # P(b 作为后继)
        lift = conf / baseline if baseline > 0 else float("nan")
        if not (lift >= min_lift):
            continue

        # 2×2 列联表(转移层面):
        #              b 是后继    不是
        #   源是 a      n_ab      n_a-n_ab   | n_a
        #   源不是 a   n_b-n_ab   ...        | N-n_a
        n11 = int(n_ab)
        n10 = n_a - n11
        n01 = n_b - n11
        n00 = N_trans - n_a - n_b + n11
        denom = n_a * (N_trans - n_a) * n_b * (N_trans - n_b)
        if denom > 0 and n00 >= 0:
            chi2_stat = N_trans * (n11 * n00 - n10 * n01) ** 2 / denom
            p_value = float(chi2_dist.sf(chi2_stat, 1))
        else:
            chi2_stat, p_value = float("nan"), 1.0

        # lift 的对数尺度近似置信区间(Delta 方法)
        var_log = 1 / n11 - 1 / n_a - 1 / n_b + 1 / N_trans
        if var_log > 0:
            se = float(np.sqrt(var_log))
            ci_low = float(lift * np.exp(-1.96 * se))
            ci_high = float(lift * np.exp(1.96 * se))
        else:
            ci_low = ci_high = None

        da, db = dom.get(a), dom.get(b)
        rows.append({
            "source_activity": a,
            "target_activity": b,
            "source_domain": da,
            "target_domain": db,
            "is_cross_domain": bool(da and db and da != db),
            "n_source_trans": n_a,
            "n_target_trans": n_b,
            "n_ab": n11,
            "support": round(support, 6),
            "confidence": round(conf, 4),
            "baseline": round(baseline, 4),
            "lift": round(lift, 3),
            "lift_ci_low": round(ci_low, 3) if ci_low is not None else None,
            "lift_ci_high": round(ci_high, 3) if ci_high is not None else None,
            "chi2": round(float(chi2_stat), 4) if chi2_stat == chi2_stat else None,
            "p_value": p_value,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Benjamini-Hochberg FDR 校正
    df = df.sort_values("p_value", kind="mergesort").reset_index(drop=True)
    m = len(df)
    p = df["p_value"].to_numpy(dtype=float)
    scaled = p * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(scaled[::-1])[::-1]
    df["p_adjusted"] = np.clip(adjusted, 0.0, 1.0)
    df["significant"] = df["p_adjusted"] < alpha

    df = df.sort_values(
        ["significant", "is_cross_domain", "lift"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    logger.info(
        "触发关联检验: 转移总数 %d, 检验 %d 条规则, FDR 校正后显著 %d 条(跨流程 %d 条)",
        N_trans, m, int(df["significant"].sum()),
        int((df["significant"] & df["is_cross_domain"]).sum()),
    )
    return df.head(max_rules).reset_index(drop=True)


def association_summary(rules: pd.DataFrame) -> dict:
    """关联规则结果摘要,供报告展示。"""
    if rules.empty:
        return {"tested": 0, "significant": 0, "cross_domain": 0}
    sig = rules[rules["significant"]]
    return {
        "tested": int(len(rules)),
        "significant": int(len(sig)),
        "cross_domain": int((sig["is_cross_domain"]).sum()),
        "max_lift": float(rules["lift"].max()),
        "median_lift": float(rules["lift"].median()),
    }


def cross_process_triggers(ocel: Any, min_count: int = 5) -> pd.DataFrame:
    """
    筛出跨子流程的触发关系。

    步骤:
    1. 用「活动的主导对象类型」给每个活动打上子流程标签
    2. 取对象内活动转移
    3. 保留源活动与目标活动属于不同子流程的转移

    返回按出现次数排序的触发关系表,含中位时延。
    """
    from .ocel_loader import activity_object_profile

    # 主导对象类型用于给活动打子流程标签,资源类关联(员工)不参与
    profile = activity_object_profile(ocel, exclude_qualifiers=_RESOURCE_QUALIFIERS)
    act_domain = dict(zip(profile[ACT], profile["dominant_object_type"]))

    trans = object_transitions(ocel)
    if trans.empty:
        return trans

    trans["source_domain"] = trans["source_activity"].map(act_domain)
    trans["target_domain"] = trans["target_activity"].map(act_domain)

    cross = trans[
        trans["source_domain"].notna()
        & trans["target_domain"].notna()
        & (trans["source_domain"] != trans["target_domain"])
    ].copy()

    # 跨流程关系可能出现在多个对象类型上,按(源,目标)再聚合一次
    agg = cross.groupby(["source_activity", "target_activity",
                         "source_domain", "target_domain"]).agg(
        count=("count", "sum"),
        median_delay_h=("median_delay_h", "median"),
        object_types=("object_type", lambda s: ", ".join(sorted(set(s)))),
    ).reset_index()

    agg = agg[agg["count"] >= min_count]
    return agg.sort_values("count", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------- 桥接活动

def bridge_activities(ocel: Any, min_events: int = 2) -> pd.DataFrame:
    """
    识别"桥接活动":一次事件关联多种对象类型的活动。

    这些活动是两个流程发生交接的地方。桥接度越高,
    说明该活动把越多子流程串在一起。
    """
    rel = ocel.relations[[EID, ACT, OTYPE]].drop_duplicates()
    per_act = rel.groupby(ACT).agg(
        num_object_types=(OTYPE, "nunique"),
        num_events=(EID, "nunique"),
    ).reset_index()
    per_act = per_act[per_act["num_events"] >= min_events]
    per_act["bridge_score"] = (
        (per_act["num_object_types"] - 1) * per_act["num_events"]
    )
    return per_act.sort_values("bridge_score", ascending=False).reset_index(drop=True)


def object_lifecycles(ocel: Any) -> pd.DataFrame:
    """
    每种对象类型的生命周期特征:起始活动、结束活动、平均事件数。

    用来刻画每个子流程的边界——例如采购订单从"Create Purchase Order"
    到"Post Goods Receipt for PO"。
    """
    rel = ocel.relations[[OID, OTYPE, ACT, TS]].copy()
    rel[TS] = pd.to_datetime(rel[TS], utc=True, errors="coerce")
    rel = rel.sort_values([OID, TS], kind="mergesort")

    first = rel.groupby(OID).first().reset_index()
    last = rel.groupby(OID).last().reset_index()
    per_obj = rel.groupby(OID).size().reset_index(name="n_events")
    base = first[[OID, OTYPE, ACT]].rename(columns={ACT: "start_activity"})
    base = base.merge(per_obj, on=OID)
    base = base.merge(last[[OID, ACT]].rename(columns={ACT: "end_activity"}), on=OID)

    out = base.groupby(OTYPE).agg(
        num_objects=(OID, "nunique"),
        avg_events_per_object=("n_events", "mean"),
        top_start=("start_activity", lambda s: s.value_counts().index[0]),
        top_end=("end_activity", lambda s: s.value_counts().index[0]),
    ).reset_index().rename(columns={OTYPE: "object_type"})
    out["avg_events_per_object"] = out["avg_events_per_object"].round(2)
    return out.sort_values("num_objects", ascending=False).reset_index(drop=True)


def bidirectional_pairs(rules: pd.DataFrame) -> list[dict]:
    """
    找出互相触发的活动对。

    若 a→b 与 b→a 双双显著,说明两者互为因果地驱动对方 ——
    这通常不是"流程 A 触发流程 B"的干净链路,而是**返工环**:
    a 产生返工引出 b,b 又引出 a,反复循环。

    这类关系在纯频次视角下看不出来(两条规则各自看都正常),
    必须两个方向都做检验才能暴露。
    """
    if rules.empty:
        return []
    sig = rules[rules["significant"]]
    by_pair: dict[tuple[str, str], list] = {}
    for _, r in sig.iterrows():
        key = tuple(sorted([r["source_activity"], r["target_activity"]]))
        by_pair.setdefault(key, []).append(r)

    out = []
    for (a, b), items in by_pair.items():
        if len(items) != 2 or a == b:
            continue
        fwd = next(x for x in items if x["source_activity"] == a)
        rev = next(x for x in items if x["source_activity"] == b)
        out.append({
            "activity_a": a,
            "activity_b": b,
            "lift_a_to_b": round(float(fwd["lift"]), 2),
            "lift_b_to_a": round(float(rev["lift"]), 2),
            "n_a_to_b": int(fwd["n_ab"]),
            "n_b_to_a": int(rev["n_ab"]),
            "is_cross_domain": bool(fwd["is_cross_domain"]),
        })
    return sorted(out, key=lambda d: -(d["lift_a_to_b"] + d["lift_b_to_a"]))


def full_cross_process_report(ocel: Any, min_trigger_count: int = 5) -> dict:
    """聚合所有跨流程分析结果,供报告消费。"""
    logger.info("开始跨流程关系挖掘")
    interaction = object_interaction_graph(ocel)
    triggers = cross_process_triggers(ocel, min_count=min_trigger_count)
    bridges = bridge_activities(ocel)
    lifecycles = object_lifecycles(ocel)

    # 统计显著性验证
    rules = activity_association_rules(ocel)
    summary = association_summary(rules)

    # 按不同口径取前三,报告要同时呈现"有信息量"和"有体量"两类
    sig = rules[rules["significant"]] if not rules.empty else rules
    by_lift = sig.sort_values("lift", ascending=False).head(12) if not sig.empty else sig
    by_volume = sig.sort_values("n_ab", ascending=False).head(12) if not sig.empty else sig

    logger.info(
        "对象类型交互对 %d,跨流程触发关系 %d 条,桥接活动 %d 个,"
        "显著关联规则 %d 条",
        len(interaction), len(triggers), len(bridges), summary["significant"],
    )
    return {
        "object_interaction": interaction.to_dict(orient="records"),
        "cross_process_triggers": triggers.head(30).to_dict(orient="records"),
        "bridge_activities": bridges.head(20).to_dict(orient="records"),
        "object_lifecycles": lifecycles.to_dict(orient="records"),
        "association_summary": summary,
        "rules_by_lift": by_lift.to_dict(orient="records"),
        "rules_by_volume": by_volume.to_dict(orient="records"),
        "bidirectional_pairs": bidirectional_pairs(rules),
    }
