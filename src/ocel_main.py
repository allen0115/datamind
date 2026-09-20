"""OCEL 分析主入口:数据加载 → 跨流程挖掘 → 对象中心发现 → 报告。"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from .ocel_loader import load_ocel, summarize_ocel, object_type_stats
from .ocel_cross_process import (
    full_cross_process_report,
    object_interaction_graph,
    cross_process_triggers,
)
from .ocel_discovery import (
    ocdfg_object_type_stats,
    render_object_type_dfg,
    ocpn_summary,
)
from .ocel_report import build_ocel_report, write_ocel_report

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("datamind.ocel")

# 时间戳质量提示:根据数据集实际情况填写
_TS_WARNING_DEFAULT = (
    "本数据集的绝对时间戳由模拟器生成(跨度 1994–2020,明显不合业务常理),"
    "且 24,854 个事件中仅 10,175 个唯一时间戳(约 59% 重复)。"
    "因此表 4 中的「触发次数」可靠,但<b>时延数据不可用于 SLA 结论</b>。"
    "接入真实系统日志后此项限制自动消失。"
)


def run_ocel_pipeline(dataset_name: str, config_path: str | Path = "config.yaml") -> Path:
    cfg = yaml.safe_load(open(config_path, encoding="utf-8"))
    root = Path(config_path).parent

    ds = cfg["ocel_datasets"][dataset_name]
    ocel_path = root / ds["path"]
    top_dfgs = ds.get("top_dfgs", 5)
    min_trigger = ds.get("min_trigger_count", 5)

    # 1. 加载
    ocel = load_ocel(ocel_path, max_objects=ds.get("max_objects"))
    summary = summarize_ocel(ocel)
    logger.info("概览: %s", {k: summary[k] for k in
                              ("num_events", "num_objects", "num_object_types")})

    # 2. 对象类型统计
    ot_stats_df = object_type_stats(ocel)

    # 3. 跨流程关系挖掘
    cross = full_cross_process_report(ocel, min_trigger_count=min_trigger)

    # 4. 对象中心流程发现
    import pm4py
    logger.info("发现 OCDFG / OCPN")
    ocdfg = pm4py.discover_ocdfg(ocel)
    ocpn_obj = pm4py.discover_oc_petri_net(ocel)
    ot_disc = ocdfg_object_type_stats(ocdfg, ocel)

    # 为有流转边的主要对象类型渲染 DFG。
    # 有些对象类型描述的是同一条流程的不同层级(如采购订单头 EBELN
    # 与其行项目 EBELN_EBELP),流程形状高度重合。这里用近重复检测
    # (活动集相同 且 边集 Jaccard 相似度 >= 0.9)避免报告里出现近乎一样的图。
    def _shape(otype: str):
        acts = frozenset(ocdfg["activities_ot"]["events"].get(otype, []))
        edges = frozenset(ocdfg["edges"]["event_couples"].get(otype, {}).keys())
        return acts, edges

    candidates = [r["object_type"] for r in ot_disc.to_dict(orient="records")
                  if r["num_edges"] > 0]
    dfg_svgs: dict[str, str] = {}
    kept: list[tuple[str, frozenset, frozenset]] = []

    for otype in candidates:
        acts, edges = _shape(otype)
        dup_of = None
        for kname, kacts, kedges in kept:
            if acts != kacts:
                continue
            union = edges | kedges
            if union and len(edges & kedges) / len(union) >= 0.9:
                dup_of = kname
                break
        if dup_of:
            logger.info("对象类型 %s 与 %s 流程高度重合,跳过渲染", otype, dup_of)
            continue

        svg = render_object_type_dfg(ocdfg, ocel, otype)
        if "<svg" not in svg:
            continue
        kept.append((otype, acts, edges))
        dfg_svgs[otype] = svg
        logger.info("已渲染对象类型 DFG: %s", otype)
        if len(dfg_svgs) >= top_dfgs:
            break

    # 5. 组装报告
    html = build_ocel_report(
        title=cfg["report"].get("ocel_title", "OCEL 对象中心流程分析报告"),
        dataset_description=ds.get("description", dataset_name),
        summary=summary,
        object_types=ot_stats_df.to_dict(orient="records"),
        cross=cross,
        discovery={"object_type_stats": ot_disc.to_dict(orient="records")},
        dfg_svgs=dfg_svgs,
        ocpn=ocpn_summary(ocpn_obj),
        timestamp_warning=ds.get("timestamp_warning", _TS_WARNING_DEFAULT),
    )
    out = write_ocel_report(html, root / cfg["report"]["output_dir"],
                            filename_prefix=f"ocel_{dataset_name}")
    logger.info("报告已生成: %s", out)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="datamind OCEL 分析")
    p.add_argument("--dataset", required=True, help="config.yaml 中 ocel_datasets 的键名")
    p.add_argument("--config", default="config.yaml")
    a = p.parse_args(argv)
    try:
        path = run_ocel_pipeline(a.dataset, a.config)
    except Exception as e:
        logger.exception("OCEL 流水线失败: %s", e)
        return 1
    print(f"\n报告: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
