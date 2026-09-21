"""datamind PoC 主入口:串联数据加载 → 流程发现 → 合规 → 性能 → 报告。"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .data_loader import load_xes, summarize, filter_complete_lifecycle
from .process_discovery import (
    discover_petri_net,
    export_dfg_svg,
    export_petri_net_svg,
    get_process_stats,
    get_start_end_activities,
    get_variants,
)
from .conformance import conformance_check
from .performance import (
    case_durations,
    full_performance_report,
)
from .report_generator import build_report, write_report
from .llm_client import build_llm_client, llm_settings
from .llm_context import build_single_facts
from .insight import maybe_generate_insight

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("datamind.main")


def load_config(config_path: str | Path) -> dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_pipeline(dataset_name: str, config_path: str | Path = "config.yaml") -> Path:
    """
    执行完整流水线,返回生成的报告路径。
    """
    cfg = load_config(config_path)
    project_root = Path(config_path).parent

    ds_cfg = cfg["datasets"][dataset_name]
    xes_path = project_root / ds_cfg["xes_file"]
    filter_cfg = ds_cfg.get("case_filter", {}) or {}

    # 1. 数据加载
    df = load_xes(
        xes_path,
        max_cases=filter_cfg.get("max_cases"),
        case_id_prefix=filter_cfg.get("prefix", "") or "",
    )
    df = filter_complete_lifecycle(df)
    summary = summarize(df)

    # 2. 流程发现
    disc_cfg = cfg["discovery"]
    net, im, fm = discover_petri_net(
        df,
        algorithm=disc_cfg["algorithm"],
        noise_threshold=disc_cfg["noise_threshold"],
    )
    dfg_svg = export_dfg_svg(df)
    petri_svg = export_petri_net_svg(net, im, fm)
    process_stats = get_process_stats(net)
    se_acts = get_start_end_activities(df)
    variants = get_variants(df, top_k=10)

    # 3. 合规检查
    conf_cfg = cfg["conformance"]
    conformance = conformance_check(
        df, net, im, fm, method=conf_cfg["method"]
    )

    # 4. 性能分析
    perf_cfg = cfg["performance"]
    # 详细 case durations 用于报告分布图
    durations_df = case_durations(df)
    performance = full_performance_report(
        df,
        sla_days=perf_cfg["sla_days"],
        top_n=perf_cfg["bottleneck_top_n"],
    )

    # 5. LLM 智能解读(可选:仅 llm.enabled=true 且有 API key 时挂载)
    llm_client = build_llm_client(cfg, root=project_root)
    insight = None
    if llm_client.available:
        facts = build_single_facts(
            dataset=dataset_name, summary=summary, process_stats=process_stats,
            conformance=conformance, performance=performance, variants=variants,
            max_variants=int(llm_settings(cfg).get("max_variants", 8)),
        )
        insight = maybe_generate_insight(facts, llm_client)
        logger.info("LLM 智能解读: %s",
                    "已生成" if insight and insight.source == "llm" else "已降级")

    # 6. 报告组装
    html_str = build_report(
        title=cfg["report"]["title"],
        dataset_description=ds_cfg["description"],
        summary=summary,
        bpmn_xml=petri_svg,
        dfg_svg=dfg_svg,
        process_stats=process_stats,
        variants=variants,
        start_activities=se_acts["start_activities"],
        conformance=conformance,
        performance=performance,
        sla_days=perf_cfg["sla_days"],
        bottleneck_n=perf_cfg["bottleneck_top_n"],
        durations_df=durations_df,  # 显式传入
        llm_insight=insight.to_dict() if insight else None,
    )
    report_path = write_report(
        html_str,
        output_dir=project_root / cfg["report"]["output_dir"],
        filename_prefix=f"report_{dataset_name}",
    )
    logger.info("报告已生成: %s", report_path)
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="datamind BPM 分析 PoC")
    parser.add_argument("--dataset", required=True,
                        help="config.yaml 中定义的 dataset 名,如 domestic_declarations")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args(argv)

    try:
        report_path = run_pipeline(args.dataset, args.config)
    except Exception as e:
        logger.exception("流水线失败: %s", e)
        return 1
    print(f"\n报告: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())