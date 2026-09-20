"""BPMN 模拟 → XES / OCEL 转换链路测试。

覆盖三件事:
  1. BPMN 模型本身结构合法、能导出可解析的 BPMN 2.0 XML
  2. 模拟器产出的源表满足引用完整性(任务归属实例、时间不倒流)
  3. 转换器的口径正确(会签折叠、孤立事件为 0)且产物能被 pm4py 读回
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pandas as pd
import pytest

from src.bpmn_converter import (
    ACT,
    EID,
    OID,
    OTYPE,
    QUAL,
    TS,
    build_ocel,
    convert,
    extract_events,
    to_xes_df,
    write_ocel,
    write_xes,
)
from src.bpmn_model import (
    ACTIVITY_KINDS,
    PROCESSES,
    export_bpmn_xml,
    to_bpmn_xml,
)
from src.bpmn_simulator import SimConfig, simulate

_NS = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"


@pytest.fixture(scope="module")
def tables():
    return simulate(SimConfig(n_requisitions=30, seed=7))


@pytest.fixture(scope="module")
def converted(tables):
    return convert(tables)


# --------------------------------------------------------------------------
# 1. 模型
# --------------------------------------------------------------------------

def test_model_is_valid():
    for p in PROCESSES:
        p.validate()


def test_bpmn_xml_is_wellformed():
    root = ET.fromstring(to_bpmn_xml())
    assert root.tag == f"{_NS}definitions"
    proc_ids = {p.get("id") for p in root.findall(f"{_NS}process")}
    assert proc_ids == {p.key for p in PROCESSES}


def test_bpmn_xml_has_diagram():
    xml = to_bpmn_xml()
    assert "bpmndi:BPMNPlane" in xml
    assert "di:waypoint" in xml


def test_export_bpmn_xml_to_disk(tmp_path):
    path = export_bpmn_xml(tmp_path / "nested" / "model.bpmn")
    assert path.exists()
    assert ET.parse(path).getroot() is not None


# --------------------------------------------------------------------------
# 2. 模拟源表
# --------------------------------------------------------------------------

def test_source_tables_referential_integrity(tables):
    pi_ids = set(tables.process_instance["instance_id"])
    assert set(tables.task["instance_id"]).issubset(pi_ids)
    assert set(tables.variable["instance_id"]).issubset(pi_ids)


def test_task_time_ordering(tables):
    bad = tables.task[tables.task["end_time"] < tables.task["start_time"]]
    assert bad.empty


def test_business_keys_are_unique_per_process(tables):
    for key in ("purchase_requisition", "purchase_order", "goods_receipt", "invoice"):
        sub = tables.process_instance[tables.process_instance["process_key"] == key]
        assert sub["business_key"].is_unique, f"{key} 的 business_key 有重复"


def test_multi_instance_produces_multiple_task_rows(tables):
    mi = tables.task[tables.task["node_id"] == "n_mgr_appr"]
    assert not mi.empty
    # 同一个活动实例下应有 >= 2 条任务(会签)
    per_act = mi.groupby("act_inst_id")["task_id"].nunique()
    assert (per_act >= 2).all()


# --------------------------------------------------------------------------
# 3. 转换口径
# --------------------------------------------------------------------------

def test_gateway_and_events_are_excluded(converted):
    events = converted.events
    assert not events.empty
    # 网关/开始/结束事件不产生日志条目
    assert "是否需审批" not in set(events[ACT])
    assert "采购需求提出" not in set(events[ACT])


def test_multi_instance_is_collapsed(tables, converted):
    mi_events = converted.events[converted.events["is_multi_instance"]]
    assert not mi_events.empty
    mi_tasks = tables.task[tables.task["node_id"] == "n_mgr_appr"]
    # 任务行数应多于折叠后的事件数
    assert len(mi_tasks) > len(mi_events[mi_events["node_id"] == "n_mgr_appr"])


def test_event_ids_are_unique(converted):
    assert converted.events[EID].is_unique


def test_no_isolated_events(converted):
    q = converted.quality
    assert q["isolated_events"] == 0
    assert q["dangling_relations"] == 0


def test_all_objects_are_reachable(converted):
    assert set(converted.relations[OID]).issubset(set(converted.objects[OID]))


def test_relations_cover_all_activities(converted):
    """每个活动都至少关联一种对象(否则说明注册表漏配)。"""
    ev = converted.events[[EID, ACT]].merge(converted.relations[[EID]], on=EID)
    assert set(ev[ACT]) == set(converted.events[ACT])


def test_resource_relations_present(converted):
    assert "performer" in set(converted.relations[QUAL])


def test_quality_metrics_reasonable(converted):
    q = converted.quality
    assert q["avg_objects_per_event"] >= 1.5, "多对象关联没建立起来"
    assert q["timestamp_unique_pct"] > 95.0, "时间戳重复过多,时延不可信"
    assert q["num_object_types"] == 9


# --------------------------------------------------------------------------
# 4. 产物可回读
# --------------------------------------------------------------------------

def test_xes_instance_roundtrip(converted, tmp_path):
    df = to_xes_df(converted.events, converted.relations, "instance")
    assert {"case:concept:name", "concept:name", "time:timestamp"} <= set(df.columns)
    path = write_xes(df, tmp_path / "sim_instance.xes")
    assert path.exists()

    import pm4py

    log = pm4py.read_xes(str(path))
    assert len(log) == len(df)
    assert log["case:concept:name"].nunique() == converted.events["instance_id"].nunique()


def test_xes_object_flattening_duplicates_events(converted):
    """按对象拍平后,一个事件会落进多条 trace —— 这正是 OCEL 要解决的问题。"""
    inst = to_xes_df(converted.events, converted.relations, "instance")
    flat = to_xes_df(converted.events, converted.relations, "object:purchase_order")
    assert len(flat) > 0
    assert len(flat) < len(inst)   # 只覆盖与订单相关的事件
    per_case = flat.groupby("case:concept:name").size()
    assert per_case.mean() > 1     # 每张订单是一条多事件 trace

    # 同一事件会同时出现在订单 trace、发票 trace、员工 trace 里 —— 这就是"拍扁"的代价
    total = sum(
        len(to_xes_df(converted.events, converted.relations, f"object:{t}"))
        for t in converted.objects[OTYPE].unique()
    )
    assert total > len(converted.events)


def test_xes_unknown_case_notion_raises(converted):
    with pytest.raises(ValueError):
        to_xes_df(converted.events, converted.relations, "object:not_exist")


def test_ocel_roundtrip(converted, tmp_path):
    ocel = build_ocel(converted.events, converted.objects,
                      converted.relations, converted.o2o)
    path = write_ocel(ocel, tmp_path / "sim.jsonocel")
    assert path.exists()

    from src.ocel_loader import load_ocel, summarize_ocel

    loaded = load_ocel(path)
    summary = summarize_ocel(loaded)
    assert summary["num_events"] == len(converted.events)
    assert summary["num_objects"] == len(converted.objects)
    assert summary["isolated_events"] == 0


def test_extract_events_only_keeps_activity_kinds(tables):
    events = extract_events(tables)
    kinds = set(tables.task["task_type"].unique())
    assert kinds & set(ACTIVITY_KINDS), "模拟数据里应当有 userTask/serviceTask"
    assert len(events) <= len(tables.task)
    assert pd.api.types.is_datetime64_any_dtype(events[TS])
    assert events[OTYPE].equals(events[ACT])
