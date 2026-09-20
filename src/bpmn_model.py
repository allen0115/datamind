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

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

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


@dataclass(frozen=True)
class Node:
    """BPMN 流程中的一个节点。"""

    id: str
    name: str
    kind: str = USER_TASK
    role: str = ""                 # 执行角色,模拟器据此挑人
    multi_instance: bool = False   # 是否会签(多实例)
    mi_min: int = 2
    mi_max: int = 3
    reject_prob: float = 0.0       # 驳回/不通过概率(0 表示该节点不产生结果分支)
    duration_mean: float = 30.0    # 处理时长均值(分钟,对数正态)
    duration_sigma: float = 0.7


@dataclass(frozen=True)
class Flow:
    """BPMN 顺序流。"""

    src: str
    tgt: str
    weight: float = 1.0
    when: str = ""      # "" | "approve" | "reject"
    loop: bool = False  # 返工回流边(驳回后回到前面的活动)


@dataclass(frozen=True)
class ProcessDef:
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
        Node("s_req", "采购需求提出", START_EVENT),
        Node("n_submit", "Submit Purchase Requisition", USER_TASK,
             role="采购申请人", duration_mean=25),
        Node("g_need_appr", "是否需审批", EXCLUSIVE_GATEWAY),
        Node("n_mgr_appr", "Approve Requisition by Manager", USER_TASK,
             role="部门经理", multi_instance=True, mi_min=2, mi_max=3,
             reject_prob=0.18, duration_mean=60),
        Node("g_appr_result", "审批结果", EXCLUSIVE_GATEWAY),
        Node("n_revise", "Revise Requisition", USER_TASK,
             role="采购申请人", duration_mean=40),
        Node("n_convert", "Convert to Purchase Order", SERVICE_TASK,
             role="采购专员", duration_mean=10),
        Node("n_cancel", "Cancel Requisition", SERVICE_TASK,
             role="采购专员", duration_mean=15),
        Node("e_req_done", "申请关闭", END_EVENT),
        Node("e_req_drop", "申请作废", END_EVENT),
    ),
    flows=(
        Flow("s_req", "n_submit"),
        Flow("n_submit", "g_need_appr"),
        Flow("g_need_appr", "n_mgr_appr", weight=0.85),
        Flow("g_need_appr", "n_convert", weight=0.15),
        Flow("n_mgr_appr", "g_appr_result"),
        Flow("g_appr_result", "n_convert", weight=0.75),
        Flow("g_appr_result", "n_revise", weight=0.20, when="reject", loop=True),
        Flow("g_appr_result", "n_cancel", weight=0.05, when="reject"),
        Flow("n_revise", "n_submit", loop=True),
        Flow("n_convert", "e_req_done"),
        Flow("n_cancel", "e_req_drop"),
    ),
)

PURCHASE_ORDER = ProcessDef(
    key="purchase_order",
    name="采购订单流程",
    business_object="purchase_order",
    start="s_po",
    nodes=(
        Node("s_po", "订单创建", START_EVENT),
        Node("n_create_po", "Create Purchase Order", SERVICE_TASK,
             role="采购专员", duration_mean=15),
        Node("g_amount", "金额是否超审批阈值", EXCLUSIVE_GATEWAY),
        Node("n_dir_appr", "Approve Purchase Order by Director", USER_TASK,
             role="采购总监", reject_prob=0.15, duration_mean=90),
        Node("g_dir_result", "总监审批结果", EXCLUSIVE_GATEWAY),
        Node("n_revise_po", "Revise Purchase Order", SERVICE_TASK,
             role="采购专员", duration_mean=25),
        Node("n_send_po", "Send Order to Supplier", SERVICE_TASK,
             role="采购专员", duration_mean=5),
        Node("n_cancel_po", "Cancel Purchase Order", SERVICE_TASK,
             role="采购专员", duration_mean=10),
        Node("e_po_done", "订单生效", END_EVENT),
        Node("e_po_drop", "订单取消", END_EVENT),
    ),
    flows=(
        Flow("s_po", "n_create_po"),
        Flow("n_create_po", "g_amount"),
        Flow("g_amount", "n_dir_appr", weight=0.35),
        Flow("g_amount", "n_send_po", weight=0.65),
        Flow("n_dir_appr", "g_dir_result"),
        Flow("g_dir_result", "n_send_po", weight=0.80),
        Flow("g_dir_result", "n_revise_po", weight=0.15, when="reject", loop=True),
        Flow("g_dir_result", "n_cancel_po", weight=0.05, when="reject"),
        Flow("n_revise_po", "n_create_po", loop=True),
        Flow("n_send_po", "e_po_done"),
        Flow("n_cancel_po", "e_po_drop"),
    ),
)

GOODS_RECEIPT = ProcessDef(
    key="goods_receipt",
    name="收货质检流程",
    business_object="goods_receipt",
    start="s_gr",
    nodes=(
        Node("s_gr", "供应商到货", START_EVENT),
        Node("n_post_gr", "Post Goods Receipt", SERVICE_TASK,
             role="仓储专员", duration_mean=12),
        Node("n_inspect", "Quality Inspection", USER_TASK,
             role="质检员", reject_prob=0.12, duration_mean=45),
        Node("g_qc", "质检是否合格", EXCLUSIVE_GATEWAY),
        Node("n_putaway", "Putaway to Warehouse", SERVICE_TASK,
             role="仓储专员", duration_mean=30),
        Node("n_return", "Return to Supplier", SERVICE_TASK,
             role="仓储专员", duration_mean=35),
        Node("e_gr_done", "收货完成", END_EVENT),
    ),
    flows=(
        Flow("s_gr", "n_post_gr"),
        Flow("n_post_gr", "n_inspect"),
        Flow("n_inspect", "g_qc"),
        Flow("g_qc", "n_putaway", weight=0.88),
        Flow("g_qc", "n_return", weight=0.12, when="reject"),
        Flow("n_putaway", "e_gr_done"),
        Flow("n_return", "e_gr_done"),
    ),
)

INVOICE_VERIFICATION = ProcessDef(
    key="invoice_verification",
    name="发票校验流程",
    business_object="invoice",
    start="s_inv",
    nodes=(
        Node("s_inv", "收到供应商发票", START_EVENT),
        Node("n_enter_inv", "Enter Incoming Invoice", USER_TASK,
             role="财务专员", duration_mean=25),
        Node("n_match", "Three-Way Match", SERVICE_TASK,
             role="财务专员", reject_prob=0.18, duration_mean=8),
        Node("g_match", "三单匹配结果", EXCLUSIVE_GATEWAY),
        Node("n_park", "Park Invoice", USER_TASK,
             role="财务专员", duration_mean=20),
        Node("n_handle_exc", "Handle Invoice Exception", USER_TASK,
             role="财务专员", duration_mean=60),
        Node("n_appr_inv", "Approve Invoice", USER_TASK,
             role="应付会计", multi_instance=True, mi_min=2, mi_max=2,
             duration_mean=50),
        Node("n_pay", "Post Payment", SERVICE_TASK, role="出纳", duration_mean=12),
        Node("n_clear", "Clear Invoice", SERVICE_TASK,
             role="应付会计", duration_mean=6),
        Node("e_inv_done", "发票关闭", END_EVENT),
    ),
    flows=(
        Flow("s_inv", "n_enter_inv"),
        Flow("n_enter_inv", "n_match"),
        Flow("n_match", "g_match"),
        Flow("g_match", "n_appr_inv", weight=0.82),
        Flow("g_match", "n_park", weight=0.18, when="reject"),
        Flow("n_park", "n_handle_exc"),
        Flow("n_handle_exc", "n_match", loop=True),
        Flow("n_appr_inv", "n_pay"),
        Flow("n_pay", "n_clear"),
        Flow("n_clear", "e_inv_done"),
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
