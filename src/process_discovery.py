"""流程发现:Inductive Miner / Alpha Miner,导出 BPMN 用于报告渲染。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def discover_petri_net(
    df: pd.DataFrame,
    algorithm: str = "inductive",
    noise_threshold: float = 0.2,
) -> tuple[Any, Any, Any]:
    """
    从事件日志发现流程模型(Petri Net)。

    Parameters
    ----------
    df : pd.DataFrame
        标准化的 XES 风格 DataFrame,需含 case:concept:name, concept:name, time:timestamp 列
    algorithm : str
        'inductive' | 'alpha' | 'heuristic'
    noise_threshold : float
        Inductive Miner 的噪声过滤阈值(0-1),越大过滤越多

    Returns
    -------
    net, im, fm : petri net, initial marking, final marking
    """
    import pm4py

    log = pm4py.convert_to_event_log(df)

    algo = algorithm.lower()
    if algo == "inductive":
        net, im, fm = pm4py.discover_petri_net_inductive(
            log, noise_threshold=noise_threshold
        )
    elif algo == "alpha":
        net, im, fm = pm4py.discover_petri_net_alpha(log)
    elif algo == "heuristic":
        net, im, fm = pm4py.discover_petri_net_heuristic(
            log, noise_threshold=noise_threshold
        )
    else:
        raise ValueError(f"未知流程发现算法: {algorithm}")

    n_transitions = len(net.transitions)
    n_places = len(net.places)
    logger.info(
        "流程发现完成 (%s): %d transitions, %d places",
        algorithm, n_transitions, n_places,
    )
    return net, im, fm


def export_bpmn_xml(net: Any, im: Any, fm: Any) -> str:
    """
    将 Petri Net 转换为 BPMN 2.0 XML 字符串(bpmn-js Viewer 直接渲染)。

    pm4py 2.7 中 pm4py.convert_to_bpmn 返回 BPMN 图对象,
    需要通过 etree exporter 的 get_xml_string 拿到 XML 字符串。
    """
    import pm4py
    from pm4py.objects.bpmn.exporter.variants.etree import get_xml_string

    bpmn_graph = pm4py.convert_to_bpmn(net, im, fm)
    xml_bytes = get_xml_string(bpmn_graph, parameters={})
    if isinstance(xml_bytes, bytes):
        xml_str = xml_bytes.decode("utf-8")
    else:
        xml_str = str(xml_bytes)
    return xml_str


def export_petri_net_svg(net: Any, im: Any, fm: Any) -> str:
    """
    将 Petri Net 渲染为带布局的 SVG 字符串(供 HTML 报告嵌入)。

    pm4py 自带的 BPMN 导出器不计算布局坐标,所有节点都在 (0,0),
    会导致 bpmn-js 渲染空白。这里改用 graphviz 直接渲染 Petri Net:
    - transitions(可见活动): 圆角矩形 + 名称
    - 静默变迁(τ): 小号深色实心方块,不显示内部 ID
    - places(状态): 圆圈,起始/终止加粗着色
    - arcs(流转): 有向边

    返回的 SVG 可直接嵌入 HTML,无需前端 JS。
    """
    import graphviz

    dot = graphviz.Digraph(
        "process",
        format="svg",
        graph_attr={
            "rankdir": "LR",
            "splines": "spline",
            "nodesep": "0.35",
            "ranksep": "0.7",
            "fontname": "Helvetica",
            "bgcolor": "transparent",
            "pad": "0.2",
        },
        node_attr={
            "fontname": "Helvetica",
            "fontsize": "10",
        },
        edge_attr={
            "arrowsize": "0.6",
            "color": "#64748b",
        },
    )

    # 节点 ID 用顺序编号,避免 id() 泄漏到 <title> 里,也规避 DOT 保留字符
    place_ids = {id(p): f"p{i}" for i, p in enumerate(net.places)}
    trans_ids = {id(t): f"t{i}" for i, t in enumerate(net.transitions)}

    # places(状态)
    for place in net.places:
        tokens = im[place] if place in im else 0
        is_start = tokens > 0
        is_end = fm.get(place, 0) > 0
        dot.node(
            place_ids[id(place)],
            "",
            shape="circle",
            width="0.18",
            height="0.18",
            fixedsize="true",
            style="filled",
            fillcolor="#1e3a8a" if is_start else ("#10b981" if is_end else "#ffffff"),
            color="#1e3a8a" if is_start else ("#059669" if is_end else "#94a3b8"),
            penwidth="1.6",
        )

    # transitions(活动 + 静默变迁)
    for trans in net.transitions:
        if trans.label:
            display = trans.label
            if len(display) > 30:
                for sep in (" by ", " (", " - "):
                    if sep in display:
                        head, tail = display.split(sep, 1)
                        display = f"{head}\\n{sep.strip()} {tail}"
                        break
            dot.node(
                trans_ids[id(trans)],
                display,
                shape="box",
                style="rounded,filled",
                fillcolor="#dbeafe",
                color="#3b82f6",
                penwidth="1.2",
                margin="0.08,0.04",
            )
        else:
            # 静默变迁:小实心方块,不显示 ID
            dot.node(
                trans_ids[id(trans)],
                "",
                shape="box",
                width="0.1",
                height="0.1",
                fixedsize="true",
                style="filled",
                fillcolor="#334155",
                color="#334155",
            )

    # arcs(流转)
    for arc in net.arcs:
        src = arc.source
        tgt = arc.target
        src_id = place_ids[id(src)] if src in net.places else trans_ids[id(src)]
        tgt_id = place_ids[id(tgt)] if tgt in net.places else trans_ids[id(tgt)]
        dot.edge(src_id, tgt_id)

    svg_bytes = dot.pipe(format="svg")
    svg_str = svg_bytes.decode("utf-8") if isinstance(svg_bytes, bytes) else str(svg_bytes)
    # 保留 graphviz 的自然 width/height,由报告容器滚动展示,避免缩放后文字过小
    return svg_str


def export_dfg_svg(df: pd.DataFrame, min_edge_count: int = 1) -> str:
    """
    渲染 Directly-Follows Graph (DFG) 为 SVG。

    DFG 是过程挖掘里最易读的视图,适合业务方阅读:
    - 节点 = 活动,节点颜色深浅 = 执行频次
    - 边 = "紧接着发生"的关系,边上数字 = 出现次数
    - 边越粗 = 该流转路径越常见

    相比 Petri 网(35 个变迁 + 21 个库所),DFG 只有 17 个活动节点,
    图宽度可控,文字可读。

    返回自然尺寸的 SVG,由容器滚动展示,不做缩放,保证文字清晰。
    """
    import graphviz
    from collections import Counter
    import pm4py

    log = pm4py.convert_to_event_log(df)
    dfg, start_acts, end_acts = pm4py.discover_dfg(log)

    act_freq = Counter(df["concept:name"])
    if not act_freq:
        return '<div class="warning-box">无活动数据,无法生成 DFG。</div>'

    max_freq = max(act_freq.values())
    max_edge = max(dfg.values()) if dfg else 1

    # 节点 ID 必须避开 DOT 的保留语法:':' 是端口分隔符,空格/括号也会破坏解析。
    # 这里统一用顺序 ID(n0, n1, ...),活动名只出现在 label 里。
    node_id: dict[str, str] = {act: f"n{i}" for i, act in enumerate(act_freq)}

    # 计算每个活动在 trace 中的平均位置,用于区分"前进边"和"回退边"。
    # 审批流程存在大量回退(如 REJECTED 回到 SUBMITTED),若让这些环参与
    # 图层排序,graphviz 会把整图压成一列。给回退边加 constraint=false,
    # 让排名仍按主流方向从左到右推进。
    pos_sum: dict[str, float] = {}
    pos_cnt: dict[str, int] = {}
    for _, grp in df.groupby("case:concept:name")["concept:name"]:
        for i, act in enumerate(grp):
            pos_sum[act] = pos_sum.get(act, 0.0) + i
            pos_cnt[act] = pos_cnt.get(act, 0) + 1
    avg_pos = {a: pos_sum[a] / pos_cnt[a] for a in pos_sum}

    dot = graphviz.Digraph(
        "dfg",
        format="svg",
        graph_attr={
            "rankdir": "LR",
            "splines": "spline",
            "nodesep": "0.45",
            "ranksep": "1.0",
            "fontname": "Helvetica",
            "bgcolor": "transparent",
            "pad": "0.25",
        },
        node_attr={"fontname": "Helvetica", "fontsize": "12"},
        edge_attr={
            "fontname": "Helvetica",
            "fontsize": "10",
            "color": "#64748b",
            "arrowsize": "0.7",
        },
    )

    def _shade(freq: int) -> tuple[str, str]:
        ratio = freq / max_freq
        if ratio > 0.66:
            return "#1e40af", "#ffffff"
        if ratio > 0.33:
            return "#3b82f6", "#ffffff"
        if ratio > 0.10:
            return "#93c5fd", "#0c4a6e"
        return "#dbeafe", "#0c4a6e"

    for act, freq in act_freq.items():
        fill, font = _shade(freq)
        line1, line2 = act, ""
        if len(act) > 26:
            for sep in (" by ", " ("):
                if sep in act:
                    head, tail = act.split(sep, 1)
                    line1, line2 = head, f"{sep.strip()} {tail}"
                    break
        label = f"{line1}\n{line2}\n({freq:,})" if line2 else f"{line1}\n({freq:,})"
        dot.node(
            node_id[act],
            label,
            shape="box",
            style="rounded,filled",
            fillcolor=fill,
            color="#1e40af",
            fontcolor=font,
            penwidth="1.2",
            margin="0.12,0.06",
        )

    # 开始 / 结束伪节点
    dot.node("__start__", "开始", shape="circle", style="filled",
             fillcolor="#1e3a8a", fontcolor="#ffffff", fontsize="11",
             width="0.75", fixedsize="true")
    dot.node("__end__", "结束", shape="doublecircle", style="filled",
             fillcolor="#059669", fontcolor="#ffffff", fontsize="11",
             width="0.75", fixedsize="true")

    for (src, tgt), cnt in dfg.items():
        if cnt < min_edge_count:
            continue
        pen = max(0.6, min(3.2, 0.6 + 2.6 * (cnt / max_edge)))
        # 回退边不参与排名约束,避免把图压扁
        is_back = avg_pos.get(tgt, 0) < avg_pos.get(src, 0)
        dot.edge(
            node_id[src], node_id[tgt],
            label=f"{cnt:,}",
            penwidth=f"{pen:.1f}",
            constraint="false" if is_back else "true",
            color="#94a3b8" if is_back else "#64748b",
            style="dashed" if is_back else "solid",
        )

    # 起始边:从"开始"指向入口活动
    for act, cnt in (start_acts or {}).items():
        dot.edge("__start__", node_id[act], label=f"{cnt:,}",
                 color="#1e40af", penwidth="1.3")

    # 结束边:只有平均位置靠后的活动才参与排名约束
    late_threshold = sorted(avg_pos.values())[len(avg_pos) // 2] if avg_pos else 0
    for act, cnt in (end_acts or {}).items():
        is_late = avg_pos.get(act, 0) >= late_threshold
        dot.edge(node_id[act], "__end__", label=f"{cnt:,}",
                 color="#059669", penwidth="1.3",
                 constraint="true" if is_late else "false")

    svg_bytes = dot.pipe(format="svg")
    svg_str = svg_bytes.decode("utf-8") if isinstance(svg_bytes, bytes) else str(svg_bytes)
    return svg_str


def get_process_stats(net: Any) -> dict:
    """返回流程模型的结构统计。"""
    return {
        "num_transitions": len(net.transitions),
        "num_places": len(net.places),
        "num_arcs": len(net.arcs),
        "activities": sorted(t.label for t in net.transitions if t.label),
    }


def get_start_end_activities(df: pd.DataFrame) -> dict:
    """统计起始/结束活动,用于报告的概览卡。"""
    import pm4py

    log = pm4py.convert_to_event_log(df)
    start = pm4py.get_start_activities(log)
    end = pm4py.get_end_activities(log)
    return {"start_activities": start, "end_activities": end}


def get_variants(df: pd.DataFrame, top_k: int = 10) -> list[dict]:
    """返回 Top K 流程变体(按频次排序),用于报告展示流程多样性。"""
    import pm4py

    log = pm4py.convert_to_event_log(df)
    variants = pm4py.get_variants(log)
    sorted_variants = sorted(variants.items(), key=lambda kv: -len(kv[1]))[:top_k]
    return [
        {"variant": v[0], "count": len(v[1])}
        for v in sorted_variants
    ]