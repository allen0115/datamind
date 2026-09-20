"""BPM 引擎执行模拟器。

产出的是「BPM 系统里真实存在的那几张表」,而不是直接产出事件日志:

  引擎表(对应 Activiti/Flowable/Camunda 的 ACT_HI_* 历史表)
    bpm_process_instance  流程实例:instance_id / business_key / 时间 / 状态
    bpm_task              任务实例:一次节点执行,多实例会签会产生多行
    bpm_variable          流程变量:业务单据号在这里,是对象 ID 的主要藏身处

  业务表(对应 ERP / 业务库)
    purchase_requisition / purchase_order / po_item / goods_receipt
    gr_item / invoice / material / supplier / employee

模拟器刻意保留真实引擎的"脏"特征:会签多实例一人一行、驳回会产生返工回流、
时间戳按秒推进、business_key 只带一个主单据号。转换器的职责就是把这些
还原成干净的事件与对象关联。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .bpmn_model import (
    END_EVENT,
    EXCLUSIVE_GATEWAY,
    GOODS_RECEIPT,
    INVOICE_VERIFICATION,
    PURCHASE_ORDER,
    PURCHASE_REQUISITION,
    SERVICE_TASK,
    START_EVENT,
    USER_TASK,
    Node,
    ProcessDef,
)

# 角色 → 人数(决定会签能挑出几个不同的人)
_ROLE_POOL: dict[str, int] = {
    "采购申请人": 40,
    "部门经理": 8,
    "采购总监": 3,
    "采购专员": 10,
    "仓储专员": 8,
    "质检员": 5,
    "财务专员": 8,
    "应付会计": 5,
    "出纳": 3,
}

_DEPT_BY_ROLE: dict[str, str] = {
    "采购申请人": "采购部",
    "部门经理": "采购部",
    "采购总监": "采购中心",
    "采购专员": "采购部",
    "仓储专员": "供应链部",
    "质检员": "供应链部",
    "财务专员": "财务部",
    "应付会计": "财务部",
    "出纳": "财务部",
}

_CATEGORIES = ("办公用品", "IT设备", "原材料", "包装材料", "劳保用品")
_REGIONS = ("华东", "华南", "华北", "西南", "海外")


@dataclass
class SimConfig:
    n_requisitions: int = 200        # 采购申请条数(整条 P2P 链条的起点)
    seed: int = 42
    start_at: str = "2024-01-01"
    chain_gap_hours: tuple[float, float] = (1.0, 8.0)   # 相邻链条的起始间隔
    approval_threshold: float = 50000.0                  # 超过此金额走总监审批
    max_rework: int = 3              # 单个节点最多返工次数(防死循环)
    n_materials: int = 60
    n_suppliers: int = 12


@dataclass
class SimTables:
    """模拟出来的源系统表。"""

    process_instance: pd.DataFrame   # bpm_process_instance
    task: pd.DataFrame               # bpm_task
    variable: pd.DataFrame           # bpm_variable
    business: dict[str, pd.DataFrame]  # 业务表

    def all_tables(self) -> dict[str, pd.DataFrame]:
        out = {
            "bpm_process_instance": self.process_instance,
            "bpm_task": self.task,
            "bpm_variable": self.variable,
        }
        out.update({f"biz_{k}": v for k, v in self.business.items()})
        return out


# --------------------------------------------------------------------------
# 基础主数据
# --------------------------------------------------------------------------

def _gen_employees(rng: random.Random) -> list[dict]:
    rows, idx = [], 0
    for role, cnt in _ROLE_POOL.items():
        for _ in range(cnt):
            idx += 1
            rows.append({
                "employee_id": f"EMP-{idx:04d}",
                "name": f"Employee {idx:03d}",
                "role": role,
                "department": _DEPT_BY_ROLE[role],
            })
    return rows


def _gen_materials(rng: random.Random, n: int) -> list[dict]:
    return [{
        "material_code": f"MAT-{i + 1:05d}",
        "material_name": f"Material {i + 1:03d}",
        "category": rng.choice(_CATEGORIES),
    } for i in range(n)]


def _gen_suppliers(rng: random.Random, n: int) -> list[dict]:
    return [{
        "supplier_code": f"SUP-{i + 1:03d}",
        "supplier_name": f"Supplier {i + 1:02d}",
        "region": rng.choice(_REGIONS),
    } for i in range(n)]


# --------------------------------------------------------------------------
# BPMN 执行引擎
# --------------------------------------------------------------------------

def _duration(rng: random.Random, node: Node) -> timedelta:
    """处理时长:对数正态分布,并做上下限截断,避免长尾失控。"""
    minutes = rng.lognormvariate(math.log(max(node.duration_mean, 1.0)), node.duration_sigma)
    minutes = min(max(minutes, 1.0), node.duration_mean * 8)
    return timedelta(seconds=int(minutes * 60))


def _pick_flow(pdef: ProcessDef, node_id: str, outcome: str,
               visit_count: dict[str, int], ctx: dict, rng: random.Random) -> str:
    """按 XOR 网关的权重选出下一个节点。"""
    out = pdef.out_flows(node_id)
    if not out:
        raise ValueError(f"流程 {pdef.key}: 节点 {node_id} 没有出边")

    # 审批结果决定走"通过"分支还是"驳回"分支
    if outcome == "reject":
        cands = [f for f in out if f.when == "reject"]
    else:
        cands = [f for f in out if f.when in ("", "approve")]
    if not cands:
        cands = out

    # 返工次数用尽:强制不再走回流边,保证流程一定能收敛
    max_rework = ctx["max_rework"]
    if visit_count.get(node_id, 0) >= max_rework:
        non_loop = [f for f in cands if not f.loop]
        if non_loop:
            cands = non_loop

    bias = ctx.get("gate_bias", {}).get(node_id)
    weights = [bias.get(f.tgt, f.weight) if bias else f.weight for f in cands]
    return rng.choices(cands, weights=weights, k=1)[0].tgt


def _run_instance(pdef: ProcessDef, rng: random.Random, start_time: datetime,
                  instance_id: str, ctx: dict) -> tuple[list[dict], list[str], datetime]:
    """执行一个流程实例,返回 (任务行, 访问过的节点 id, 结束时间)。"""
    tasks: list[dict] = []
    visited: list[str] = []
    visit_count: dict[str, int] = {}
    clock = start_time
    cur = pdef.start
    outcome = "approve"
    act_seq = 0

    def pick(role: str, n: int) -> list[dict]:
        if role in ctx.get("fixed_roles", {}) and n == 1:
            return [ctx["fixed_roles"][role]]
        pool = ctx["by_role"][role]
        if n <= 1:
            return [rng.choice(pool)]
        return rng.sample(pool, min(n, len(pool)))

    while True:
        node = pdef.node(cur)
        if node.kind == END_EVENT:
            break
        if node.kind in (START_EVENT, EXCLUSIVE_GATEWAY):
            cur = _pick_flow(pdef, cur, outcome, visit_count, ctx, rng)
            continue

        visit_count[cur] = visit_count.get(cur, 0) + 1
        act_seq += 1
        act_inst_id = f"{instance_id}|A{act_seq:02d}"

        # 驳回判定:返工次数用尽后强制通过,避免死循环
        if node.reject_prob > 0 and visit_count[cur] < ctx["max_rework"]:
            outcome = "reject" if rng.random() < node.reject_prob else "approve"
        else:
            outcome = "approve"

        base = {
            "instance_id": instance_id,
            "process_key": pdef.key,
            "act_inst_id": act_inst_id,
            "node_id": node.id,
            "node_name": node.name,
            "task_type": USER_TASK if node.kind == USER_TASK else SERVICE_TASK,
            "seq": act_seq,
        }

        if node.multi_instance:
            # 会签:同一个活动实例下 N 个审批人各产生一条任务,起止时间错开
            assignees = pick(node.role, rng.randint(node.mi_min, node.mi_max))
            ends: list[datetime] = []
            for i, emp in enumerate(assignees):
                wait = timedelta(minutes=rng.uniform(5, 120) if i == 0 else rng.uniform(2, 60))
                st = clock + wait
                en = st + _duration(rng, node)
                ends.append(en)
                tasks.append({
                    **base,
                    "task_id": ctx["next_tid"](),
                    "assignee": emp["employee_id"],
                    "assignee_role": emp["role"],
                    "start_time": st,
                    "end_time": en,
                    "outcome": outcome,
                })
            clock = max(ends)
        else:
            emp = pick(node.role, 1)[0]
            st = clock + timedelta(minutes=rng.uniform(5, 240))
            en = st + _duration(rng, node)
            clock = en
            tasks.append({
                **base,
                "task_id": ctx["next_tid"](),
                "assignee": emp["employee_id"],
                "assignee_role": emp["role"],
                "start_time": st,
                "end_time": en,
                "outcome": outcome,
            })

        visited.append(cur)
        cur = _pick_flow(pdef, cur, outcome, visit_count, ctx, rng)

    return tasks, visited, clock


# --------------------------------------------------------------------------
# 业务驱动:申请 → 订单 → 收货 → 发票
# --------------------------------------------------------------------------

def simulate(cfg: SimConfig | None = None) -> SimTables:
    if cfg is None:
        cfg = SimConfig()
    rng = random.Random(cfg.seed)

    employees = _gen_employees(rng)
    by_role: dict[str, list[dict]] = {}
    for e in employees:
        by_role.setdefault(e["role"], []).append(e)
    materials = _gen_materials(rng, cfg.n_materials)
    suppliers = _gen_suppliers(rng, cfg.n_suppliers)

    counters = {"inst": 0, "task": 0}

    def next_iid() -> str:
        counters["inst"] += 1
        return f"PI-{counters['inst']:06d}"

    def next_tid() -> str:
        counters["task"] += 1
        return f"TK-{counters['task']:07d}"

    ctx: dict = {
        "by_role": by_role,
        "max_rework": cfg.max_rework,
        "next_tid": next_tid,
        "gate_bias": {},
        "fixed_roles": {},
    }

    pi_rows: list[dict] = []
    task_rows: list[dict] = []
    var_rows: list[dict] = []
    pr_rows: list[dict] = []
    po_rows: list[dict] = []
    poi_rows: list[dict] = []
    gr_rows: list[dict] = []
    gri_rows: list[dict] = []
    inv_rows: list[dict] = []

    def add_vars(instance_id: str, **kv) -> None:
        for k, v in kv.items():
            var_rows.append({"instance_id": instance_id, "name": k, "value": str(v)})

    def add_instance(iid: str, pdef: ProcessDef, business_key: str,
                     start: datetime, end: datetime, apply_user: str,
                     amount: float, status: str) -> None:
        pi_rows.append({
            "instance_id": iid,
            "process_key": pdef.key,
            "business_key": business_key,
            "status": status,
            "apply_user": apply_user,
            "amount": round(amount, 2),
            "start_time": start,
            "end_time": end,
        })

    po_seq = gr_seq = inv_seq = 0
    cursor = datetime.fromisoformat(cfg.start_at)

    for i in range(cfg.n_requisitions):
        cursor += timedelta(hours=rng.uniform(*cfg.chain_gap_hours))
        chain_start = cursor

        # ---- 1. 采购申请 ----
        pr_no = f"PR-{i + 1:06d}"
        requester = rng.choice(by_role["采购申请人"])
        mat = rng.choice(materials)
        qty = rng.randint(1, 200)
        pr_amount = round(qty * rng.uniform(20, 800), 2)
        pr_rows.append({
            "pr_no": pr_no, "material_code": mat["material_code"], "qty": qty,
            "amount": pr_amount, "requester": requester["employee_id"],
            "created_at": chain_start,
        })

        ctx["fixed_roles"] = {"采购申请人": requester}
        iid = next_iid()
        tasks, visited, end = _run_instance(PURCHASE_REQUISITION, rng, chain_start, iid, ctx)
        task_rows.extend(tasks)
        add_vars(iid, prNo=pr_no, materialCode=mat["material_code"], amount=pr_amount)
        add_instance(iid, PURCHASE_REQUISITION, pr_no, chain_start, end,
                     requester["employee_id"], pr_amount, "completed")

        if "n_convert" not in visited:
            continue

        # ---- 2. 采购订单(一份申请偶尔拆成两张订单) ----
        n_po = 2 if rng.random() < 0.12 else 1
        for _ in range(n_po):
            po_seq += 1
            po_no = f"PO-{po_seq:06d}"
            supplier = rng.choice(suppliers)
            n_items = rng.randint(1, 4)
            items: list[dict] = []
            for j in range(n_items):
                m = mat if j == 0 else rng.choice(materials)
                items.append({
                    "po_no": po_no,
                    "item_no": f"{10 * (j + 1):03d}",
                    "material_code": m["material_code"],
                    "qty": rng.randint(1, 100),
                    "unit_price": round(rng.uniform(15, 1500), 2),
                })
            poi_rows.extend(items)
            po_amount = round(sum(it["qty"] * it["unit_price"] for it in items), 2)

            ctx["gate_bias"] = {
                # 金额越大越可能触发总监审批 —— 让金额与路径产生真实相关性
                "g_amount": {"n_dir_appr": 0.9 if po_amount > cfg.approval_threshold else 0.05,
                             "n_send_po": 0.1 if po_amount > cfg.approval_threshold else 0.95}
            }
            ctx["fixed_roles"] = {}
            po_start = end + timedelta(hours=rng.uniform(0.5, 24))
            iid = next_iid()
            tasks, visited, po_end = _run_instance(PURCHASE_ORDER, rng, po_start, iid, ctx)
            task_rows.extend(tasks)
            add_vars(iid, prNo=pr_no, poNo=po_no, amount=po_amount,
                     supplierCode=supplier["supplier_code"])
            po_sent = "n_send_po" in visited
            po_rows.append({
                "po_no": po_no, "pr_no": pr_no, "supplier_code": supplier["supplier_code"],
                "amount": po_amount, "status": "SENT" if po_sent else "CANCELLED",
                "created_at": po_start,
            })
            add_instance(iid, PURCHASE_ORDER, po_no, po_start, po_end,
                         tasks[0]["assignee"], po_amount, "completed" if po_sent else "cancelled")

            if not po_sent:
                continue

            # ---- 3. 收货(可能分批发货) ----
            n_gr = 2 if rng.random() < 0.30 else 1
            gr_time = po_end
            gr_returned = False
            for _ in range(n_gr):
                gr_seq += 1
                gr_no = f"GR-{gr_seq:06d}"
                picked = rng.sample(items, k=rng.randint(1, len(items)))
                gr_items = [{
                    "gr_no": gr_no,
                    "item_no": f"{10 * (idx + 1):03d}",
                    "material_code": it["material_code"],
                    "qty": max(1, int(it["qty"] * rng.uniform(0.3, 1.0))),
                } for idx, it in enumerate(picked)]
                gri_rows.extend(gr_items)
                received_qty = sum(g["qty"] for g in gr_items)

                ctx["gate_bias"] = {}
                gr_start = gr_time + timedelta(hours=rng.uniform(2, 72))
                iid = next_iid()
                tasks, visited, gr_end = _run_instance(GOODS_RECEIPT, rng, gr_start, iid, ctx)
                task_rows.extend(tasks)
                add_vars(iid, poNo=po_no, grNo=gr_no,
                         materialCode=gr_items[0]["material_code"])
                gr_rows.append({
                    "gr_no": gr_no, "po_no": po_no, "received_qty": received_qty,
                    "received_at": gr_start,
                })
                add_instance(iid, GOODS_RECEIPT, gr_no, gr_start, gr_end,
                             tasks[0]["assignee"], 0.0, "completed")
                gr_time = gr_end
                gr_returned = gr_returned or ("n_return" in visited)

            # ---- 4. 发票校验(收货有退货 → 三单匹配更容易失败) ----
            n_inv = 2 if rng.random() < 0.15 else 1
            inv_time = gr_time
            for k in range(n_inv):
                inv_seq += 1
                invoice_no = f"INV-{inv_seq:06d}"
                share = 1.0 if n_inv == 1 else (0.6 if k == 0 else 0.4)
                inv_amount = round(po_amount * share * rng.uniform(0.98, 1.02), 2)

                ctx["gate_bias"] = {
                    "g_match": {"n_park": 0.75 if gr_returned else 0.15,
                                "n_appr_inv": 0.25 if gr_returned else 0.85}
                }
                inv_start = inv_time + timedelta(hours=rng.uniform(4, 120))
                iid = next_iid()
                tasks, visited, inv_end = _run_instance(INVOICE_VERIFICATION, rng, inv_start, iid, ctx)
                task_rows.extend(tasks)
                add_vars(iid, poNo=po_no, invoiceNo=invoice_no, amount=inv_amount)
                paid = "n_clear" in visited
                inv_rows.append({
                    "invoice_no": invoice_no, "po_no": po_no, "amount": inv_amount,
                    "status": "PAID" if paid else "PARKED", "received_at": inv_start,
                })
                add_instance(iid, INVOICE_VERIFICATION, invoice_no, inv_start, inv_end,
                             tasks[0]["assignee"], inv_amount, "completed" if paid else "parked")
                inv_time = inv_end

    def _df(rows: list[dict], time_cols: tuple[str, ...] = ()) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        for c in time_cols:
            if c in df.columns:
                df[c] = pd.to_datetime(df[c])
        return df

    task_df = _df(task_rows, ("start_time", "end_time"))
    task_df = task_df.sort_values(["instance_id", "seq", "start_time"]).reset_index(drop=True)
    if not task_df.empty:
        task_df["duration_ms"] = (
            (task_df["end_time"] - task_df["start_time"]).dt.total_seconds() * 1000
        ).astype(int)

    return SimTables(
        process_instance=_df(pi_rows, ("start_time", "end_time")),
        task=task_df,
        variable=_df(var_rows),
        business={
            "purchase_requisition": _df(pr_rows, ("created_at",)),
            "purchase_order": _df(po_rows, ("created_at",)),
            "po_item": _df(poi_rows),
            "goods_receipt": _df(gr_rows, ("received_at",)),
            "gr_item": _df(gri_rows),
            "invoice": _df(inv_rows, ("received_at",)),
            "material": _df(materials),
            "supplier": _df(suppliers),
            "employee": _df(employees),
        },
    )
