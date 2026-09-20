"""性能与瓶颈分析:活动耗时、资源负载、SLA 偏离、退回率。"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

CASE_COL = "case:concept:name"
ACT_COL = "concept:name"
TS_COL = "time:timestamp"
RES_COL = "org:resource"


def case_durations(df: pd.DataFrame) -> pd.DataFrame:
    """每个 case 的总耗时(小时/天)。"""
    if TS_COL not in df.columns or CASE_COL not in df.columns:
        return pd.DataFrame()
    grp = df.groupby(CASE_COL)[TS_COL].agg(["min", "max"]).reset_index()
    grp["duration_hours"] = (grp["max"] - grp["min"]).dt.total_seconds() / 3600
    grp["duration_days"] = grp["duration_hours"] / 24
    return grp


def activity_durations(df: pd.DataFrame) -> pd.DataFrame:
    """
    每个活动在同一 case 内的耗时:
    duration = next_event.timestamp - this_event.timestamp
    这是"等待 + 处理"时间,适合发现停滞点。
    """
    if TS_COL not in df.columns:
        return pd.DataFrame()
    df = df.sort_values([CASE_COL, TS_COL]).copy()
    df["next_ts"] = df.groupby(CASE_COL)[TS_COL].shift(-1)
    df["waiting_hours"] = (df["next_ts"] - df[TS_COL]).dt.total_seconds() / 3600
    df["waiting_days"] = df["waiting_hours"] / 24
    stats = df.groupby(ACT_COL)["waiting_hours"].agg([
        ("count", "count"),
        ("mean", "mean"),
        ("median", "median"),
        ("p90", lambda s: s.quantile(0.9)),
        ("p99", lambda s: s.quantile(0.99)),
        ("max", "max"),
    ]).reset_index().sort_values("mean", ascending=False)
    return stats


def resource_load(df: pd.DataFrame) -> pd.DataFrame:
    """每个资源(审批人)处理的事件数与平均耗时。"""
    if RES_COL not in df.columns:
        return pd.DataFrame()
    agg_dict = {
        "event_count": (ACT_COL, "count"),
        "activity_diversity": (ACT_COL, "nunique"),
    }
    if CASE_COL in df.columns:
        agg_dict["case_count"] = (CASE_COL, "nunique")
    load = df.groupby(RES_COL).agg(**agg_dict).reset_index().sort_values("event_count", ascending=False)
    return load


def rework_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """
    退回/重复分析:同一 case 内同一活动出现多次视为 rework。
    返回每个活动的 rework 频次与占比。
    """
    if CASE_COL not in df.columns or ACT_COL not in df.columns:
        return pd.DataFrame()
    activity_per_case = df.groupby([CASE_COL, ACT_COL]).size().reset_index(name="count")
    rework = activity_per_case[activity_per_case["count"] > 1]
    rework_summary = rework.groupby(ACT_COL).agg(
        rework_cases=(CASE_COL, "nunique"),
        avg_repetitions=("count", "mean"),
        max_repetitions=("count", "max"),
    ).reset_index().sort_values("rework_cases", ascending=False)
    total_cases_per_activity = df.groupby(ACT_COL)[CASE_COL].nunique().reset_index(name="total_cases")
    rework_summary = rework_summary.merge(total_cases_per_activity, on=ACT_COL, how="left")
    rework_summary["rework_rate"] = (rework_summary["rework_cases"] /
                                     rework_summary["total_cases"]).round(3)
    return rework_summary


def sla_breach(df: pd.DataFrame, sla_days: int = 14) -> dict:
    """统计 SLA 偏离(case 总耗时 > sla_days)的占比。"""
    durations = case_durations(df)
    if durations.empty:
        return {"sla_days": sla_days, "breach_count": 0, "total": 0, "breach_rate": 0}
    breach = durations[durations["duration_days"] > sla_days]
    total = len(durations)
    return {
        "sla_days": sla_days,
        "breach_count": int(len(breach)),
        "total": int(total),
        "breach_rate": round(len(breach) / total, 3) if total else 0,
        "avg_breach_days": round(breach["duration_days"].mean(), 2) if len(breach) else 0,
    }


def bottleneck_top_n(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """综合耗时与频次,排出 Top N 瓶颈节点。"""
    activity_stats = activity_durations(df)
    if activity_stats.empty:
        return pd.DataFrame()
    activity_stats["impact_score"] = (
        activity_stats["mean"] * activity_stats["count"]
    )
    return activity_stats.sort_values("impact_score", ascending=False).head(n)


def full_performance_report(df: pd.DataFrame, sla_days: int = 14,
                            top_n: int = 10) -> dict:
    """聚合所有性能指标,返回单一字典供报告消费。"""
    logger.info("开始性能分析 (SLA=%d 天)", sla_days)
    durations = case_durations(df)
    act_stats = activity_durations(df)
    bottleneck = bottleneck_top_n(df, n=top_n)
    rework = rework_analysis(df)
    sla = sla_breach(df, sla_days=sla_days)
    resource = resource_load(df)

    return {
        "case_durations": {
            "mean_days": round(float(durations["duration_days"].mean()), 2) if not durations.empty else 0,
            "median_days": round(float(durations["duration_days"].median()), 2) if not durations.empty else 0,
            "p90_days": round(float(durations["duration_days"].quantile(0.9)), 2) if not durations.empty else 0,
            "min_days": round(float(durations["duration_days"].min()), 2) if not durations.empty else 0,
            "max_days": round(float(durations["duration_days"].max()), 2) if not durations.empty else 0,
        },
        "activity_stats": act_stats.to_dict(orient="records"),
        "bottleneck": bottleneck.to_dict(orient="records"),
        "rework": rework.to_dict(orient="records"),
        "sla": sla,
        "resource_load": resource.head(20).to_dict(orient="records") if not resource.empty else [],
    }