"""XES 事件日志加载、采样、标准化。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def load_xes(
    xes_path: str | Path,
    max_cases: Optional[int] = None,
    case_id_prefix: str = "",
) -> pd.DataFrame:
    """
    加载 XES 事件日志为 DataFrame。

    返回的 DataFrame 至少包含以下列(已标准化):
        case_id      - 流程实例 ID
        activity     - 活动名称(标准化为小写、去除首尾空白)
        timestamp    - 时间戳(pd.Timestamp)
        resource     - 执行人/资源(可选)
        lifecycle    - 生命周期状态(可选,例如 'start' / 'complete')
        + 其余 XES 中保留的属性列

    Parameters
    ----------
    xes_path : str | Path
        XES 文件路径
    max_cases : int | None
        限制最多保留的 case 数,用于快速 PoC
    case_id_prefix : str
        仅保留 case id 以此前缀开头的 case,空字符串表示不过滤
    """
    xes_path = Path(xes_path)
    if not xes_path.exists():
        raise FileNotFoundError(f"XES 文件不存在: {xes_path}")

    logger.info("加载 XES 文件: %s", xes_path)
    try:
        import pm4py

        log = pm4py.read_xes(str(xes_path))
        df = pm4py.convert_to_dataframe(log)
    except Exception as e:
        raise RuntimeError(f"XES 解析失败: {e}") from e

    # pm4py 默认列名(XES 标准),我们保留以便 pm4py 各函数直接工作
    # 不做重命名,让 'case:concept:name', 'concept:name', 'time:timestamp' 保持原样
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")

    if "concept:name" in df.columns:
        df["concept:name"] = df["concept:name"].astype(str).str.strip()

    # case id 前缀过滤
    if case_id_prefix and "case:concept:name" in df.columns:
        before = df["case:concept:name"].nunique()
        df = df[df["case:concept:name"].astype(str).str.startswith(case_id_prefix)]
        logger.info("按前缀 %s 过滤: %d -> %d cases",
                    case_id_prefix, before, df["case:concept:name"].nunique())

    # 限制 case 数(保留每个 case 的所有事件)
    if max_cases is not None and "case:concept:name" in df.columns and df["case:concept:name"].nunique() > max_cases:
        keep_cases = df["case:concept:name"].drop_duplicates().head(max_cases).tolist()
        df = df[df["case:concept:name"].isin(keep_cases)]
        logger.info("限制 case 数: 保留 %d cases", max_cases)

    if "time:timestamp" in df.columns:
        df = df.sort_values(["case:concept:name", "time:timestamp"]).reset_index(drop=True)

    logger.info(
        "加载完成: %d 事件, %d cases, %d 活动",
        len(df), df["case:concept:name"].nunique() if "case:concept:name" in df.columns else 0,
        df["concept:name"].nunique() if "concept:name" in df.columns else 0,
    )
    return df


def summarize(df: pd.DataFrame) -> dict:
    """生成事件日志的概要统计,用于报告 header。"""
    summary = {
        "num_events": int(len(df)),
        "num_cases": int(df["case:concept:name"].nunique()) if "case:concept:name" in df.columns else 0,
        "num_activities": int(df["concept:name"].nunique()) if "concept:name" in df.columns else 0,
        "columns": list(df.columns),
    }
    if "time:timestamp" in df.columns and df["time:timestamp"].notna().any():
        summary["time_range_start"] = str(df["time:timestamp"].min())
        summary["time_range_end"] = str(df["time:timestamp"].max())
    if "org:resource" in df.columns:
        summary["num_resources"] = int(df["org:resource"].nunique())
    return summary


def filter_complete_lifecycle(df: pd.DataFrame) -> pd.DataFrame:
    """
    仅保留 lifecycle == 'complete' 的事件(去除 start 行),
    避免双计数。
    """
    if "lifecycle:transition" not in df.columns:
        return df
    before = len(df)
    df = df[df["lifecycle:transition"].astype(str).str.lower() == "complete"]
    logger.info("保留 complete 事件: %d -> %d", before, len(df))
    return df.reset_index(drop=True)