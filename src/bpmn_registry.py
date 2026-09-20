"""对象注册表:BPM 执行数据 → OCEL 的映射规则(转换的唯一真源)。

上一轮结论里说过:转换的本质不是格式问题,而是「对象模型建模」问题。
这个模块把建模结果显式固化下来,转换器只按表驱动执行:

  ObjectSpec     业务实体 → 对象类型(来自哪张表、主键是什么、带哪些属性)
  O2ORule        对象与对象之间的结构关系(行项目属于抬头、发票对应订单……)
  ExpansionRule  事件命中某些活动时,把父对象展开成子对象
  variable_map   流程变量 → 对象类型(引擎里对象 ID 的主要藏身处)

改业务模型时只改这里,不用动转换器。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ObjectSpec:
    """一个业务实体类型。"""

    name: str                        # ocel:type
    table: str                       # 业务表名
    id_cols: tuple[str, ...]         # 主键列(多列=复合主键)
    attrs: tuple[str, ...] = ()      # 作为对象属性带进 OCEL
    label: str = ""                  # 中文说明


@dataclass(frozen=True)
class O2ORule:
    """对象—对象关系规则:子表的若干列拼成父对象的 ID。"""

    child_type: str
    parent_type: str
    join_cols: tuple[str, ...]       # 子表中构成父对象 ID 的列
    table: str
    qualifier: str = "composes"


@dataclass(frozen=True)
class ExpansionRule:
    """事件展开规则:事件已关联 parent 且活动命中时,再展开到 child。

    join_cols 是「子表里构成父对象 ID 的列」,与 O2ORule 同义。
    """

    parent_type: str
    child_type: str
    table: str
    join_cols: tuple[str, ...]
    activities: tuple[str, ...]
    qualifier: str = "contains"


@dataclass(frozen=True)
class ObjectRegistry:
    objects: tuple[ObjectSpec, ...]
    o2o_rules: tuple[O2ORule, ...]
    expansion_rules: tuple[ExpansionRule, ...]
    variable_map: dict[str, str] = field(default_factory=dict)
    # 资源维度:任务的 assignee 也当对象,可挖「谁/哪个角色卡住流程」
    resource_object: str | None = "employee"

    def spec(self, name: str) -> ObjectSpec:
        for s in self.objects:
            if s.name == name:
                return s
        raise KeyError(f"未注册的对象类型: {name}")

    @property
    def type_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.objects)


DEFAULT_REGISTRY = ObjectRegistry(
    objects=(
        ObjectSpec("purchase_requisition", "purchase_requisition", ("pr_no",),
                   attrs=("material_code", "qty", "amount", "requester"), label="采购申请"),
        ObjectSpec("purchase_order", "purchase_order", ("po_no",),
                   attrs=("supplier_code", "amount", "status"), label="采购订单"),
        ObjectSpec("po_item", "po_item", ("po_no", "item_no"),
                   attrs=("material_code", "qty", "unit_price"), label="订单行项目"),
        ObjectSpec("goods_receipt", "goods_receipt", ("gr_no",),
                   attrs=("po_no", "received_qty"), label="收货单"),
        ObjectSpec("gr_item", "gr_item", ("gr_no", "item_no"),
                   attrs=("material_code", "qty"), label="收货行项目"),
        ObjectSpec("invoice", "invoice", ("invoice_no",),
                   attrs=("po_no", "amount", "status"), label="供应商发票"),
        ObjectSpec("material", "material", ("material_code",),
                   attrs=("material_name", "category"), label="物料"),
        ObjectSpec("supplier", "supplier", ("supplier_code",),
                   attrs=("supplier_name", "region"), label="供应商"),
        ObjectSpec("employee", "employee", ("employee_id",),
                   attrs=("name", "role", "department"), label="员工/资源"),
    ),
    o2o_rules=(
        O2ORule("po_item", "purchase_order", ("po_no",), "po_item", "composes"),
        O2ORule("gr_item", "goods_receipt", ("gr_no",), "gr_item", "composes"),
        O2ORule("purchase_order", "purchase_requisition", ("pr_no",),
                "purchase_order", "derived_from"),
        O2ORule("goods_receipt", "purchase_order", ("po_no",),
                "goods_receipt", "receives"),
        O2ORule("invoice", "purchase_order", ("po_no",), "invoice", "billed_for"),
    ),
    expansion_rules=(
        # 建订单/改订单:一个订单抬头事件同时作用于所有行项目
        ExpansionRule("purchase_order", "po_item", "po_item", ("po_no",),
                      ("Create Purchase Order", "Revise Purchase Order"), "contains"),
        # 收货过账:一张收货单同时过账多行
        ExpansionRule("goods_receipt", "gr_item", "gr_item", ("gr_no",),
                      ("Post Goods Receipt",), "contains"),
        # 订单行 / 收货行都会落到具体物料上
        ExpansionRule("po_item", "material", "po_item", ("po_no", "item_no"),
                      ("Create Purchase Order", "Revise Purchase Order"), "refers_to"),
        ExpansionRule("gr_item", "material", "gr_item", ("gr_no", "item_no"),
                      ("Post Goods Receipt",), "refers_to"),
    ),
    variable_map={
        "prNo": "purchase_requisition",
        "poNo": "purchase_order",
        "grNo": "goods_receipt",
        "invoiceNo": "invoice",
        "materialCode": "material",
        "supplierCode": "supplier",
    },
    resource_object="employee",
)
