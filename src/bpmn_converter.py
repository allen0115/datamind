"""转换工具:BPM 引擎执行数据 → XES / OCEL 2.0。

这是上一轮结论的落地实现,四条关联规则按优先级依次执行:

  1. business_key   流程实例的 business_key → 主对象(引擎唯一的原生线索)
  2. variable       流程变量 → 对象(对象 ID 的主要藏身处)
  3. expansion      命中指定活动时,父对象展开成子对象(订单 → 行项目 → 物料)
  4. resource       assignee → 员工对象(资源维度)

两条口径约定(决定了日志能不能用):
  - 事件 = 一个「活动实例」,多实例会签的 N 条任务折叠成 1 个事件
  - 只有 userTask / serviceTask 入日志,网关与开始/结束事件是路由噪声,丢弃

XES 侧提供两种 case notion:
  - instance           case = 流程实例(BPM 引擎原生导出形态)
  - object:<otype>     case = 业务对象(OCEL flattening,一个事件会出现在多条 trace 里)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .bpmn_model import ACTIVITY_KINDS, PROCESS_BY_KEY
from .bpmn_registry import DEFAULT_REGISTRY, ObjectRegistry
from .bpmn_simulator import SimTables

# OCEL 2.0 标准列名(与 pm4py / ocel-standard.org 一致)
EID = "ocel:eid"
OID = "ocel:oid"
OID2 = "ocel:oid_2"
OTYPE = "ocel:type"
ACT = "ocel:activity"
TS = "ocel:timestamp"
QUAL = "ocel:qualifier"

# XES 标准列名
XES_CASE = "case:concept:name"
XES_ACT = "concept:name"
XES_TS = "time:timestamp"
XES_RESOURCE = "org:resource"

_EVENT_ATTRS = (
    "instance_id", "process_key", "process_name", "node_id", "outcome",
    "duration_ms", "assignee", "assignee_role", "n_assignees", "is_multi_instance",
)


def _oid_of(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.Series:
    """把若干列拼成对象 ID(复合主键 → 'PO-1|010')。"""
    return df[list(cols)].astype(str).apply("|".join, axis=1)


# --------------------------------------------------------------------------
# 1. 事件:任务表 → 活动实例
# --------------------------------------------------------------------------

def extract_events(tables: SimTables) -> pd.DataFrame:
    """把 bpm_task 折叠成事件表(多实例会签合并,网关不入日志)。"""
    task = tables.task
    df = task[task["task_type"].isin(ACTIVITY_KINDS)].copy()
    df = df.sort_values(["instance_id", "seq", "start_time", "task_id"])

    grouped = df.groupby("act_inst_id", sort=False)
    ev = grouped.agg(
        instance_id=("instance_id", "first"),
        process_key=("process_key", "first"),
        node_id=("node_id", "first"),
        activity=("node_name", "first"),
        task_type=("task_type", "first"),
        timestamp=("end_time", "max"),
        start_time=("start_time", "min"),
        outcome=("outcome", "first"),
        assignee=("assignee", lambda s: ",".join(sorted(set(s)))),
        assignee_role=("assignee_role", "first"),
        n_assignees=("task_id", "nunique"),
        seq=("seq", "first"),
    ).reset_index()

    ev = ev.rename(columns={"act_inst_id": EID, "activity": ACT, "timestamp": TS})
    ev["duration_ms"] = (
        (ev[TS] - ev["start_time"]).dt.total_seconds() * 1000
    ).astype(int)
    ev["is_multi_instance"] = ev["n_assignees"] > 1
    ev[OTYPE] = ev[ACT]                       # OCEL 的 event type 约定等于活动名
    ev["process_name"] = ev["process_key"].map(
        lambda k: PROCESS_BY_KEY[k].name if k in PROCESS_BY_KEY else k
    )
    return ev.sort_values([TS, EID]).reset_index(drop=True)


# --------------------------------------------------------------------------
# 2. 对象:业务表 → 对象表
# --------------------------------------------------------------------------

def extract_objects(tables: SimTables,
                    registry: ObjectRegistry = DEFAULT_REGISTRY) -> pd.DataFrame:
    frames = []
    for spec in registry.objects:
        if spec.table not in tables.business:
            raise KeyError(f"注册表引用了不存在的业务表: {spec.table}")
        t = tables.business[spec.table]
        df = pd.DataFrame({OID: _oid_of(t, spec.id_cols), OTYPE: spec.name})
        for a in spec.attrs:
            if a in t.columns:
                df[a] = t[a].values
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------
# 3. 关联:四条规则按优先级叠加
# --------------------------------------------------------------------------

def _rule_business_key(ev: pd.DataFrame, tables: SimTables) -> pd.DataFrame:
    proc_obj = {k: p.business_object for k, p in PROCESS_BY_KEY.items()}
    pi = tables.process_instance[["instance_id", "process_key", "business_key"]]
    m = ev[[EID, "instance_id"]].merge(pi, on="instance_id", how="left")
    return pd.DataFrame({
        EID: m[EID],
        OID: m["business_key"].astype(str),
        OTYPE: m["process_key"].map(proc_obj),
        QUAL: "main",
    })


def _rule_variable(ev: pd.DataFrame, tables: SimTables,
                   registry: ObjectRegistry) -> pd.DataFrame:
    var = tables.variable[tables.variable["name"].isin(registry.variable_map)].copy()
    if var.empty:
        return pd.DataFrame(columns=[EID, OID, OTYPE, QUAL])
    var[OTYPE] = var["name"].map(registry.variable_map)
    var = var.rename(columns={"value": OID})
    m = ev[[EID, "instance_id"]].merge(
        var[["instance_id", OID, OTYPE]], on="instance_id", how="inner")
    out = pd.DataFrame({EID: m[EID], OID: m[OID].astype(str), OTYPE: m[OTYPE]})
    out[QUAL] = "reference"
    return out


def _rule_expansion(current: pd.DataFrame, ev_act: pd.DataFrame,
                    tables: SimTables, registry: ObjectRegistry) -> list[pd.DataFrame]:
    frames = []
    for rule in registry.expansion_rules:
        child_spec = registry.spec(rule.child_type)
        t = tables.business[rule.table]
        idx = pd.DataFrame({
            "parent": _oid_of(t, rule.join_cols),
            "child": _oid_of(t, child_spec.id_cols),
        }).drop_duplicates()

        merged = current.merge(ev_act, on=EID, how="left")
        hit = merged[(merged[OTYPE] == rule.parent_type)
                     & (merged[ACT].isin(rule.activities))]
        if hit.empty:
            continue
        hit = hit.merge(idx, left_on=OID, right_on="parent", how="inner")
        frames.append(pd.DataFrame({
            EID: hit[EID],
            OID: hit["child"],
            OTYPE: rule.child_type,
            QUAL: rule.qualifier,
        }))
    return frames


def _rule_resource(ev: pd.DataFrame, registry: ObjectRegistry) -> pd.DataFrame:
    if not registry.resource_object:
        return pd.DataFrame(columns=[EID, OID, OTYPE, QUAL])
    tmp = ev[[EID, "assignee"]].copy()
    tmp["_one"] = tmp["assignee"].astype(str).str.split(",")
    tmp = tmp.explode("_one")
    return pd.DataFrame({
        EID: tmp[EID],
        OID: tmp["_one"],
        OTYPE: registry.resource_object,
        QUAL: "performer",
    })


def extract_relations(events: pd.DataFrame, tables: SimTables,
                      registry: ObjectRegistry = DEFAULT_REGISTRY) -> pd.DataFrame:
    ev_act = events[[EID, ACT]]
    frames: list[pd.DataFrame] = [
        _rule_business_key(events, tables),
        _rule_variable(events, tables, registry),
    ]
    # expansion 依赖前面产出的父对象关联,按注册表顺序逐条叠加
    current = pd.concat(frames, ignore_index=True)
    for f in _rule_expansion(current, ev_act, tables, registry):
        frames.append(f)
        current = pd.concat([current, f], ignore_index=True)
    frames.append(_rule_resource(events, registry))

    rel = pd.concat(frames, ignore_index=True)
    rel = rel.dropna(subset=[EID, OID, OTYPE])
    # 同一对 (事件, 对象) 只保留优先级最高的一条
    rel = rel.drop_duplicates(subset=[EID, OID], keep="first")
    return rel.reset_index(drop=True)


# --------------------------------------------------------------------------
# 4. 对象—对象关系
# --------------------------------------------------------------------------

def extract_o2o(tables: SimTables,
                registry: ObjectRegistry = DEFAULT_REGISTRY) -> pd.DataFrame:
    frames = []
    for rule in registry.o2o_rules:
        child_spec = registry.spec(rule.child_type)
        t = tables.business[rule.table]
        frames.append(pd.DataFrame({
            OID: _oid_of(t, child_spec.id_cols),
            OID2: _oid_of(t, rule.join_cols),
            OTYPE: rule.child_type,
            QUAL: rule.qualifier,
        }))
    if not frames:
        return pd.DataFrame(columns=[OID, OID2, OTYPE, QUAL])
    return pd.concat(frames, ignore_index=True).drop_duplicates().reset_index(drop=True)


# --------------------------------------------------------------------------
# 5. XES:两种 case notion
# --------------------------------------------------------------------------

def to_xes_df(events: pd.DataFrame, relations: pd.DataFrame,
              case_notion: str = "instance") -> pd.DataFrame:
    """把事件表拍平成 XES 结构。

    case_notion:
      - 'instance'       case = 流程实例
      - 'object:<otype>' case = 某类业务对象(一个事件会落进多条 trace)
    """
    if case_notion == "instance":
        work = pd.DataFrame({
            XES_CASE: events["instance_id"],
            EID: events[EID],
        })
    elif case_notion.startswith("object:"):
        otype = case_notion.split(":", 1)[1]
        sub = relations[relations[OTYPE] == otype]
        if sub.empty:
            raise ValueError(f"对象类型 {otype} 没有任何事件关联")
        work = pd.DataFrame({XES_CASE: sub[OID], EID: sub[EID]})
    else:
        raise ValueError(f"不支持的 case notion: {case_notion}")

    merged = work.merge(events, on=EID, how="left")
    merged = merged.sort_values([XES_CASE, TS, EID])

    out = pd.DataFrame({
        XES_CASE: merged[XES_CASE].astype(str),
        XES_ACT: merged[ACT].astype(str),
        XES_TS: pd.to_datetime(merged[TS]),
        XES_RESOURCE: merged["assignee"].astype(str),
        "lifecycle:transition": "complete",
    })
    for a in _EVENT_ATTRS:
        if a in merged.columns:
            out[a] = merged[a].values
    return out.reset_index(drop=True)


def write_xes(df: pd.DataFrame, path: str | Path) -> Path:
    import pm4py

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    log = pm4py.convert_to_event_log(df)
    pm4py.write_xes(log, str(path))
    return path


# --------------------------------------------------------------------------
# 6. OCEL 2.0
# --------------------------------------------------------------------------

def build_ocel(events: pd.DataFrame, objects: pd.DataFrame,
               relations: pd.DataFrame, o2o: pd.DataFrame | None = None):
    """组装 pm4py OCEL 对象。"""
    from pm4py.objects.ocel.obj import OCEL

    ev = events[[EID, ACT, TS, OTYPE] + [c for c in _EVENT_ATTRS if c in events.columns]].copy()
    ob = objects.copy()
    rel = relations[[EID, OID, OTYPE, QUAL]].copy()
    return OCEL(events=ev, objects=ob, relations=rel, o2o=o2o)


def write_ocel(ocel, path: str | Path) -> Path:
    """落盘 OCEL。

    .jsonocel 走 exporter 的 OCEL20_STANDARD 变体:它产出 ocel-standard.org
    的 events/objects 数组格式,与 pm4py 的 ocel20_standard 读取变体对称
    (pm4py 默认的 write_ocel 写的是另一种 ocel:* 字典格式,自家读不回来)。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".jsonocel":
        from pm4py.objects.ocel.exporter.jsonocel import exporter as jsonocel_exporter

        jsonocel_exporter.apply(
            ocel, str(path), variant=jsonocel_exporter.Variants.OCEL20_STANDARD
        )
    else:
        import pm4py

        pm4py.write_ocel(ocel, str(path))
    return path


# --------------------------------------------------------------------------
# 7. 编排 + 质量门禁
# --------------------------------------------------------------------------

@dataclass
class ConversionResult:
    """转换产物。

    保持 dataclass:四个字段都是 DataFrame,交给 pydantic 只会得到
    「开 arbitrary_types_allowed 然后放弃校验」的结果,没有收益。
    结构化 schema 的校验交给 bpmn_registry 与 llm_context。
    """

    events: pd.DataFrame
    objects: pd.DataFrame
    relations: pd.DataFrame
    o2o: pd.DataFrame
    quality: dict

    def quality_frame(self) -> pd.DataFrame:
        rows = [{"指标": k, "值": v} for k, v in self.quality.items()
                if not isinstance(v, (list, dict))]
        return pd.DataFrame(rows)

    def object_type_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.quality["object_types"])


def quality_report(events: pd.DataFrame, objects: pd.DataFrame,
                   relations: pd.DataFrame) -> dict:
    """转换质量门禁。孤立事件率与平均对象/事件这两个指标最关键。"""
    n_events = int(events[EID].nunique())
    n_rel = int(len(relations))
    linked = relations[EID].nunique()
    isolated = n_events - int(linked)

    ts_unique = int(events[TS].nunique())
    rel_with_obj = relations.merge(objects[[OID, OTYPE]], on=[OID, OTYPE], how="inner")
    dangling = n_rel - int(len(rel_with_obj))

    per_obj = rel_with_obj.groupby([OTYPE, OID])[EID].nunique()
    ot_rows = []
    for otype, grp in per_obj.groupby(level=0):
        objs = objects[objects[OTYPE] == otype]
        ot_rows.append({
            "object_type": otype,
            "num_objects": int(len(objs)),
            "num_events": int(rel_with_obj[rel_with_obj[OTYPE] == otype][EID].nunique()),
            "avg_events_per_object": round(float(grp.mean()), 2),
            "max_events_per_object": int(grp.max()),
        })
    ot_rows.sort(key=lambda r: -r["num_objects"])

    return {
        "num_events": n_events,
        "num_objects": int(len(objects)),
        "num_object_types": int(objects[OTYPE].nunique()),
        "num_relations": n_rel,
        "avg_objects_per_event": round(n_rel / n_events, 2) if n_events else 0,
        "isolated_events": int(isolated),
        "isolated_pct": round(isolated / n_events * 100, 2) if n_events else 0,
        "timestamp_unique": ts_unique,
        "timestamp_unique_pct": round(ts_unique / n_events * 100, 2) if n_events else 0,
        "dangling_relations": int(dangling),
        "object_types": ot_rows,
    }


def convert(tables: SimTables,
            registry: ObjectRegistry = DEFAULT_REGISTRY) -> ConversionResult:
    """完整转换:引擎表 + 业务表 → 事件 / 对象 / 关联 / o2o。"""
    events = extract_events(tables)
    objects = extract_objects(tables, registry)
    relations = extract_relations(events, tables, registry)
    o2o = extract_o2o(tables, registry)

    # 丢弃指向不存在对象的关联(通常是业务库外键缺失或变量值脏)
    before = len(relations)
    relations = relations[relations[OID].isin(set(objects[OID]))].reset_index(drop=True)
    dropped_unknown = before - len(relations)

    # 只保留被事件引用过的对象:没被任何事件碰过的对象不携带流程信息,
    # pm4py 导出时也会丢弃。提前剔除,让质量指标与落盘产物严格一致。
    linked = set(relations[OID])
    unreferenced = objects[~objects[OID].isin(linked)]
    objects = objects[objects[OID].isin(linked)].reset_index(drop=True)
    o2o = o2o[o2o[OID].isin(linked) & o2o[OID2].isin(linked)].reset_index(drop=True)

    quality = quality_report(events, objects, relations)
    quality["dropped_unknown_objects"] = int(dropped_unknown)
    quality["unreferenced_objects"] = int(len(unreferenced))
    quality["unreferenced_by_type"] = {
        k: int(v) for k, v in unreferenced.groupby(OTYPE).size().items()
    }
    return ConversionResult(events=events, objects=objects,
                            relations=relations, o2o=o2o, quality=quality)
