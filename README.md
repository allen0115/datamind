# datamind: BPM 流程审批系统 PoC

数据挖掘视角下的 BPM 流程审批系统 PoC,基于 BPIC 2020(差旅报销)公开数据集,实现:

1. **流程发现** —— Inductive Miner 自动推导真实审批路径
2. **合规检查** —— 基于推导模型识别偏离路径
3. **性能与瓶颈分析** —— 识别审批耗时、退回率、SLA 偏离

最终产出:单文件 HTML 报告,含流程图、瓶颈图表、合规统计。

## 项目结构

```
datamind/
├── data/                # 事件日志(XES 单流程 / OCEL 2.0 对象中心)
├── reports/             # 生成的 HTML 报告
├── outputs/             # 中间产物
├── src/                 # 核心代码
│   ├── data_loader.py          # 传统事件日志(XES)加载
│   ├── process_discovery.py    # 流程发现 + 流程图渲染
│   ├── conformance.py          # 合规检查
│   ├── performance.py          # 性能与瓶颈分析
│   ├── report_generator.py     # 单流程 HTML 报告
│   ├── main.py                 # 单流程入口
│   │
│   ├── ocel_loader.py          # OCEL 2.0 加载与统计
│   ├── ocel_cross_process.py   # 跨流程关系挖掘(核心)
│   ├── ocel_discovery.py       # 对象中心流程发现(OCPM)
│   ├── ocel_report.py          # OCEL HTML 报告
│   ├── ocel_main.py            # OCEL 入口
│   │
│   ├── bpmn_model.py           # BPMN 模型定义 + BPMN 2.0 XML 导出
│   ├── bpmn_simulator.py       # BPM 引擎执行模拟(引擎表 + 业务表)
│   ├── bpmn_registry.py        # 对象注册表(转换映射的唯一真源)
│   ├── bpmn_converter.py       # 引擎数据 → XES / OCEL 转换
│   ├── sim_main.py             # 模拟 + 转换入口
│   │
│   ├── llm_client.py           # LLM 调用层(provider/缓存/审计/降级)
│   ├── llm_context.py          # pm4py ↔ LLM 交接契约 + 数字溯源校验
│   ├── insight.py              # L0 结果解读
│   ├── hypothesis.py           # L3 假设验证闭环
│   ├── object_model_draft.py   # L1 对象模型抽取与回灌校验
│   └── llm_main.py             # L4 交互问答 CLI + 回归集
├── tests/
├── config.yaml
├── pyproject.toml      # 依赖源（唯一事实来源）
├── uv.lock             # uv 生成的精确版本锁（含哈希）
├── .python-version     # 固定解释器版本（3.14）
└── README.md
```

## 依赖管理（uv）

本仓库用 **uv** 管理 Python 依赖：`pyproject.toml` 为依赖源，`uv.lock` 为精确锁定（含哈希），
`.venv` 由 uv 自动创建，无需手工 `pip install`。

```bash
# 安装 uv（macOS / Linux）
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync                                   # 创建/同步 .venv 并安装核心 + dev 依赖
uv run python -m src.main --dataset domestic_declarations   # 用 uv 托管的环境运行
uv run python -m tests.test_smoke         # 跑烟囱测试
uv add <package>                          # 新增依赖（自动改 pyproject.toml + uv.lock）
```

`--break-system-packages` 之类的 pip 参数不再需要，uv 环境天然隔离。

> **跨平台注意**：`.venv` 是在 macOS 上创建的（`python` 软链指向 uv 管理的 macOS 解释器），
> 只能在 macOS 本机使用。若在 Linux 容器/VM 里运行（例如桌面端的沙箱环境），
> 不要直接 `uv sync` —— 那会把 `.venv` 重建为 Linux 版、破坏本机环境。
> 改用环境变量把依赖装到别处，项目配置与 `uv.lock` 仍然生效：
>
> ```bash
> UV_PROJECT_ENVIRONMENT=/tmp/datamind-env uv sync --python 3.14
> UV_PROJECT_ENVIRONMENT=/tmp/datamind-env uv run python -m src.ocel_main --dataset p2p
> ```

## 快速开始

```bash
# 安装 Python 依赖（uv 自动建 .venv）
uv sync

# 安装 graphviz 系统二进制(流程图渲染必需)
brew install graphviz              # macOS
# apt-get install graphviz         # Ubuntu / Debian

# 准备数据:把 BPIC 2020 的 XES 文件放到 data/ 目录
# 下载链接:https://www.kaggle.com/datasets/asjad99/understand-travel-reimbursement-process
# 推荐 DomesticDeclarations.xes(国内报销,审批特征最贴近通用审批场景)
uv run python -m tests.download_data      # 打印下载指引

# 运行完整流水线
uv run python -m src.main --dataset domestic_declarations

# 报告输出到 reports/report_<dataset>_<timestamp>.html
```

## 报告里的流程图怎么来的

报告里的图都是**服务端用 graphviz 生成 SVG 后直接嵌入 HTML**,不依赖前端 JS。

单流程报告(BPIC)包含两张图:

1. **直接跟随图(DFG)** —— 主视图。节点是活动,颜色深浅代表执行频次,边上数字是该流转
   发生的次数,虚线边表示回退(如被驳回后重新提交)。业务方可直接读。
2. **Petri 网** —— 折叠区。这是合规检查所依据的形式化模型,图中深色小方块是静默
   变迁(路由节点)。

> 注意:pm4py 自带的 `convert_to_bpmn` 导出器不计算布局坐标(所有节点都在 (0,0)),
> 用 bpmn-js 渲染会得到空白画布,因此本项目未采用该路径。

---

## OCEL 对象中心分析(跨流程关系挖掘)

传统事件日志把流程"拍扁"成单条 case,对象之间的关联在拍扁过程中丢失,
因此无法回答"流程 A 触发流程 B"。"扩展 OCEL" 模块解决的就是这个问题:
OCEL 允许一个事件同时关联多个对象,关联关系得以保留。

### 快速开始

```bash
# 下载官方 P2P OCEL 2.0 数据集(约 2.4MB,CC-BY-4.0)
curl -sL -o data/ocel/p2p.jsonocel.zip \
  "https://zenodo.org/api/records/8433706/files/2_p2p.jsonocel.zip/content"
cd data/ocel && unzip p2p.jsonocel.zip && cd ../..

# 运行 OCEL 分析
uv run python -m src.ocel_main --dataset p2p
```

### 三项分析产出

| 分析 | 回答什么问题 | 报告章节 |
|---|---|---|
| **对象交互图** | 哪两个子流程耦合最紧? | 第 3 节 |
| **跨流程触发关系** | 流程 A 的哪个活动触发了流程 B?触发多少次? | 第 4 节 |
| **统计显著性验证** | 这些"触发"是否强于随机?哪些是返工环? | 第 5 节 |
| **对象中心流程发现** | 每种对象各自的完整生命周期长什么样? | 第 8 节 |

### 统计显著性验证的口径说明

第 4 节给的是**触发次数**,第 5 节判断这个次数**是否强于随机**。两者必须一起看。

度量以**对象上的相邻事件对(转移)** 为事务单位,而非"对象共现"。
这条口径选择很关键:一个事件平均关联 4.23 个对象,高频活动会覆盖极大量对象
(本数据集 "Create Purchase Order" 覆盖 64% 的对象),若用共现口径,
边际概率被撑大会让 lift 公式严重稀释,主干链路反而算出 lift < 1 的"负相关"。

指标定义:`confidence = P(B 紧随 A | A 出现)`,`baseline = P(B 作为后继)`,
`lift = confidence / baseline`。显著性用卡方检验 + **Benjamini-Hochberg FDR 校正**
(同时检验上百条规则,不校正会有大量假阳性),lift 给出 Delta 方法的 95% 置信区间。

**为什么不能只看 confidence**:`Create Purchase Order → Enter Incoming Invoice`
的 confidence 高达 97.4%,但 baseline 本身就有 80.4%,因此 lift 仅 1.21 ——
高计数是体量驱动的,不是特异性驱动的。报告第 5 节用这个实例说明了陷阱。

统计量的正确性由 `tests/test_association.py` 验证(手工算例、scipy 交叉对比、
零假设假阳性率、BH 性质),共 6 项。

### 数据源说明

使用的是 **RWTH Aachen 官方 OCEL 2.0 合集**(ocel-standard.org,制定 OCEL 标准的团队维护)。
P2P 数据集为 SAP 真实单据体系:采购申请 → 采购订单 → 收货 → 发票 → 付款。

> **不要用 HuggingFace 上的 VynFi OCEL 数据集**:实测其 94% 的事件不关联任何对象
> (`object_refs` 为空列表),对象关联缺失导致跨流程分析在结构上无法成立;
> 且其 README 宣称的 production_order / quality_inspection 对象类型在实际数据中并不存在。

### 已知数据限制

P2P 数据集是**模拟生成**的,绝对时间戳跨度 1994–2020 且约 59% 重复。
因此报告中的「触发次数」可靠,但**时延数据不可用于 SLA 结论**。
接入真实系统日志后此项限制自动消失(移除 `config.yaml` 里的 `timestamp_warning`)。

---

## BPMN 模拟数据集 → XES / OCEL(转换工具)

真实 BPM 系统的流程实例执行数据要变成 POC 可用的数据集,中间缺的不是格式,
而是**对象维度**:引擎只会给流程实例带一个 `business_key`。
这条链路把这件事完整跑通一遍,并可直接替换成真实数据源。

### 快速开始

```bash
# 一步生成:BPMN 模型 + 模拟执行 + 转换
uv run python -m src.sim_main --cases 200 --seed 42

# 产物在 data/sim/
#   p2p_sim.bpmn                      BPMN 2.0 模型(可用 Camunda Modeler 打开)
#   p2p_sim.jsonocel                  OCEL 2.0 数据集
#   p2p_sim_instance.xes              XES(case = 流程实例)
#   p2p_sim_flat_purchase_order.xes   XES(case = 业务对象,即 OCEL flattening)
#   source/*.csv                      源系统表(引擎表 + 业务表)
#   conversion_quality.json           转换质量门禁指标

# 接现有分析流水线
uv run python -m src.ocel_main --dataset p2p_sim
uv run python -m src.main      --dataset p2p_sim
```

### 四层结构

| 模块 | 职责 |
|---|---|
| `src/bpmn_model.py` | BPMN 模型定义(节点/顺序流/网关权重),可导出 BPMN 2.0 XML |
| `src/bpmn_simulator.py` | 按模型执行,产出引擎表(`bpm_process_instance` / `bpm_task` / `bpm_variable`)与业务表 |
| `src/bpmn_registry.py` | 对象注册表:对象类型、o2o 关系、展开规则、变量映射。**改业务模型只改这里** |
| `src/bpmn_converter.py` | 表驱动的转换器:事件折叠 + 四条关联规则 + XES/OCEL 落盘 + 质量门禁 |

### 转换口径

- **事件 = 一个活动实例**。多实例会签的 N 条任务折叠成 1 个事件(否则频次虚增 N 倍);
  驳回产生的返工是不同活动实例,**保留**(返工环是最有价值的信号)。
- **网关与开始/结束事件不入日志**,它们是路由噪声。
- 关联规则按优先级叠加:`business_key`(主对象) → `variable`(变量里的单据号)
  → `expansion`(命中指定活动时展开成行项目/物料) → `resource`(assignee 当对象)。

### 质量门禁

`conversion_quality.json` 输出的指标可直接当验收标准:

| 指标 | 本数据集(200 申请) | 不合格意味着 |
|---|---|---|
| 孤立事件率 | 0% | 关联规则没覆盖全部活动 |
| 平均对象/事件 | 3.89 | 低于 1.5 说明多对象关联没建立 |
| 时间戳唯一率 | 100% | 精度不够,时延不可信 |
| 悬空关联 | 0 | 业务库外键缺失或变量值脏 |

---

## LLM × pm4py(组合分析)

pm4py 擅长计算与判定,LLM 擅长语义与假设。两者结合的唯一原则是:

> **LLM 负责语义理解与假设提出,pm4py 负责计算与判定,结论中的每个数字必须来自 pm4py。**

### 快速开始

```bash
# 1) 装模型 SDK(不装也能跑完整流水线,自动降级为规则化结论)
uv sync --extra llm

# 2) 配置模型:复制模板填 key(.env 已被 .gitignore 排除)
cp .env.example .env
#   默认给的是 DeepSeek:
#     DATAMIND_LLM_MODEL=deepseek-flash
#     DATAMIND_LLM_BASE_URL=https://api.deepseek.com
```

DeepSeek 用 OpenAI 兼容协议,官方参数:

| 参数 | 值 |
|---|---|
| `base_url`(OpenAI SDK) | `https://api.deepseek.com` |
| `base_url`(Anthropic) | `https://api.deepseek.com/anthropic` |
| `model` | `deepseek-flash`(推荐)/ `deepseek-v4-pro` |

> 旧模型名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 仍可调用,
> 但对应模型已下线,实际由 DeepSeek-V4.1-Flash 提供服务并按 Flash 价格计费。

配置优先级:**环境变量(.env) > config.yaml > 默认值**,换模型不用改配置文件。
想换通义 / 本地 vLLM 等 OpenAI 兼容服务,只改 .env 里的三行即可(模板里已给出示例)。

```bash
# L0 结果解读:把统计结果翻译成业务语言
.venv/bin/python -m src.llm_main --dataset p2p_sim --question "哪些环节最慢?"

# L3 假设验证:LLM 提假设,pm4py 用 lift + 卡方 + BH FDR 判定
.venv/bin/python -m src.llm_main --dataset p2p_sim --question "有哪些可疑的异常链路?"

# L1 对象模型抽取:从 BPMN 产出候选注册表并回灌校验
.venv/bin/python -m src.llm_main --mode object-model --bpmn data/sim/p2p_sim.bpmn

# 回归集:防模型/提示变更后结论漂移
.venv/bin/python -m src.llm_main --regression eval/llm_regression.yaml

# 报告也会新增「智能解读」「异常假设验证」章节
.venv/bin/python -m src.ocel_main --dataset p2p_sim
```

### 四个层次

| 层 | 模块 | LLM 做什么 | pm4py 做什么 |
|---|---|---|---|
| L0 结果解读 | `src/insight.py` | 指标 → 业务结论 + 行动建议 | 全部数字 |
| L3 假设验证 | `src/hypothesis.py` | 提出候选异常链路 | lift + 卡方 + BH FDR 判定 |
| L1 对象建模 | `src/object_model_draft.py` | 从 BPMN + 表结构提候选注册表 | 回灌转换验证质量 |
| L4 交互问答 | `src/llm_main.py` | 语义理解 | 意图路由用规则(确定性,省成本) |

底座:`src/llm_client.py`(调用/缓存/审计/降级)、`src/llm_context.py`(payload 压缩 + 数字溯源)。

### 三道防幻觉闸门

1. **Schema 校验** —— 输出不符合结构即判调用失败,重试一次后仍失败则降级
2. **数字溯源** —— `assert_numbers_from_facts()`:输出里出现 payload 之外的数字即拒绝
   (会先抹掉 `p90_days` 这类指标名,避免把名字里的 90 误判成编造数字)
3. **白名单** —— 假设只能引用日志中真实存在的活动名与对象类型,越界即丢弃

### 采样与缓存

- 变体按**分层采样**(高频取代表 + 低频异常优先保留),不是截断 Top-N
- `temperature=0` + 磁盘缓存,缓存 key 含 `template_version`,改 prompt 不递增版本不会误命中旧缓存
- 审计写 `outputs/llm_audit.jsonl`,**只记元信息不记 prompt 原文**

### 真实模型验证

```bash
# 离线测试(默认,不打模型、不花钱)
.venv/bin/python -m pytest -q            # 118 passed, 2 deselected

# 真实模型 e2e(需要 .env 里是可用的 key)
.venv/bin/python -m pytest -m e2e -s
```

e2e 断言的不是"模型说了什么",而是两件硬的事:
1. 真实返回能被 pydantic schema 接住
2. 结论里的每个数字都能溯源到 pm4py 的 facts(编造就失败)

`tests/test_llm_openai_provider.py` 另外用一个假的 openai 模块替身,
把"真实调用"链路里除最后一跳 HTTP 之外的部分全部验证过(prompt 组装、
`response_format=json_object`、`temperature=0`、schema 失败重试、异常降级)。

### 离线行为

`llm.enabled: false` 或没有 API key 时:
- 报告不出现新增章节(与开启前完全一致)
- CLI 走规则化降级,结论由阈值规则产生
- 假设验证与对象模型校验**完全由 pm4py 计算**,不受影响

---

## 依赖说明

| 依赖 | 用途 | 备注 |
|---|---|---|
| pm4py 2.7+ | 过程挖掘核心库(流程发现、合规检查) | AGPL v3,商用需注意 |
| graphviz | 流程图渲染(SVG 服务端生成) | 还需系统二进制:`brew install graphviz` |
| pandas / numpy | 数据处理 | |
| pyyaml | 配置加载 | |
| Chart.js | 报告图表 | CDN 加载,无需安装 |

## 架构位置

作为 BPM 系统中的"流程挖掘层",典型部署位置:

```
BPM 业务系统 → Kafka 事件流 → Flink 实时聚合
                                ↓
                  离线数仓(Parquet/ClickHouse)
                                ↓
                    本项目(流程发现 + 合规 + 瓶颈)
                                ↓
                HTML 报告 / 监控告警 / 反哺业务
```