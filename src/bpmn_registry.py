"""对象注册表:BPM 执行数据 → OCEL 的映射规则(转换的唯一真源)。

上一轮结论里说过:转换的本质不是格式问题,而是「对象模型建模」问题。
这个模块把建模结果显式固化下来,转换器只按表驱动执行:

  ObjectSpec     业务实体 → 对象类型(来自哪张表、主键是什么、带哪些属性)
  O2ORule        对象与对象之间的结构关系(行项目属于抬头、发票对应订单……)
  ExpansionRule  事件命中某些活动时,把父对象展开成子对象
  variable_map   流程变量 → 对象类型(引擎里对象 ID 的主要藏身处)

改业务模型时只改这里,不用动转换器。
结构一律用 pydantic 声明,字段缺失或类型写错在定义时就暴露。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ObjectSpec(BaseModel):
    """一个业务实体类型。"""

    name: str                        # ocel:type
    table: str                       # 业务表名
    id_cols: tuple[str, ...]         # 主键列(多列=复合主键)
    attrs: tuple[str, ...] = ()      # 作为对象属性带进 OCEL
    label: str = ""                  # 中文说明


class O2ORule(BaseModel):
    """对象—对象关系规则:子表的若干列拼成父对象的 ID。"""

    child_type: str
    parent_type: str
    join_cols: tuple[str, ...]       # 子表中构成父对象 ID 的列
    table: str
    qualifier: str = "composes"


class ExpansionRule(BaseModel):
    """事件展开规则:事件已关联 parent 且活动命中时,再展开到 child。

    join_cols 是「子表里构成父对象 ID 的列」,与 O2ORule 同义。
    """

    parent_type: str
    child_type: str
    table: str
    join_cols: tuple[str, ...]
    activities: tuple[str, ...]
    qualifier: str = "contains"


class ObjectRegistry(BaseModel):
    objects: tuple[ObjectSpec, ...]
    o2o_rules: tuple[O2ORule, ...]
    expansion_rules: tuple[ExpansionRule, ...]
    variable_map: dict[str, str] = Field(default_factory=dict)
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
        ObjectSpec(name="purchase_requisition", table="purchase_requisition",
                   id_cols=("pr_no",),
                   attrs=("material_code", "qty", "amount", "requester"), label="采购申请"),
        ObjectSpec(name="purchase_order", table="purchase_order", id_cols=("po_no",),
                   attrs=("supplier_code", "amount", "status"), label="采购订单"),
        ObjectSpec(name="po_item", table="po_item", id_cols=("po_no", "item_no"),
                   attrs=("material_code", "qty", "unit_price"), label="订单行项目"),
        ObjectSpec(name="goods_receipt", table="goods_receipt", id_cols=("gr_no",),
                   attrs=("po_no", "received_qty"), label="收货单"),
        ObjectSpec(name="gr_item", table="gr_item", id_cols=("gr_no", "item_no"),
                   attrs=("material_code", "qty"), label="收货行项目"),
        ObjectSpec(name="invoice", table="invoice", id_cols=("invoice_no",),
                   attrs=("po_no", "amount", "status"), label="供应商发票"),
        ObjectSpec(name="material", table="material", id_cols=("material_code",),
                   attrs=("material_name", "category"), label="物料"),
        ObjectSpec(name="supplier", table="supplier", id_cols=("supplier_code",),
                   attrs=("supplier_name", "region"), label="供应商"),
        ObjectSpec(name="employee", table="employee", id_cols=("employee_id",),
                   attrs=("name", "role", "department"), label="员工/资源"),
    ),
    o2o_rules=(
        O2ORule(child_type="po_item", parent_type="purchase_order",
                join_cols=("po_no",), table="po_item", qualifier="composes"),
        O2ORule(child_type="gr_item", parent_type="goods_receipt",
                join_cols=("gr_no",), table="gr_item", qualifier="composes"),
        O2ORule(child_type="purchase_order", parent_type="purchase_requisition",
                join_cols=("pr_no",), table="purchase_order", qualifier="derived_from"),
        O2ORule(child_type="goods_receipt", parent_type="purchase_order",
                join_cols=("po_no",), table="goods_receipt", qualifier="receives"),
        O2ORule(child_type="invoice", parent_type="purchase_order",
                join_cols=("po_no",), table="invoice", qualifier="billed_for"),
    ),
    expansion_rules=(
        # 建订单/改订单:一个订单抬头事件同时作用于所有行项目
        ExpansionRule(parent_type="purchase_order", child_type="po_item", table="po_item",
                      join_cols=("po_no",),
                      activities=("Create Purchase Order", "Revise Purchase Order"),
                      qualifier="contains"),
        # 收货过账:一张收货单同时过账多行
        ExpansionRule(parent_type="goods_receipt", child_type="gr_item", table="gr_item",
                      join_cols=("gr_no",), activities=("Post Goods Receipt",),
                      qualifier="contains"),
        # 订单行 / 收货行都会落到具体物料上
        ExpansionRule(parent_type="po_item", child_type="material", table="po_item",
                      join_cols=("po_no", "item_no"),
                      activities=("Create Purchase Order", "Revise Purchase Order"),
                      qualifier="refers_to"),
        ExpansionRule(parent_type="gr_item", child_type="material", table="gr_item",
                      join_cols=("gr_no", "item_no"), activities=("Post Goods Receipt",),
                      qualifier="refers_to"),
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
