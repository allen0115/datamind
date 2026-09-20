"""端到端入口:BPMN 模型 → 模拟执行 → 转换成 XES / OCEL 数据集。

用法:
    python -m src.sim_main --cases 200 --seed 42
    python -m src.sim_main --cases 50 --case-notion instance --case-notion object:invoice
    python -m src.sim_main --no-source          # 不落源表 CSV,只出日志

产物(默认目录 data/sim):
    p2p_sim.bpmn                BPMN 2.0 模型(可直接用 Camunda Modeler 打开)
    p2p_sim.jsonocel            OCEL 2.0 数据集
    p2p_sim_instance.xes        XES(case = 流程实例)
    p2p_sim_flat_<otype>.xes    XES(case = 业务对象,即 OCEL flattening)
    source/*.csv                源系统表(引擎表 + 业务表),便于回溯核对
    conversion_quality.json     转换质量门禁指标
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .bpmn_converter import (
    build_ocel,
    convert,
    to_xes_df,
    write_ocel,
    write_xes,
)
from .bpmn_model import PROCESSES, export_bpmn_xml
from .bpmn_simulator import SimConfig, simulate

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("datamind.sim")


def run(cases: int = 200, seed: int = 42, outdir: str | Path = "data/sim",
        name: str = "p2p_sim", case_notions: tuple[str, ...] = ("instance", "object:purchase_order"),
        dump_source: bool = True, start_at: str = "2024-01-01",
        approval_threshold: float = 50000.0) -> dict[str, str]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}

    # 1. BPMN 模型
    bpmn_path = export_bpmn_xml(outdir / f"{name}.bpmn", PROCESSES)
    outputs["bpmn"] = str(bpmn_path)
    logger.info("BPMN 模型已导出: %s(%d 条流程)", bpmn_path, len(PROCESSES))

    # 2. 模拟执行:产出源系统表
    cfg = SimConfig(n_requisitions=cases, seed=seed, start_at=start_at,
                    approval_threshold=approval_threshold)
    tables = simulate(cfg)
    logger.info("模拟完成: %d 流程实例 / %d 任务实例 / %d 流程变量",
                len(tables.process_instance), len(tables.task), len(tables.variable))

    if dump_source:
        src_dir = outdir / "source"
        src_dir.mkdir(parents=True, exist_ok=True)
        for tname, df in tables.all_tables().items():
            df.to_csv(src_dir / f"{tname}.csv", index=False)
        outputs["source_dir"] = str(src_dir)
        logger.info("源表 CSV 已写入: %s", src_dir)

    # 3. 转换
    result = convert(tables)
    events = result.events
    logger.info("转换完成: %d 事件 / %d 对象 / %d 事件-对象关联",
                len(events), len(result.objects), len(result.relations))

    # 4. OCEL 2.0
    ocel = build_ocel(events, result.objects, result.relations, result.o2o)
    ocel_path = write_ocel(ocel, outdir / f"{name}.jsonocel")
    outputs["ocel"] = str(ocel_path)
    logger.info("OCEL 已写入: %s", ocel_path)

    # 5. XES(每种 case notion 一个文件)
    for notion in case_notions:
        try:
            xes_df = to_xes_df(events, result.relations, notion)
        except ValueError as e:
            logger.warning("跳过 case notion %s: %s", notion, e)
            continue
        suffix = ("flat_" + notion.split(":", 1)[1]) if notion.startswith("object:") else notion
        path = outdir / f"{name}_{suffix}.xes"
        write_xes(xes_df, path)
        outputs[f"xes:{notion}"] = str(path)
        logger.info("XES 已写入: %s(%d 条事件, %d 条 trace)",
                    path, len(xes_df), xes_df["case:concept:name"].nunique())

    # 6. 质量门禁
    qpath = outdir / "conversion_quality.json"
    qpath.write_text(json.dumps(result.quality, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    outputs["quality"] = str(qpath)

    print("\n=== 转换质量门禁 ===")
    print(result.quality_frame().to_string(index=False))
    print("\n=== 各对象类型 ===")
    print(result.object_type_frame().to_string(index=False))

    q = result.quality
    if q["isolated_pct"] > 0:
        logger.warning("存在 %.2f%% 孤立事件,对象关联规则未覆盖全部活动", q["isolated_pct"])
    if q["avg_objects_per_event"] < 1.5:
        logger.warning("平均对象/事件 %.2f 偏低,多对象关联没建立起来",
                       q["avg_objects_per_event"])
    return outputs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="BPMN 模拟数据集 → XES / OCEL 转换")
    p.add_argument("--cases", type=int, default=200, help="采购申请条数(整条 P2P 链条的起点)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--outdir", default="data/sim")
    p.add_argument("--name", default="p2p_sim", help="产物文件名前缀")
    p.add_argument("--case-notion", action="append", default=None,
                   help="XES 的 case 口径,可重复。instance | object:<otype>")
    p.add_argument("--start-at", default="2024-01-01")
    p.add_argument("--approval-threshold", type=float, default=50000.0)
    p.add_argument("--no-source", action="store_true", help="不导出源表 CSV")
    a = p.parse_args(argv)

    notions = tuple(a.case_notion) if a.case_notion else ("instance", "object:purchase_order")
    try:
        run(cases=a.cases, seed=a.seed, outdir=a.outdir, name=a.name,
            case_notions=notions, dump_source=not a.no_source,
            start_at=a.start_at, approval_threshold=a.approval_threshold)
    except Exception as e:
        logger.exception("转换失败: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
