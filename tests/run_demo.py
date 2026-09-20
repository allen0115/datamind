"""在 datamind 项目 outputs 目录里跑一个端到端测试,产出真实可查的报告。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_loader import load_xes, summarize, filter_complete_lifecycle
from src.process_discovery import (
    discover_petri_net, export_bpmn_xml, get_process_stats,
    get_start_end_activities, get_variants,
)
from src.conformance import conformance_check
from src.performance import case_durations, full_performance_report
from src.report_generator import build_report, write_report
from tests.test_smoke import make_synthetic_xes, to_xes_via_pm4py


def main():
    df = make_synthetic_xes()
    project_root = Path("/sessions/stoic-adoring-faraday/mnt/datamind")
    tmpdir = project_root / "outputs"
    tmpdir.mkdir(parents=True, exist_ok=True)
    xes_path = to_xes_via_pm4py(df, tmpdir / "synthetic_demo.xes")
    print(f"合成 XES: {xes_path}")

    loaded = load_xes(xes_path, max_cases=50)
    loaded = filter_complete_lifecycle(loaded)
    summary = summarize(loaded)
    print(f"加载: {summary['num_events']} 事件 / {summary['num_cases']} cases / {summary['num_activities']} 活动")

    net, im, fm = discover_petri_net(loaded, algorithm="inductive", noise_threshold=0.2)
    bpmn = export_bpmn_xml(net, im, fm)
    ps = get_process_stats(net)
    se = get_start_end_activities(loaded)
    variants = get_variants(loaded, top_k=10)
    print(f"流程模型: {ps['num_transitions']} transitions, {len(variants)} variants")

    conf = conformance_check(loaded, net, im, fm, method="token_replay")
    print(f"合规度: {conf.get('fitness', 0):.2%}")

    durations = case_durations(loaded)
    perf = full_performance_report(loaded, sla_days=14, top_n=10)
    print(f"性能: 平均 {perf['case_durations']['mean_days']} 天,P90 {perf['case_durations']['p90_days']} 天")

    html = build_report(
        title="datamind 演示报告 · 合成 BPM 数据",
        dataset_description="合成国内报销流程(模拟 BPIC 2020)",
        summary=summary,
        bpmn_xml=bpmn,
        process_stats=ps,
        variants=variants,
        start_activities=se["start_activities"],
        conformance=conf,
        performance=perf,
        sla_days=14,
        bottleneck_n=10,
        durations_df=durations,
    )
    out = write_report(html, project_root / "reports", filename_prefix="demo_report")
    print(f"\n✓ 报告已生成: {out}")
    print(f"  文件大小: {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()