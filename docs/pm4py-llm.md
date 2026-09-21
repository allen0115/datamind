## 一句话原则

**LLM 负责语义与假设，pm4py 负责计算与判定；两者只通过结构化契约交接，数字永远不由 LLM 生成。**

这句不是保守，而是由两边的能力边界决定的。

---

## 一、为什么必须结合：各自"做不到"什么

| | pm4py 能做到 | pm4py 做不到 |
|---|---|---|
| 计算 | 精确计数、频次、时延分布 | —— |
| 建模 | 从日志推导模型，给出 fitness 等可复现指标 | —— |
| 判定 | 合规判定、显著性检验、可审计 | —— |
| 语义 | —— | 读不懂 `"审批"`/`"Approve"`/`"SP01"` 是同一件事 |
| 结构化 | 必须吃 `(case, activity, timestamp)` 三元组 | 无法从工单描述、BPMN XML、需求文档里抽日志 |
| 意图 | —— | 无法回答"最近为什么慢了"这种开放问题 |
| 解释 | 只给数字和图 | 无法把 DFG 翻译成业务语言 |

反过来，LLM 的能力边界同样明确：

- **不能数数**：让它从几千行日志里数"驳回多少次"，必然错。注意力会被长上下文稀释，且它是概率采样不是计数。
- **不能替代形式化判定**：合规、显著性这类需要可复现结论的事，LLM 只能给"看起来合理"的答案。
- **不能进权限链路**：这正是 `bpm-agent/AGENTS.md` 里 Gate 层"零 LLM、零随机性"的铁律。

所以结合方式不是"让 LLM 来做流程挖掘"，而是**把 pm4py 变成 LLM 可调用的一组确定性工具，让 LLM 做它唯一擅长的事：理解问题、选择工具、解释结论、提出假设**。

---

## 二、pm4py 侧的原理（LLM 需要"知道"什么）

要把 pm4py 工具化，得先明确它每一步在算什么，才能设计好交接契约。

**1. 事件日志的形式化**
日志 $L$ 是迹（trace）的多重集，迹是活动序列：$\sigma = \langle a_1, a_2, ..., a_n \rangle$。**变体（variant）** = 去重后的迹及其出现次数。整个日志的信息量往往集中在少数变体上——这是后面"采样策略"的关键。

**2. 流程发现**（Inductive Miner）
不是启发式拼图，而是**递归切分**：在直接跟随图（DFG）上找切割（cut），把活动集划分为互斥子集，识别为四种结构之一——顺序 `→`、选择 `×`、并发 `∧`、循环 `↺`，递归直到原子活动。`noise_threshold` 就是在这一步过滤低频边。**前提**：日志要干净，噪声直接反映为错误的切割。

**3. 合规检查**
- `token_replay`：用发现的 Petri 网重放迹，统计缺失/剩余 token → fitness
- `alignments`：求日志与模型的最优对齐（代价最小编辑序列），更准但慢

**4. 统计显著性**（`datamind/src/ocel_cross_process.py` 已实现）
以**对象上的相邻转移**为事务单位（不是共现——共现口径在平均对象/事件 4.23 时会把边际概率撑大，导致主干链路算出 lift < 1 的假"负相关"）：

$$\text{lift} = \frac{P(B \mid A)}{P(B)}$$

再用卡方检验 + **Benjamini-Hochberg FDR 校正**（同时检验上百条规则不校正会有大量假阳性）。这一步是"LLM 提假设、pm4py 证伪"闭环的判定核心。

**5. OCEL / 对象中心**
传统日志把流程"拍扁"成单 case，对象间关联丢失。OCEL 允许一个事件关联多个对象，从而回答"流程 A 触发流程 B"。OCPM 按对象类型展开出各自的生命周期视图。

---

## 三、五种结合范式（由浅入深，风险递增）

| 层级 | 做什么 | 交接契约 | 风险 |
|---|---|---|---|
| **L0 结果解释** | pm4py 出数字和图 → LLM 翻译成业务结论 | 结构化指标 dict → 自然语言 | 极低，先做这个 |
| **L1 日志抽取** | LLM 从非结构化源（工单、BPMN XML、需求文档）生成事件日志/抽取 SQL/映射规则 | 非结构化文本 → `(case, activity, ts)` 或 SQL | 中，需人审核 |
| **L2 语义归一化** | LLM 归并同义活动名、推荐对象模型与抽象层级 | 活动名列表 → 归并映射表 | 中，映射表要可审计 |
| **L3 假设生成 + 统计验证** | LLM 提候选假设 → pm4py 判定 | 假设 → Declare 约束/过滤条件 → 检验结果 | 低（pm4py 兜底） |
| **L4 Agent 编排** | LLM 自主选择工具、迭代分析 | function calling 闭环 | 需要防循环、防漂移 |

**L3 是价值最高的**：LLM 的强项是"想到人类没想到的异常"，pm4py 的强项是"判定这个异常是否真的显著"。两者互补得最彻底。

举个真实例子——上一轮模拟数据里跑出来的：

```
Return to Supplier (退货) → Enter Incoming Invoice (开票)   26 次
```

LLM 提出假设"退货后仍开票，可能是流程漏洞"，pm4py 用 lift + 卡方 + FDR 判定它是否强于随机。LLM **只负责提出和解释**，判定权在 pm4py。

---

## 四、落地架构：把 pm4py 封装成 bpm-agent 的 Tool

`bpm-agent/tools/base.py` 的 `execute()` 管道已经把安全边界做好了：参数校验 → 身份过滤 → 超时 → 脱敏 → 审计 → 防注入标记。pm4py 工具**直接复用这套管道**，不要另起炉灶。

```python
class MineProcessModel(Tool):
    """流程发现 + 合规:LLM 只填参数,数字全部由 pm4py 算出。"""
    name = "mine_process_model"
    Params = MineParams          # dataset / algorithm / noise_threshold / top_variants

    async def run(self, params: MineParams, identity_filter: dict) -> dict:
        df = load_xes(params.dataset)                 # 确定性加载
        net, im, fm = discover_petri_net(df, params.algorithm, params.noise_threshold)
        conf = conformance_check(df, net, im, fm, "token_replay")
        variants = get_variants(df, top_k=params.top_variants)

        return {
            "fitness": round(conf["fitness"], 3),
            "num_cases": int(df["case:concept:name"].nunique()),
            "num_variants": len(variants),
            "top_variants": variants[:params.top_variants],
            "bottlenecks": full_performance_report(df)["bottlenecks"][:5],
            # 关键:给 LLM 一个可引用的证据锚点,避免它凭空解释
            "evidence": f"reports/{params.report_ref}#sec3",
        }
```

设计要点：

1. **返回结构化 dict，不返回大表**。变体只给 Top-N + 总数，瓶颈只给 Top-5。全量塞进上下文既贵又会让 LLM 丢失重点。
2. **`evidence` 字段是刚需**。每个结论都要指向报告的具体章节，让回答可溯源。
3. **超时要比默认 5s 长**。流程发现是 CPU 密集，建议单独设 `timeout_s=30`，并对 `(dataset, algorithm, noise_threshold)` 做结果缓存。
4. **脱敏照旧**：金额/手机号走 `mask_columns`，pm4py 不例外。

---

## 五、可靠性工程（决定这套东西能不能上线）

**1. 数字只能来自工具返回**
Prompt 里写死：结论中的每个数字必须能在工具返回体中找到，找不到就说"数据里没有"。这是防幻觉最有效的一条，比任何"请仔细核对"都管用。

**2. 采样要按变体分层，不要截断**
大日志喂给 LLM 前必须缩小。正确做法是**保留高频变体的代表迹 + 低频异常的样本**，而不是取前 N 条——前 N 条在时间序上往往只覆盖一种流程形态。

**3. LLM 生成的规则必须回灌验证**
L1/L2 里 LLM 产出的映射表、抽取 SQL、Declare 约束，一律先跑一遍 pm4py：fitness 是否达标、孤立事件率是否为 0、活动数是否在预期区间。不达标就带着指标让 LLM 修，或回退到人工。

**4. 成本与 KV-cache**
`bpm-agent` 的约束是 `system_base.md` 零动态变量。同理，pm4py 工具调用的**系统提示和工具 schema 必须稳定**，只有参数变化。报告片段走 RAG 按需注入，不要整篇塞进去。

**5. 防结论漂移**
模型版本或 prompt 变了，同一份日志可能得出不同结论。`eval/` 下要有固定数据集 + 固定问题的回归集，把关键结论做成断言。

---

## 六、反模式清单

| 反模式 | 后果 |
|---|---|
| 让 LLM 直接读 XES/CSV 全文 | 超上下文、计数错误、结论不可复现 |
| 让 LLM 算频次/百分比/时延 | 必然幻觉 |
| 用 LLM 判定合规/显著性 | 不可审计，同样的输入不同次结论不同 |
| LLM 参与权限或过滤条件拼接 | 越权漏洞（Gate 层铁律） |
| 让 LLM 直接产出 BPMN 而不校验 fitness | 模型"看起来合理"但与数据不符 |
| 把全量变体表塞进上下文 | 成本爆炸 + 重点丢失 |

---

## 七、建议的落地路线

```
第 1 步(L0)  把 datamind 的分析能力封装成 3~5 个 Tool:
             流程发现 / 合规检查 / 瓶颈分析 / 跨流程触发 / 对象画像
             → 立刻能回答"这个流程长什么样、哪里有瓶颈",风险最低

第 2 步(L3)  加假设验证工具:LLM 提假设 → 生成过滤条件/Declare 约束
             → pm4py 算 lift + 卡方 + FDR → 判定显著与否

第 3 步(L1)  用 LLM 从 BPMN XML / 需求文档抽取对象模型
             → 落地成上一轮那套 object registry,人工审核后入库

第 4 步(L4)  完整 Agent 闭环:多轮调用 + 结果缓存 + 证据引用
```

第 1 步就能覆盖 80% 的运维诊断场景，且不需要任何新的算法工作——pm4py 侧 `datamind` 已经全部实现，缺的只是把它包装成 `Tool` 并接进 `ToolRegistry`。