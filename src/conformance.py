"""合规检查:基于推导模型评估日志的偏差。"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def conformance_check(
    df: pd.DataFrame,
    net: Any,
    im: Any,
    fm: Any,
    method: str = "token_replay",
) -> dict:
    """
    对日志执行合规检查。

    两种方法:
    - token_replay:快速,统计每个 trace 的 token 消耗
    - alignments:精确但慢,产出同步移动/日志移动/模型移动

    返回结构化的偏差统计,便于报告展示。
    """
    import pm4py

    log = pm4py.convert_to_event_log(df)

    result: dict = {"method": method, "total_cases": 0}

    if method == "token_replay":
        replayed = pm4py.conformance_diagnostics_token_based_replay(log, net, im, fm)
        # replayed: list of dicts,每元素对应一个 trace
        num_fitting = sum(1 for r in replayed if r.get("trace_is_fit"))
        num_cases = len(replayed)
        consumed = [r.get("consumed_tokens", 0) for r in replayed]
        produced = [r.get("produced_tokens", 0) for r in replayed]
        missing = [r.get("missing_tokens", 0) for r in replayed]
        remaining = [r.get("remaining_tokens", 0) for r in replayed]

        result.update({
            "num_cases": num_cases,
            "num_fitting": num_fitting,
            "num_non_fitting": num_cases - num_fitting,
            "fitness": (num_fitting / num_cases) if num_cases else 0,
            "avg_consumed_tokens": sum(consumed) / num_cases if num_cases else 0,
            "avg_produced_tokens": sum(produced) / num_cases if num_cases else 0,
            "avg_missing_tokens": sum(missing) / num_cases if num_cases else 0,
            "avg_remaining_tokens": sum(remaining) / num_cases if num_cases else 0,
        })
        # 非拟合 trace 抽样用于报告展示
        non_fit_samples = [
            {"trace": r.get("trace", ""), "missing": r.get("missing_tokens", 0)}
            for r in replayed
            if not r.get("trace_is_fit")
        ][:10]
        result["non_fitting_samples"] = non_fit_samples

    elif method == "alignments":
        aligned = pm4py.conformance_diagnostics_alignments(log, net, im, fm)
        # aligned: list of AlignmentResult,每个含 .cost / .alignment
        num_cases = len(aligned)
        avg_cost = sum(a.cost for a in aligned) / num_cases if num_cases else 0
        result.update({
            "num_cases": num_cases,
            "avg_cost": avg_cost,
        })
    else:
        raise ValueError(f"未知合规检查方法: {method}")

    logger.info(
        "合规检查 (%s): %d cases, fitness=%.3f",
        method, result.get("num_cases", 0), result.get("fitness", 0)
    )
    return result


def get_diagnostics_summary(replay_result: dict) -> dict:
    """
    把 token replay 结果转换为对架构师友好的可视化数据:
    - 拟合 vs 非拟合占比
    - 缺失 token 分布(提示哪些活动未被模型覆盖)
    """
    if "num_fitting" not in replay_result:
        return {}

    total = replay_result["num_cases"]
    return {
        "fitting_pct": round(replay_result["num_fitting"] / total * 100, 2) if total else 0,
        "non_fitting_pct": round(replay_result["num_non_fitting"] / total * 100, 2) if total else 0,
        "avg_missing_tokens": round(replay_result.get("avg_missing_tokens", 0), 3),
        "avg_remaining_tokens": round(replay_result.get("avg_remaining_tokens", 0), 3),
    }