"""OCEL 2.0 加载与基础统计。

OCEL(Object-Centric Event Log)与普通事件日志的根本差别:
一个事件可以同时关联多个对象(订单、发票、收货单……),
对象之间通过共享事件建立联系。这正是挖掘"流程 A 触发流程 B"
的数据基础——在普通日志里这种关系是丢失的。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# pm4py OCEL 的标准列名
EID = "ocel:eid"
OID = "ocel:oid"
OTYPE = "ocel:type"
ACT = "ocel:activity"
TS = "ocel:timestamp"
QUAL = "ocel:qualifier"


def load_ocel(path: str | Path, variant: str = "ocel20_standard",
              max_objects: Optional[int] = None) -> Any:
    """
    读取 OCEL 2.0 日志(pm4py OCEL 对象)。

    Parameters
    ----------
    path : 文件路径
        OCEL 2.0 JSON(.jsonocel)或 SQLite
    variant : str
        'ocel20_standard' 对应 OCEL 2.0 JSON 规范。
        注意 pm4py 的 'classic' 变体只认 OCEL 1.0 的键名,
        喂 OCEL 2.0 文件会报 KeyError: 'ocel:objects'。
    max_objects : int | None
        只保留覆盖前 N 个对象的事件,用于快速试跑
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"OCEL 文件不存在: {path}")

    import pm4py
    from pm4py.objects.ocel.importer.jsonocel import importer as json_importer
    from pm4py.objects.ocel.importer.jsonocel.variants import ocel20_standard

    logger.info("加载 OCEL: %s", path.name)
    if path.suffix in (".jsonocel", ".json"):
        ocel = json_importer.apply(str(path), variant=ocel20_standard)
    else:
        ocel = pm4py.read_ocel(str(path))

    logger.info(
        "加载完成: %d 事件 / %d 对象 / %d 关联",
        len(ocel.events), len(ocel.objects), len(ocel.relations),
    )

    if max_objects is not None:
        ocel = _sample_by_objects(ocel, max_objects)

    return ocel


def _sample_by_objects(ocel: Any, max_objects: int) -> Any:
    """按对象抽样:保留前 N 个对象相关的事件与关联。"""
    from pm4py.objects.ocel.obj import OCEL

    keep_oids = ocel.objects[OID].head(max_objects).tolist()
    rel = ocel.relations[ocel.relations[OID].isin(keep_oids)]
    keep_eids = rel[EID].unique().tolist()

    new = OCEL(
        events=ocel.events[ocel.events[EID].isin(keep_eids)].reset_index(drop=True),
        objects=ocel.objects[ocel.objects[OID].isin(keep_oids)].reset_index(drop=True),
        relations=rel.reset_index(drop=True),
    )
    logger.info("抽样后: %d 事件 / %d 对象 / %d 关联",
                len(new.events), len(new.objects), len(new.relations))
    return new


def summarize_ocel(ocel: Any) -> dict:
    """OCEL 概览统计,用于报告头部。"""
    ev, ob, rel = ocel.events, ocel.objects, ocel.relations

    ts = pd.to_datetime(ev[TS], utc=True, errors="coerce") if TS in ev.columns else None
    summary = {
        "num_events": int(len(ev)),
        "num_objects": int(len(ob)),
        "num_relations": int(len(rel)),
        "num_event_types": int(ev[ACT].nunique()),
        "num_object_types": int(ob[OTYPE].nunique()),
        "avg_objects_per_event": round(len(rel) / len(ev), 2) if len(ev) else 0,
    }
    if ts is not None and ts.notna().any():
        summary["time_range_start"] = str(ts.min())
        summary["time_range_end"] = str(ts.max())
        summary["span_days"] = int((ts.max() - ts.min()).days)

    # 孤立事件:不关联任何对象的事件(OCEL 质量的关键指标)
    linked_eids = set(rel[EID].unique())
    isolated = int((~ev[EID].isin(linked_eids)).sum())
    summary["isolated_events"] = isolated
    summary["isolated_pct"] = round(isolated / len(ev) * 100, 2) if len(ev) else 0
    return summary


def object_type_stats(ocel: Any) -> pd.DataFrame:
    """各对象类型的统计:对象数、涉及事件数、涉及活动数、平均生命周期。"""
    ob, rel = ocel.objects, ocel.relations
    rows = []
    for otype in ob[OTYPE].unique():
        oids = ob.loc[ob[OTYPE] == otype, OID]
        sub = rel[rel[OID].isin(oids)]
        # 该类型对象参与的唯一事件
        n_events = sub[EID].nunique()
        n_activities = sub[ACT].nunique()

        # 单个对象的事件数分布
        per_obj = sub.groupby(OID)[EID].nunique()
        rows.append({
            "object_type": otype,
            "num_objects": int(len(oids)),
            "num_events": int(n_events),
            "num_activities": int(n_activities),
            "avg_events_per_object": round(per_obj.mean(), 2) if len(per_obj) else 0,
            "max_events_per_object": int(per_obj.max()) if len(per_obj) else 0,
        })
    return pd.DataFrame(rows).sort_values("num_objects", ascending=False).reset_index(drop=True)


def activity_object_profile(ocel: Any,
                            exclude_qualifiers: tuple[str, ...] = ()) -> pd.DataFrame:
    """
    每个活动关联的对象类型画像。

    用来判断一个活动"属于"哪个子流程:
    比如 'Create Purchase Requisition' 主要碰 BANFN(采购申请),
    'Post Goods Receipt for PO' 主要碰 MBLNR(物料凭证)。

    exclude_qualifiers: 计算主导类型时要排除的关联限定符。
    资源类关联(如 performer / 员工对象)不代表业务子流程归属,
    若不排除,它会和主对象并列甚至抢占主导,把同流程内的相邻活动
    误判成"跨流程"。
    """
    rel = ocel.relations
    if exclude_qualifiers and QUAL in rel.columns:
        rel = rel[~rel[QUAL].isin(exclude_qualifiers)]
    ct = rel.groupby([ACT, OTYPE]).size().reset_index(name="count")
    total = ct.groupby(ACT)["count"].transform("sum")
    ct["share"] = (ct["count"] / total).round(3)

    # 主导类型:优先取"主对象"(qualifier=main),即该活动所属流程的主单据。
    # 只看计数会被展开出来的子对象带偏 —— 比如收货过账同时过账多行,
    # gr_item 的关联数远大于 goods_receipt,按计数就会把收货判成"属于行项目"。
    # 没有主对象关联时(官方数据集 qualifier 全为空)退回计数最大的类型。
    if QUAL in rel.columns and (rel[QUAL] == "main").any():
        main_ct = (rel[rel[QUAL] == "main"]
                   .groupby([ACT, OTYPE]).size()
                   .reset_index(name="main_count"))
        dominant = main_ct.loc[main_ct.groupby(ACT)["main_count"].idxmax(), [ACT, OTYPE]]
    else:
        dominant = ct.loc[ct.groupby(ACT)["count"].idxmax(), [ACT, OTYPE]]
    dominant.columns = [ACT, "dominant_object_type"]
    return ct.merge(dominant, on=ACT).sort_values(["count"], ascending=False).reset_index(drop=True)


def event_type_stats(ocel: Any) -> pd.DataFrame:
    """各活动(事件类型)的事件数、关联对象数、平均关联对象数。"""
    ev, rel = ocel.events, ocel.relations
    agg = rel.groupby(ACT).agg(
        num_relations=(EID, "size"),
        num_objects=(OID, "nunique"),
        num_object_types=(OTYPE, "nunique"),
    ).reset_index()
    evc = ev.groupby(ACT).size().reset_index(name="num_events")
    out = evc.merge(agg, on=ACT, how="left").fillna(0)
    out["avg_objects_per_event"] = (out["num_relations"] / out["num_events"]).round(2)
    return out.sort_values("num_events", ascending=False).reset_index(drop=True)
