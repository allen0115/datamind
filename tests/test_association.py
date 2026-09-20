"""
触发关联规则(统计显著性)的正确性验证。

统计指标不能"看起来合理"就用 —— 这里用三种方式交叉验证:
1. 手工构造的已知答案
2. 与 scipy 自身的列联表函数对比
3. 零假设下的随机模拟(独立生成数据,验证假阳性率是否接近 alpha)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _build_ocel_from_sequences(sequences: list[list[str]]):
    """
    按「对象 → 活动序列」构造最小 OCEL。

    每个对象产生 len(seq)-1 个转移(相邻事件对),
    这正是 activity_association_rules 统计的事务单位。
    """
    from pm4py.objects.ocel.obj import OCEL

    objects, ev_rows, rel_rows = [], [], []
    for i, seq in enumerate(sequences):
        oid = f"o{i}"
        objects.append({"ocel:oid": oid, "ocel:type": "T"})
        for j, act in enumerate(seq):
            eid = f"e{i}_{j}"
            ts = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(minutes=j)
            ev_rows.append({"ocel:eid": eid, "ocel:activity": act,
                            "ocel:timestamp": ts})
            rel_rows.append({"ocel:eid": eid, "ocel:oid": oid,
                             "ocel:activity": act, "ocel:timestamp": ts,
                             "ocel:type": "T"})
    return OCEL(events=pd.DataFrame(ev_rows),
                objects=pd.DataFrame(objects),
                relations=pd.DataFrame(rel_rows))


# ---------------------------------------------------------------- 手工校验

def test_lift_confidence_against_hand_calculation():
    """
    已知答案验证。

    构造 1000 个转移:
      120 个对象序列 [A,B]   → 120 个 A→B 转移
       80 个对象序列 [A,X]   →  80 个 A→X 转移   ⇒ n_a(A 为源) = 200
      180 个对象序列 [Y,B]   → 180 个 Y→B 转移   ⇒ n_b(B 为目标) = 120+180 = 300
      620 个对象序列 [Z,W]   → 620 个 Z→W 转移   ⇒ 转移总数 = 1000

    期望:
      support    = 120/1000 = 0.12
      confidence = 120/200  = 0.6      (P(B 紧随 A | A 出现))
      baseline   = 300/1000 = 0.3      (P(B 作为后继))
      lift       = 0.6/0.3  = 2.0
    """
    from src.ocel_cross_process import activity_association_rules

    seqs = []
    seqs += [["A", "B"]] * 120
    seqs += [["A", "X"]] * 80
    seqs += [["Y", "B"]] * 180
    seqs += [["Z", "W"]] * 620
    ocel = _build_ocel_from_sequences(seqs)

    rules = activity_association_rules(ocel, min_support=0.0, min_lift=0.0)
    row = rules[(rules["source_activity"] == "A")
                & (rules["target_activity"] == "B")]
    assert len(row) == 1, "应恰好命中 A→B 这条规则"
    r = row.iloc[0]

    assert r["n_source_trans"] == 200 and r["n_target_trans"] == 300
    assert r["n_ab"] == 120
    assert r["support"] == pytest.approx(0.12, abs=1e-9)
    assert r["confidence"] == pytest.approx(0.6, abs=1e-9)
    assert r["baseline"] == pytest.approx(0.3, abs=1e-9)
    assert r["lift"] == pytest.approx(2.0, abs=1e-6)


def test_lift_is_directional():
    """
    与「对象共现」口径不同,转移口径下 lift 是有方向的:
    lift(A→B) 与 lift(B→A) 一般不相等。
    """
    from src.ocel_cross_process import activity_association_rules

    seqs = []
    seqs += [["A", "B"]] * 120
    seqs += [["A", "X"]] * 80
    seqs += [["Y", "B"]] * 180
    seqs += [["Z", "W"]] * 620
    ocel = _build_ocel_from_sequences(seqs)
    rules = activity_association_rules(ocel, min_support=0.0, min_lift=0.0)

    ab = rules[(rules["source_activity"] == "A")
               & (rules["target_activity"] == "B")].iloc[0]
    ba = rules[(rules["source_activity"] == "B")
               & (rules["target_activity"] == "A")]
    # A→B 存在而 B→A 不存在(B 从不作为源,只在末尾)
    assert ab["lift"] == pytest.approx(2.0, abs=1e-6)
    assert len(ba) == 0, "B 从未作为源活动,不应有 B→A 规则"


def test_chi2_matches_scipy_contingency():
    """卡方统计量与 scipy.stats.chi2_contingency 对比。"""
    from scipy.stats import chi2_contingency
    from src.ocel_cross_process import activity_association_rules

    seqs = []
    seqs += [["A", "B"]] * 120
    seqs += [["A", "X"]] * 80
    seqs += [["Y", "B"]] * 180
    seqs += [["Z", "W"]] * 620
    ocel = _build_ocel_from_sequences(seqs)

    rules = activity_association_rules(ocel, min_support=0.0, min_lift=0.0)
    r = rules[(rules["source_activity"] == "A")
              & (rules["target_activity"] == "B")].iloc[0]

    # 转移层面 2×2:[[n11,n10],[n01,n00]]
    #   n11=120, n10=80, n01=180, n00=1000-200-300+120=620
    ref_chi2, ref_p, dof, _ = chi2_contingency(
        [[120, 80], [180, 620]], correction=False)
    assert dof == 1
    assert r["chi2"] == pytest.approx(ref_chi2, rel=1e-6)
    assert r["p_value"] == pytest.approx(ref_p, rel=1e-9)


def test_independent_sequences_give_lift_near_one():
    """后继活动在源活动上独立分布时,lift 应接近 1。"""
    from src.ocel_cross_process import activity_association_rules

    rng = np.random.default_rng(11)
    seqs = []
    for _ in range(20000):
        # 每条序列固定两跳,后继在 A/B/C 中均匀随机 ⇒ 与源无关
        seqs.append(["S", rng.choice(["A", "B", "C"])])
    ocel = _build_ocel_from_sequences(seqs)

    rules = activity_association_rules(ocel, min_support=0.0, min_lift=0.0)
    assert len(rules) == 3
    for _, r in rules.iterrows():
        assert r["lift"] == pytest.approx(1.0, abs=0.06), \
            f"{r['source_activity']}→{r['target_activity']} lift 应≈1,实际 {r['lift']}"
    # 独立时不应判为显著
    assert not rules["significant"].any()


def test_false_positive_rate_under_null():
    """
    零假设下的假阳性率检验:后继活动随机,不应产生显著规则。
    BH 校正后判显著的比例应接近 alpha(给足容差吸收模拟噪声)。
    """
    from src.ocel_cross_process import activity_association_rules

    rng = np.random.default_rng(2024)
    alpha = 0.05
    n_cases, trials = 1500, 25

    false_pos, total = 0, 0
    for _ in range(trials):
        seqs = [["S", str(rng.integers(0, 10))] for _ in range(n_cases)]
        ocel = _build_ocel_from_sequences(seqs)
        rules = activity_association_rules(
            ocel, min_support=0.0, min_lift=0.0, alpha=alpha)
        if rules.empty:
            continue
        false_pos += int(rules["significant"].sum())
        total += len(rules)

    fpr = false_pos / total if total else 0.0
    assert fpr < alpha * 2.5, f"零假设下假阳性率过高: {fpr:.4f}"


def test_bh_adjustment_properties():
    """BH 校正的基本性质:调整后 p ≥ 原始 p,单调不减,且落在 [0,1]。"""
    from src.ocel_cross_process import activity_association_rules

    rng = np.random.default_rng(5)
    seqs = []
    for _ in range(6000):
        seqs.append(["S", str(rng.integers(0, 6))])
    # 人为制造强关联:S→A
    seqs += [["S", "A"]] * 3000
    ocel = _build_ocel_from_sequences(seqs)

    rules = activity_association_rules(ocel, min_support=0.0, min_lift=0.0)
    assert (rules["p_adjusted"] >= rules["p_value"] - 1e-12).all(), \
        "校正后 p 值不应小于原始 p 值"
    assert rules["p_adjusted"].between(0, 1).all(), "p 值应在 [0,1] 内"

    s = rules.sort_values("p_value")["p_adjusted"].to_numpy()
    assert np.all(np.diff(s) >= -1e-12), "调整后 p 值应随原始 p 单调不减"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
