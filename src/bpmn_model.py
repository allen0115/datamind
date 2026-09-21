"""BPMN 2.0 采购到付款(P2P)流程模型。

本模块用极简的数据结构描述 BPMN 的三个核心元素,既是模拟器的执行依据,
也可以导出为标准 BPMN 2.0 XML(含 DI 坐标),能用 Camunda Modeler 打开。

  - Node : 活动(userTask / serviceTask)、事件(start / end)、网关(exclusive)
  - Flow : 顺序流。weight 表示 XOR 分支概率,when 表示审批结果条件,
           loop 标记驳回返工的回流边
  - ProcessDef : 一个流程定义,business_object 指明其 business_key 属于哪个对象类型

之所以把"业务对象类型"挂在流程定义上:真实 BPM 引擎只会给流程实例带一个
business_key,它是「流程实例 → 业务对象」的唯一原生线索。
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field

# 节点类型
START_EVENT = "startEvent"
END_EVENT = "endEvent"
USER_TASK = "userTask"
SERVICE_TASK = "serviceTask"
EXCLUSIVE_GATEWAY = "exclusiveGateway"

# 会被转换成事件日志条目的节点类型(网关与开始/结束事件是路由噪声,不入日志)
ACTIVITY_KINDS = (USER_TASK, SERVICE_TASK)

# BPMN 2.0 命名空间
_NS_MODEL = "http://www.omg.org/spec/BPMN/20100524/MODEL"
_NS_BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
_NS_DC = "http://www.omg.org/spec/DD/20100524/DC"
_NS_DI = "http://www.omg.org/spec/DD/20100524/DI"


class Node(BaseModel):
    """BPMN 流程中的一个节点。"""

    id: str
    name: str
    kind: str = USER_TASK
    role: str = ""                       # 执行角色,模拟器据此挑人
    multi_instance: bool = False         # 是否会签(多实例)
    mi_min: int = 2
    mi_max: int = 3
    reject_prob: float = Field(default=0.0, ge=0.0, le=1.0)   # 驳回/不通过概率
    duration_mean: float = 30.0          # 处理时长均值(分钟,对数正态)
    duration_sigma: float = 0.7


class Flow(BaseModel):
    """BPMN 顺序流。"""

    src: str
    tgt: str
    weight: float = Field(default=1.0, gt=0.0)
    when: str = Field(default="", description="| approve | reject")
    loop: bool = False            # 返工回流边(驳回后回到前面的活动)


class ProcessDef(BaseModel):
    """BPMN 流程定义。"""

    key: str
    name: str
    business_object: str           # business_key 对应的对象类型
    start: str
    nodes: tuple[Node, ...]
    flows: tuple[Flow, ...]

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"流程 {self.key} 中不存在节点 {node_id}")

    def out_flows(self, node_id: str) -> list[Flow]:
        return [f for f in self.flows if f.src == node_id]

    def validate(self) -> None:
        """模型自检:端点存在、起点唯一、每个节点可达、网关有多条出路。"""
        ids = {n.id for n in self.nodes}
        for f in self.flows:
            if f.src not in ids or f.tgt not in ids:
                raise ValueError(f"流程 {self.key}: 顺序流 {f.src}->{f.tgt} 端点不存在")
        if self.start not in ids:
            raise ValueError(f"流程 {self.key}: 起点 {self.start} 不存在")
        seen = {self.start}
        stack = [self.start]
        while stack:
            cur = stack.pop()
            for f in self.out_flows(cur):
                if f.tgt not in seen:
                    seen.add(f.tgt)
                    stack.append(f.tgt)
        unreachable = ids - seen
        if unreachable:
            raise ValueError(f"流程 {self.key}: 节点不可达 {sorted(unreachable)}")
        for n in self.nodes:
            if n.kind == EXCLUSIVE_GATEWAY and len(self.out_flows(n.id)) < 2:
                raise ValueError(f"流程 {self.key}: 网关 {n.id} 出路少于 2 条")
            if n.kind == END_EVENT and self.out_flows(n.id):
                raise ValueError(f"流程 {self.key}: 结束事件 {n.id} 不应有出边")


# --------------------------------------------------------------------------
# 模型定义:4 条彼此独立、但共享业务对象的流程
# --------------------------------------------------------------------------

PURCHASE_REQUISITION = ProcessDef(
    key="purchase_requisition",
    name="采购申请流程",
    business_object="purchase_requisition",
    start="s_req",
    nodes=(
        Node(id="s_req", name="采购需求提出", kind=START_EVENT),
        Node(id="n_submit", name="Submit Purchase Requisition", kind=USER_TASK,
             role="采购申请人", duration_mean=25),
        Node(id="g_need_appr", name="是否需审批", kind=EXCLUSIVE_GATEWAY),
        Node(id="n_mgr_appr", name="Approve Requisition by Manager", kind=USER_TASK,
             role="部门经理", multi_instance=True, mi_min=2, mi_max=3,
             reject_prob=0.18, duration_mean=60),
        Node(id="g_appr_result", name="审批结果", kind=EXCLUSIVE_GATEWAY),
        Node(id="n_revise", name="Revise Requisition", kind=USER_TASK,
             role="采购申请人", duration_mean=40),
        Node(id="n_convert", name="Convert to Purchase Order", kind=SERVICE_TASK,
             role="采购专员", duration_mean=10),
        Node(id="n_cancel", name="Cancel Requisition", kind=SERVICE_TASK,
             role="采购专员", duration_mean=15),
        Node(id="e_req_done", name="申请关闭", kind=END_EVENT),
        Node(id="e_req_drop", name="申请作废", kind=END_EVENT),
    ),
    flows=(
        Flow(src="s_req", tgt="n_submit"),
        Flow(src="n_submit", tgt="g_need_appr"),
        Flow(src="g_need_appr", tgt="n_mgr_appr", weight=0.85),
        Flow(src="g_need_appr", tgt="n_convert", weight=0.15),
        Flow(src="n_mgr_appr", tgt="g_appr_result"),
        Flow(src="g_appr_result", tgt="n_convert", weight=0.75),
        Flow(src="g_appr_result", tgt="n_revise", weight=0.20, when="reject", loop=True),
        Flow(src="g_appr_result", tgt="n_cancel", weight=0.05, when="reject"),
        Flow(src="n_revise", tgt="n_submit", loop=True),
        Flow(src="n_convert", tgt="e_req_done"),
        Flow(src="n_cancel", tgt="e_req_drop"),
    ),
)

PURCHASE_ORDER = ProcessDef(
    key="purchase_order",
    name="采购订单流程",
    business_object="purchase_order",
    start="s_po",
    nodes=(
        Node(id="s_po", name="订单创建", kind=START_EVENT),
        Node(id="n_create_po", name="Create Purchase Order", kind=SERVICE_TASK,
             role="采购专员", duration_mean=15),
        Node(id="g_amount", name="金额是否超审批阈值", kind=EXCLUSIVE_GATEWAY),
        Node(id="n_dir_appr", name="Approve Purchase Order by Director", kind=USER_TASK,
             role="采购总监", reject_prob=0.15, duration_mean=90),
        Node(id="g_dir_result", name="总监审批结果", kind=EXCLUSIVE_GATEWAY),
        Node(id="n_revise_po", name="Revise Purchase Order", kind=SERVICE_TASK,
             role="采购专员", duration_mean=25),
        Node(id="n_send_po", name="Send Order to Supplier", kind=SERVICE_TASK,
             role="采购专员", duration_mean=5),
        Node(id="n_cancel_po", name="Cancel Purchase Order", kind=SERVICE_TASK,
             role="采购专员", duration_mean=10),
        Node(id="e_po_done", name="订单生效", kind=END_EVENT),
        Node(id="e_po_drop", name="订单取消", kind=END_EVENT),
    ),
    flows=(
        Flow(src="s_po", tgt="n_create_po"),
        Flow(src="n_create_po", tgt="g_amount"),
        Flow(src="g_amount", tgt="n_dir_appr", weight=0.35),
        Flow(src="g_amount", tgt="n_send_po", weight=0.65),
        Flow(src="n_dir_appr", tgt="g_dir_result"),
        Flow(src="g_dir_result", tgt="n_send_po", weight=0.80),
        Flow(src="g_dir_result", tgt="n_revise_po", weight=0.15, when="reject", loop=True),
        Flow(src="g_dir_result", tgt="n_cancel_po", weight=0.05, when="reject"),
        Flow(src="n_revise_po", tgt="n_create_po", loop=True),
        Flow(src="n_send_po", tgt="e_po_done"),
        Flow(src="n_cancel_po", tgt="e_po_drop"),
    ),
)

GOODS_RECEIPT = ProcessDef(
    key="goods_receipt",
    name="收货质检流程",
    business_object="goods_receipt",
    start="s_gr",
    nodes=(
        Node(id="s_gr", name="供应商到货", kind=START_EVENT),
        Node(id="n_post_gr", name="Post Goods Receipt", kind=SERVICE_TASK,
             role="仓储专员", duration_mean=12),
        Node(id="n_inspect", name="Quality Inspection", kind=USER_TASK,
             role="质检员", reject_prob=0.12, duration_mean=45),
        Node(id="g_qc", name="质检是否合格", kind=EXCLUSIVE_GATEWAY),
        Node(id="n_putaway", name="Putaway to Warehouse", kind=SERVICE_TASK,
             role="仓储专员", duration_mean=30),
        Node(id="n_return", name="Return to Supplier", kind=SERVICE_TASK,
             role="仓储专员", duration_mean=35),
        Node(id="e_gr_done", name="收货完成", kind=END_EVENT),
    ),
    flows=(
        Flow(src="s_gr", tgt="n_post_gr"),
        Flow(src="n_post_gr", tgt="n_inspect"),
        Flow(src="n_inspect", tgt="g_qc"),
        Flow(src="g_qc", tgt="n_putaway", weight=0.88),
        Flow(src="g_qc", tgt="n_return", weight=0.12, when="reject"),
        Flow(src="n_putaway", tgt="e_gr_done"),
        Flow(src="n_return", tgt="e_gr_done"),
    ),
)

INVOICE_VERIFICATION = ProcessDef(
    key="invoice_verification",
    name="发票校验流程",
    business_object="invoice",
    start="s_inv",
    nodes=(
        Node(id="s_inv", name="收到供应商发票", kind=START_EVENT),
        Node(id="n_enter_inv", name="Enter Incoming Invoice", kind=USER_TASK,
             role="财务专员", duration_mean=25),
        Node(id="n_match", name="Three-Way Match", kind=SERVICE_TASK,
             role="财务专员", reject_prob=0.18, duration_mean=8),
        Node(id="g_match", name="三单匹配结果", kind=EXCLUSIVE_GATEWAY),
        Node(id="n_park", name="Park Invoice", kind=USER_TASK,
             role="财务专员", duration_mean=20),
        Node(id="n_handle_exc", name="Handle Invoice Exception", kind=USER_TASK,
             role="财务专员", duration_mean=60),
        Node(id="n_appr_inv", name="Approve Invoice", kind=USER_TASK,
             role="应付会计", multi_instance=True, mi_min=2, mi_max=2,
             duration_mean=50),
        Node(id="n_pay", name="Post Payment", kind=SERVICE_TASK, role="出纳",
             duration_mean=12),
        Node(id="n_clear", name="Clear Invoice", kind=SERVICE_TASK,
             role="应付会计", duration_mean=6),
        Node(id="e_inv_done", name="发票关闭", kind=END_EVENT),
    ),
    flows=(
        Flow(src="s_inv", tgt="n_enter_inv"),
        Flow(src="n_enter_inv", tgt="n_match"),
        Flow(src="n_match", tgt="g_match"),
        Flow(src="g_match", tgt="n_appr_inv", weight=0.82),
        Flow(src="g_match", tgt="n_park", weight=0.18, when="reject"),
        Flow(src="n_park", tgt="n_handle_exc"),
        Flow(src="n_handle_exc", tgt="n_match", loop=True),
        Flow(src="n_appr_inv", tgt="n_pay"),
        Flow(src="n_pay", tgt="n_clear"),
        Flow(src="n_clear", tgt="e_inv_done"),
    ),
)

PROCESSES: tuple[ProcessDef, ...] = (
    PURCHASE_REQUISITION,
    PURCHASE_ORDER,
    GOODS_RECEIPT,
    INVOICE_VERIFICATION,
)

PROCESS_BY_KEY: dict[str, ProcessDef] = {p.key: p for p in PROCESSES}


# --------------------------------------------------------------------------
# BPMN 2.0 XML 导出
# --------------------------------------------------------------------------

# 图形尺寸(px)
_SIZE = {
    USER_TASK: (160, 80),
    SERVICE_TASK: (160, 80),
    START_EVENT: (36, 36),
    END_EVENT: (36, 36),
    EXCLUSIVE_GATEWAY: (50, 50),
}

_TAG_NAME = {
    USER_TASK: "userTask",
    SERVICE_TASK: "serviceTask",
    START_EVENT: "startEvent",
    END_EVENT: "endEvent",
    EXCLUSIVE_GATEWAY: "exclusiveGateway",
}


def _layout(pdef: ProcessDef) -> dict[str, tuple[float, float, float, float]]:
    """按 BFS 层级做一个简单的横向泳道式布局,返回 {node_id: (x, y, w, h)}。"""
    depth: dict[str, int] = {pdef.start: 0}
    order = {n.id: i for i, n in enumerate(pdef.nodes)}
    queue = [pdef.start]
    while queue:
        cur = queue.pop(0)
        for f in pdef.out_flows(cur):
            if f.tgt not in depth:
                depth[f.tgt] = depth[cur] + 1
                queue.append(f.tgt)
    for n in pdef.nodes:
        depth.setdefault(n.id, 0)

    buckets: dict[int, list[str]] = {}
    for nid, d in depth.items():
        buckets.setdefault(d, []).append(nid)
    for ids in buckets.values():
        ids.sort(key=lambda x: order[x])

    boxes: dict[str, tuple[float, float, float, float]] = {}
    for d, ids in buckets.items():
        for k, nid in enumerate(ids):
            w, h = _SIZE[pdef.node(nid).kind]
            x = 60 + d * 230
            y_line = 60 + k * 120
            boxes[nid] = (x, y_line + (120 - h) / 2, w, h)
    return boxes


def _waypoints(src_box, tgt_box, backward: bool, y_max: float) -> list[tuple[float, float]]:
    sx, sy, sw, sh = src_box
    tx, ty, tw, th = tgt_box
    if backward:
        # 返工回流边:绕到底部走线,避免与正向边重叠
        yb = y_max + 60
        return [
            (sx + sw / 2, sy + sh), (sx + sw / 2, yb),
            (tx + tw / 2, yb), (tx + tw / 2, ty + th),
        ]
    ys, yt = sy + sh / 2, ty + th / 2
    if abs(ys - yt) < 1:
        return [(sx + sw, ys), (tx, yt)]
    mid = sx + sw + 30
    return [(sx + sw, ys), (mid, ys), (mid, yt), (tx, yt)]


def to_bpmn_xml(processes: tuple[ProcessDef, ...] = PROCESSES) -> str:
    """把流程模型序列化为 BPMN 2.0 XML(含 BPMNDiagram 坐标)。"""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<definitions xmlns="{_NS_MODEL}" xmlns:bpmndi="{_NS_BPMNDI}"'
        f' xmlns:dc="{_NS_DC}" xmlns:di="{_NS_DI}"'
        ' id="Definitions_P2P" targetNamespace="http://bpmn.io/schema/bpmn">',
    ]

    planes: list[str] = []

    for pdef in processes:
        parts.append(f'  <process id="{pdef.key}" name="{escape(pdef.name)}" isExecutable="true">')
        for n in pdef.nodes:
            tag = _TAG_NAME[n.kind]
            label = f' name="{escape(n.name)}"' if n.name else ""
            attrs = ""
            if n.multi_instance:
                attrs = (f'\n      <multiInstanceLoopCharacteristics isSequential="false">'
                         f'\n        <loopCardinality>{n.mi_max}</loopCardinality>'
                         f'\n      </multiInstanceLoopCharacteristics>')
            if n.kind == EXCLUSIVE_GATEWAY:
                parts.append(f'    <{tag} id="{n.id}"{label} />')
            else:
                parts.append(f'    <{tag} id="{n.id}"{label}>{attrs}\n    </{tag}>')
        for f in pdef.flows:
            label = f' name="{escape(f.when or "flow")}"' if f.when else ""
            parts.append(f'    <sequenceFlow id="flow_{f.src}_{f.tgt}"'
                         f' sourceRef="{f.src}" targetRef="{f.tgt}"{label} />')
        parts.append("  </process>")

        # ---- DI ----
        boxes = _layout(pdef)
        depth_rank = {nid: b[0] for nid, b in boxes.items()}
        y_max = max(b[1] + b[3] for b in boxes.values())
        planes.append(f'    <bpmndi:BPMNPlane id="Plane_{pdef.key}" bpmnElement="{pdef.key}">')
        for nid, (x, y, w, h) in sorted(boxes.items()):
            planes.append(
                f'      <bpmndi:BPMNShape id="Shape_{nid}" bpmnElement="{nid}">'
                f'<dc:Bounds x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" />'
                f'</bpmndi:BPMNShape>'
            )
        for f in pdef.flows:
            backward = depth_rank[f.tgt] < depth_rank[f.src]
            pts = _waypoints(boxes[f.src], boxes[f.tgt], backward, y_max)
            wp = "".join(f'<di:waypoint x="{x:.0f}" y="{y:.0f}" />' for x, y in pts)
            fid = f"flow_{f.src}_{f.tgt}"
            planes.append(f'      <bpmndi:BPMNEdge id="Edge_{fid}" bpmnElement="{fid}">{wp}</bpmndi:BPMNEdge>')
        planes.append("    </bpmndi:BPMNPlane>")

    parts.append(f'  <bpmndi:BPMNDiagram id="Diagram_P2P">')
    parts.extend(planes)
    parts.append("  </bpmndi:BPMNDiagram>")
    parts.append("</definitions>")
    return "\n".join(parts)


def export_bpmn_xml(path: str | Path,
                    processes: tuple[ProcessDef, ...] = PROCESSES) -> Path:
    for p in processes:
        p.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_bpmn_xml(processes), encoding="utf-8")
    return path
