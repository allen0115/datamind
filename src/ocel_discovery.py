"""对象中心流程发现(OCPM)。

两种视图:

1. **按对象类型展开的 DFG**(主视图)
   把 OCEL 按对象类型"拍扁",每种对象类型得到一条独立的直接跟随图。
   比如采购订单(EBELN)的生命周期 vs 发票(BELNR)的生命周期,
   各自清晰可读——这是 OCPM 相比传统流程挖掘最实用的产出。

2. **OCPN(对象中心 Petri 网)摘要**
   形式化模型,用于理解活动与对象类型之间的绑定关系。
   137 个变迁直接渲染会很乱,因此报告里只给结构统计。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .ocel_loader import ACT, EID, OID, OTYPE

logger = logging.getLogger(__name__)


def ocdfg_object_type_stats(ocdfg: dict, ocel: Any) -> pd.DataFrame:
    """每种对象类型的流程复杂度:活动数、边数、起始/结束活动。"""
    rel = ocel.relations
    rows = []
    for otype in sorted(ocdfg["activities_ot"]["events"].keys()):
        acts = ocdfg["activities_ot"]["events"][otype]
        edges = ocdfg["edges"]["event_couples"].get(otype, {})
        starts = ocdfg["start_activities"]["events"].get(otype, {})
        ends = ocdfg["end_activities"]["events"].get(otype, {})

        n_events = int(rel[rel[OTYPE] == otype][EID].nunique())
        top_start = max(starts.items(), key=lambda kv: len(kv[1]))[0] if starts else "—"
        top_end = max(ends.items(), key=lambda kv: len(kv[1]))[0] if ends else "—"

        rows.append({
            "object_type": otype,
            "num_activities": len(acts),
            "num_edges": len(edges),
            "num_events": n_events,
            "top_start_activity": top_start,
            "top_end_activity": top_end,
        })
    return pd.DataFrame(rows).sort_values("num_events", ascending=False).reset_index(drop=True)


def render_object_type_dfg(ocdfg: dict, ocel: Any, otype: str,
                           min_edge_count: int = 1) -> str:
    """
    渲染单一对象类型的直接跟随图(SVG)。

    节点 = 该对象类型上出现过的活动,标注事件数;
    边 = 直接跟随关系,标注出现次数。
    """
    import graphviz

    edges_raw = ocdfg["edges"]["event_couples"].get(otype, {})
    if not edges_raw:
        return (f'<div class="warning-box">对象类型 {otype} 没有可为每个对象'
                f'构成序列的事件(多数对象只参与单个事件)。</div>')

    # 事件频次(按 对象类型 × 活动)
    rel = ocel.relations
    freq = (rel[rel[OTYPE] == otype].groupby(ACT)[EID].nunique()
            .sort_values(ascending=False))
    if freq.empty:
        return f'<div class="warning-box">对象类型 {otype} 无活动数据。</div>'

    # 边计数
    edge_counts: dict[tuple[str, str], int] = {}
    for (a, b), evset in edges_raw.items():
        c = len(evset)
        if c >= min_edge_count:
            edge_counts[(a, b)] = edge_counts.get((a, b), 0) + c
    if not edge_counts:
        return f'<div class="warning-box">对象类型 {otype} 无满足阈值的流转关系。</div>'

    max_freq = int(freq.max())
    max_edge = max(edge_counts.values())

    # 节点 ID 必须避开 DOT 保留字符(':' 是端口分隔符),用顺序 ID
    acts = list(freq.index)
    nid = {a: f"n{i}" for i, a in enumerate(acts)}

    dot = graphviz.Digraph(
        f"dfg_{otype}",
        format="svg",
        graph_attr={
            "rankdir": "LR", "splines": "spline",
            "nodesep": "0.4", "ranksep": "0.9",
            "fontname": "Helvetica", "bgcolor": "transparent", "pad": "0.2",
        },
        node_attr={"fontname": "Helvetica", "fontsize": "11"},
        edge_attr={"fontname": "Helvetica", "fontsize": "9", "color": "#64748b"},
    )

    def shade(f: int) -> tuple[str, str]:
        r = f / max_freq
        if r > 0.66: return "#1e40af", "#ffffff"
        if r > 0.33: return "#3b82f6", "#ffffff"
        if r > 0.10: return "#93c5fd", "#0c4a6e"
        return "#dbeafe", "#0c4a6e"

    for act, f in freq.items():
        fill, font = shade(int(f))
        line1, line2 = act, ""
        if len(act) > 26:
            for sep in (" for ", " by ", " (", " "):
                if sep in act and len(act) > 26:
                    head, tail = act.split(sep, 1)
                    if len(head) > 10 and len(tail) > 3:
                        line1, line2 = head, f"{sep.strip()} {tail}"
                        break
        label = f"{line1}\n{line2}\n({int(f):,})" if line2 else f"{line1}\n({int(f):,})"
        dot.node(nid[act], label, shape="box", style="rounded,filled",
                 fillcolor=fill, color="#1e40af", fontcolor=font,
                 penwidth="1.2", margin="0.1,0.05")

    for (a, b), c in edge_counts.items():
        if a not in nid or b not in nid:
            continue
        pen = max(0.6, min(3.0, 0.6 + 2.4 * (c / max_edge)))
        is_back = (a == b)
        dot.edge(nid[a], nid[b], label=f"{c:,}", penwidth=f"{pen:.1f}",
                 color="#94a3b8" if is_back else "#64748b",
                 style="dashed" if is_back else "solid")

    svg = dot.pipe(format="svg")
    return svg.decode("utf-8") if isinstance(svg, bytes) else str(svg)


def ocpn_summary(ocpn: Any) -> dict:
    """OCPN 结构摘要(不渲染整图——137 个变迁渲染出来无法阅读)。"""
    labelled = [t for t in ocpn.transitions if t.label]
    silent = len(ocpn.transitions) - len(labelled)
    return {
        "num_places": len(ocpn.places),
        "num_transitions": len(ocpn.transitions),
        "num_silent_transitions": silent,
        "num_labelled_transitions": len(labelled),
        "num_arcs": len(ocpn.arcs),
    }
