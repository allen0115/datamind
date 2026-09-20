"""
烟囱测试:用合成 XES 数据验证整条流水线跑得通。

如果环境里没有 BPIC 真实数据,可以跑这个测试生成一个
与 BPIC 2020 DomesticDeclarations 结构相似的小型合成日志,
并走完所有分析步骤,产出报告。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd

# 让脚本可以以模块方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_synthetic_xes() -> pd.DataFrame:
    """构造一个小型合成 BPM 事件日志,模拟国内报销流程。"""
    import random

    random.seed(42)

    activities = [
        "Submit Declaration",
        "Check Completeness",
        "Request Missing Info",
        "Approve by Manager",
        "Approve by Finance",
        "Pay",
        "Reject",
    ]

    records = []
    case_id = 1
    base_ts = pd.Timestamp("2024-01-01", tz="UTC")

    for _ in range(50):  # 50 个 case
        ts = base_ts + pd.Timedelta(days=case_id)
        submitted = False
        approved_mgr = False
        approved_fin = False

        # 构造主流程
        n_steps = random.randint(5, 12)
        for step in range(n_steps):
            if not submitted:
                act = "Submit Declaration"
                submitted = True
            elif not approved_mgr and random.random() < 0.85:
                if random.random() < 0.2:
                    act = "Request Missing Info"
                else:
                    act = "Approve by Manager"
                    approved_mgr = True
            elif not approved_fin and random.random() < 0.7:
                act = "Approve by Finance"
                approved_fin = True
            elif random.random() < 0.1:
                act = "Reject"
                break
            else:
                act = random.choice(["Pay", "Check Completeness"])

            resource = random.choice(["alice", "bob", "carol", "dave", "eve"])
            records.append({
                "case:concept:name": f"case_{case_id:04d}",
                "concept:name": act,
                "time:timestamp": ts,
                "org:resource": resource,
                "lifecycle:transition": "complete",
            })
            ts = ts + pd.Timedelta(hours=random.randint(1, 72))

        case_id += 1

    df = pd.DataFrame(records)
    df.to_csv(tempfile.mktemp(suffix=".csv"), index=False)
    return df


def to_xes_via_pm4py(df: pd.DataFrame, path: Path) -> Path:
    """把 DataFrame 转成 XES 文件(便于 pm4py 直接读取)。"""
    import pm4py
    log = pm4py.convert_to_event_log(df)
    pm4py.write_xes(log, str(path))
    return path


def test_pipeline_with_synthetic():
    from src.data_loader import load_xes, summarize, filter_complete_lifecycle
    from src.process_discovery import (
        discover_petri_net, export_bpmn_xml, get_process_stats,
        get_start_end_activities, get_variants,
    )
    from src.conformance import conformance_check
    from src.performance import case_durations, full_performance_report
    from src.report_generator import build_report, write_report

    df = make_synthetic_xes()
    with tempfile.TemporaryDirectory() as tmpdir:
        xes_path = to_xes_via_pm4py(df, Path(tmpdir) / "synthetic.xes")
        print(f"合成 XES 已写入: {xes_path}")

        loaded = load_xes(xes_path, max_cases=20)
        loaded = filter_complete_lifecycle(loaded)
        summary = summarize(loaded)
        print(f"加载完成: {summary['num_events']} 事件, {summary['num_cases']} cases")

        net, im, fm = discover_petri_net(loaded, algorithm="inductive", noise_threshold=0.2)
        bpmn = export_bpmn_xml(net, im, fm)
        ps = get_process_stats(net)
        se = get_start_end_activities(loaded)
        variants = get_variants(loaded, top_k=5)

        conformance = conformance_check(loaded, net, im, fm, method="token_replay")
        durations = case_durations(loaded)
        performance = full_performance_report(loaded, sla_days=14, top_n=5)

        html_str = build_report(
            title="datamind PoC · 合成数据测试报告",
            dataset_description="合成报销流程(模拟 BPIC 2020 DomesticDeclarations)",
            summary=summary,
            bpmn_xml=bpmn,
            process_stats=ps,
            variants=variants,
            start_activities=se["start_activities"],
            conformance=conformance,
            performance=performance,
            sla_days=14,
            bottleneck_n=5,
            durations_df=durations,
        )
        out = write_report(html_str, Path(tmpdir) / "reports", "smoke_test")
        print(f"\n✓ 烟囱测试通过,报告: {out}")
        assert out.exists()
        assert out.stat().st_size > 10000, "报告文件过小,可能渲染失败"


if __name__ == "__main__":
    test_pipeline_with_synthetic()