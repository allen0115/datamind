"""OCEL 分析报告生成器(单文件 HTML)。"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  background:#f5f7fa; color:#1f2937; line-height:1.6; }
.container { max-width:1320px; margin:0 auto; padding:32px 24px; }
header { background:linear-gradient(135deg,#0f172a 0%,#1e40af 100%); color:#fff;
  padding:32px 24px; border-radius:12px; margin-bottom:24px; box-shadow:0 4px 20px rgba(15,23,42,.18); }
header h1 { margin:0 0 8px; font-size:28px; }
header .subtitle { opacity:.85; font-size:14px; }
section { background:#fff; border-radius:12px; padding:24px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
section h2 { margin:0 0 16px; font-size:20px; color:#0f172a; border-left:4px solid #1e40af; padding-left:12px; }
section h3 { margin:20px 0 10px; font-size:16px; color:#374151; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:16px; margin-bottom:20px; }
.card { background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%); border-radius:10px; padding:18px; border:1px solid #bfdbfe; }
.card .label { font-size:12px; color:#1e40af; text-transform:uppercase; letter-spacing:.5px; }
.card .value { font-size:26px; font-weight:700; color:#1e3a8a; margin-top:6px; }
.card .unit { font-size:14px; color:#1e40af; margin-left:4px; }
.card.good { background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%); border-color:#bbf7d0; }
.card.good .label { color:#15803d; } .card.good .value { color:#14532d; }
.diagram-container { border:1px solid #e5e7eb; border-radius:8px; background:#fafafa; padding:12px; overflow:auto; max-height:720px; }
.diagram-container svg { display:block; max-width:none; height:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { padding:8px 12px; text-align:left; border-bottom:1px solid #e5e7eb; }
th { background:#f9fafb; color:#374151; font-weight:600; }
tr:hover { background:#f9fafb; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.warning-box { background:#fef3c7; border-left:4px solid #f59e0b; padding:14px 18px; border-radius:4px; color:#78350f; margin:12px 0; }
.info-box { background:#eff6ff; border-left:4px solid #3b82f6; padding:14px 18px; border-radius:4px; color:#1e3a8a; margin:12px 0; }
.muted { color:#6b7280; font-size:13px; }
.arrow { color:#1e40af; font-weight:700; }
.tag { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; font-weight:600;
  background:#e0e7ff; color:#3730a3; margin-right:4px; }
footer { text-align:center; color:#6b7280; font-size:12px; padding:24px 0; }
"""


def _esc(v, d="—"):
    return html.escape(str(v)) if v is not None else d


def _num(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return _esc(v)


def _table(rows: list[dict], cols: list[tuple[str, str]],
           num_cols: set[str] | None = None, limit: int | None = None) -> str:
    num_cols = num_cols or set()
    if not rows:
        return f'<p class="muted">无数据</p>'
    out = ["<table><thead><tr>"]
    for _, label in cols:
        out.append(f"<th>{_esc(label)}</th>")
    out.append("</tr></thead><tbody>")
    for r in (rows[:limit] if limit else rows):
        out.append("<tr>")
        for key, _ in cols:
            v = r.get(key)
            cls = ' class="num"' if key in num_cols else ""
            if key in num_cols:
                txt = _num(v)
            elif isinstance(v, float):
                txt = f"{v:,.2f}"
            else:
                txt = _esc(v)
            out.append(f"<td{cls}>{txt}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def build_ocel_report(
    *,
    title: str,
    dataset_description: str,
    summary: dict,
    object_types: list[dict],
    cross: dict,
    discovery: dict,
    dfg_svgs: dict[str, str],
    ocpn: dict,
    timestamp_warning: str | None = None,
    llm_insight: dict | None = None,
    hypothesis_result: dict | None = None,
) -> str:
    """组装 OCEL 报告 HTML。

    llm_insight 为 InsightResult.to_dict();为 None 时不渲染该章节(向后兼容)。
    hypothesis_result 为 hypothesis.run_hypothesis_loop() 的返回;为 None 时不渲染。
    """
    from .insight import render_insight_section
    from .hypothesis import render_hypothesis_section

    insight_block = render_insight_section(llm_insight, title="10. 智能解读(LLM)")
    hypothesis_block = render_hypothesis_section(
        hypothesis_result, title="11. 异常假设验证(LLM 提出 · pm4py 判定)")

    # 对象类型表
    ot_rows = _table(
        object_types,
        [("object_type", "对象类型"), ("num_objects", "对象数"), ("num_events", "涉及事件"),
         ("num_activities", "活动数"), ("avg_events_per_object", "平均事件/对象"),
         ("max_events_per_object", "最大事件/对象")],
        num_cols={"num_objects", "num_events", "num_activities",
                  "avg_events_per_object", "max_events_per_object"},
    )

    # 对象交互图
    inter = cross.get("object_interaction", [])
    inter_rows = _table(
        inter,
        [("type_a", "对象类型 A"), ("type_b", "对象类型 B"),
         ("co_events", "共同出现事件数"), ("shared_activities", "共同活动数")],
        num_cols={"co_events", "shared_activities"}, limit=15,
    )

    # 跨流程触发关系(核心)
    trig = cross.get("cross_process_triggers", [])
    trig_rows = ""
    for r in trig[:20]:
        trig_rows += (
            f'<tr><td>{_esc(r.get("source_activity"))}</td>'
            f'<td><span class="tag">{_esc(r.get("source_domain"))}</span></td>'
            f'<td class="arrow">→</td>'
            f'<td>{_esc(r.get("target_activity"))}</td>'
            f'<td><span class="tag">{_esc(r.get("target_domain"))}</span></td>'
            f'<td class="num">{_num(r.get("count"))}</td>'
            f'<td class="muted">{_esc(r.get("object_types"))}</td></tr>'
        )
    if not trig_rows:
        trig_rows = '<tr><td colspan="7" class="muted">无跨流程触发关系</td></tr>'

    # 桥接活动
    bridge_rows = _table(
        cross.get("bridge_activities", []),
        [("ocel:activity", "活动"), ("num_object_types", "关联对象类型数"),
         ("num_events", "事件数"), ("bridge_score", "桥接评分")],
        num_cols={"num_object_types", "num_events", "bridge_score"}, limit=12,
    )

    # 对象生命周期
    life_rows = _table(
        cross.get("object_lifecycles", []),
        [("object_type", "对象类型"), ("num_objects", "对象数"),
         ("avg_events_per_object", "平均事件数"),
         ("top_start", "主要起始活动"), ("top_end", "主要结束活动")],
        num_cols={"num_objects", "avg_events_per_object"},
    )

    # 对象中心流程发现
    ot_stats = _table(
        discovery.get("object_type_stats", []),
        [("object_type", "对象类型"), ("num_activities", "活动数"),
         ("num_edges", "流转边数"), ("num_events", "事件数"),
         ("top_start_activity", "主要起始活动"), ("top_end_activity", "主要结束活动")],
        num_cols={"num_activities", "num_edges", "num_events"},
    )

    dfg_blocks = ""
    for otype, svg in dfg_svgs.items():
        n_act = next((r["num_activities"] for r in discovery.get("object_type_stats", [])
                      if r["object_type"] == otype), "?")
        dfg_blocks += (
            f'<h3>对象类型 <code>{_esc(otype)}</code> 的流程'
            f'<span class="muted"> · {n_act} 个活动</span></h3>'
            f'<div class="diagram-container">{svg}</div>'
        )

    ts_warn = (f'<div class="warning-box"><b>时间戳可靠性提示:</b> {_esc(timestamp_warning)}</div>'
               if timestamp_warning else "")

    # 统计显著性验证
    asum = cross.get("association_summary", {})
    rules_lift = cross.get("rules_by_lift", [])
    rules_vol = cross.get("rules_by_volume", [])
    bipairs = cross.get("bidirectional_pairs", [])

    def _rule_rows(records):
        if not records:
            return '<tr><td colspan="9" class="muted">无显著规则</td></tr>'
        out = ""
        for r in records:
            ci = (f'[{r.get("lift_ci_low")}, {r.get("lift_ci_high")}]'
                  if r.get("lift_ci_low") is not None else "—")
            p = r.get("p_adjusted")
            p_txt = "<1e-300" if (p is not None and p < 1e-300) else (f"{p:.2e}" if p is not None else "—")
            cross_tag = ('<span class="tag">跨流程</span>'
                         if r.get("is_cross_domain") else "")
            out += (
                f'<tr><td>{_esc(r.get("source_activity"))} {cross_tag}</td>'
                f'<td class="arrow">→</td>'
                f'<td>{_esc(r.get("target_activity"))}</td>'
                f'<td class="num">{_num(r.get("n_ab"))}</td>'
                f'<td class="num">{r.get("confidence", 0):.3f}</td>'
                f'<td class="num">{r.get("baseline", 0):.4f}</td>'
                f'<td class="num"><b>{r.get("lift", 0):.2f}</b></td>'
                f'<td class="muted">{ci}</td>'
                f'<td class="muted">{p_txt}</td></tr>'
            )
        return out

    bipair_rows = ""
    for b in bipairs[:8]:
        bipair_rows += (
            f'<tr><td>{_esc(b["activity_a"])}</td><td style="text-align:center">↔</td>'
            f'<td>{_esc(b["activity_b"])}</td>'
            f'<td class="num">{b["lift_a_to_b"]:.2f}</td>'
            f'<td class="num">{b["lift_b_to_a"]:.2f}</td>'
            f'<td class="num">{_num(b["n_a_to_b"])} / {_num(b["n_b_to_a"])}</td></tr>'
        )
    if not bipair_rows:
        bipair_rows = '<tr><td colspan="6" class="muted">未发现互相触发的活动对</td></tr>'

    isolated = summary.get("isolated_events", 0)
    iso_cls = "card good" if isolated == 0 else "card"

    def _f(v, default="—"):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return default

    asum_tested = _num(asum.get("tested", 0))
    asum_sig = _num(asum.get("significant", 0))
    asum_cross = _num(asum.get("cross_domain", 0))
    asum_maxlift = _f(asum.get("max_lift"))
    rule_rows_lift = _rule_rows(rules_lift)
    rule_rows_vol = _rule_rows(rules_vol)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{_esc(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>{_CSS}</style>
</head>
<body>
<div class="container">

<header>
  <h1>{_esc(title)}</h1>
  <div class="subtitle">{_esc(dataset_description)} · 生成时间 {dt.datetime.now():%Y-%m-%d %H:%M:%S}</div>
</header>

<section>
  <h2>1. OCEL 数据概览</h2>
  <p class="muted">OCEL(Object-Centric Event Log)让一个事件可以同时关联多个对象。
  与传统事件日志的关键差别是:对象之间的关联不会在"拍扁"成单流程时丢失,
  因此可以挖掘"A 流程触发 B 流程"这类跨流程关系。</p>
  <div class="cards">
    <div class="card"><div class="label">事件数</div><div class="value">{_num(summary.get('num_events'))}</div></div>
    <div class="card"><div class="label">对象数</div><div class="value">{_num(summary.get('num_objects'))}</div></div>
    <div class="card"><div class="label">事件-对象关联</div><div class="value">{_num(summary.get('num_relations'))}</div></div>
    <div class="card"><div class="label">事件类型</div><div class="value">{_num(summary.get('num_event_types'))}</div></div>
    <div class="card"><div class="label">对象类型</div><div class="value">{_num(summary.get('num_object_types'))}</div></div>
    <div class="card"><div class="label">平均对象/事件</div><div class="value">{summary.get('avg_objects_per_event', 0)}</div></div>
    <div class="{iso_cls}"><div class="label">孤立事件</div><div class="value">{_num(isolated)}<span class="unit">({summary.get('isolated_pct', 0)}%)</span></div></div>
  </div>
  <p class="muted">时间范围:{_esc(summary.get('time_range_start'))} → {_esc(summary.get('time_range_end'))}
  (跨度 {_num(summary.get('span_days'))} 天)</p>
  {ts_warn}
  <div class="info-box">
    <b>为什么"孤立事件"是关键指标:</b> 孤立事件指不关联任何对象的事件。
    如果这个比例高,说明数据虽然叫 OCEL,但对象关联是缺失的,
    跨流程分析在结构上就无法成立。此数据集孤立事件为 <b>{isolated}</b>,
    对象关联完整。
  </div>
</section>

<section>
  <h2>2. 对象类型概览</h2>
  {ot_rows}
</section>

<section>
  <h2>3. 对象交互图 —— 流程之间的"接缝"</h2>
  <p>统计两种对象类型在同一个事件中共同出现的次数。共现越多,
  说明这两个子流程耦合越紧、越需要在架构上一起考虑。</p>
  {inter_rows}
</section>

<section>
  <h2>4. 跨流程触发关系 —— "流程 A 触发流程 B"</h2>
  <p>沿每个对象的时间线统计相邻活动转移,再按活动的"主导对象类型"
  归类到子流程,筛出跨越子流程边界的转移。这就是跨流程触发关系。</p>
  <table>
    <thead><tr>
      <th>源活动</th><th>源子流程</th><th></th><th>目标活动</th><th>目标子流程</th>
      <th>触发次数</th><th>承载对象类型</th>
    </tr></thead>
    <tbody>{trig_rows}</tbody>
  </table>
  <div class="info-box" style="margin-top:16px;">
    <b>怎么读这张表:</b> 触发次数代表该跨流程流转在日志中出现的频次。
    次数最高的那条通常是主干链路;次数适中但 <b>异常方向</b> 的条目
    (比如"发票已录入却又有新订单创建")往往对应流程漏洞或越权操作,
    值得单独查证。
  </div>
</section>

<section>
  <h2>5. 统计显著性验证 —— 哪些"触发"不是巧合</h2>
  <p>第 4 节的触发次数只说明"发生了多少次",不说明"这个关系是否强于随机"。
  本节用关联规则的度量体系回答后者:以<b>对象上的相邻事件对(转移)</b>为事务单位,
  计算 confidence、baseline 与 <b>lift</b>,并用卡方检验 + BH 多重检验校正判显著性。</p>
  <div class="cards">
    <div class="card"><div class="label">检验规则数</div><div class="value">{asum_tested}</div></div>
    <div class="card good"><div class="label">FDR 校正后显著</div><div class="value">{asum_sig}</div></div>
    <div class="card"><div class="label">其中跨流程</div><div class="value">{asum_cross}</div></div>
    <div class="card"><div class="label">最大 lift</div><div class="value">{asum_maxlift}</div></div>
  </div>

  <div class="warning-box">
    <b>lift 与 confidence 必须一起读,只看一个会得出错误结论。</b><br/>
    度量定义:confidence = P(B 紧随 A | A 出现);baseline = P(B 作为后继);
    <b>lift = confidence / baseline</b>。
    confidence 高只说明 A 之后常接 B,但如果 B 本来就高频,baseline 也高,lift 会接近 1 ——
    即 A 其实没有提供任何额外信息。<br/><br/>
    <b>本数据集的实例:</b> <code>Create Purchase Order → Enter Incoming Invoice</code>
    的 confidence 高达 <b>97.4%</b>,看起来是压倒性的主干链路;但 baseline 本身就有
    <b>80.4%</b>,因此 lift 仅 <b>1.21</b> —— 创建采购订单几乎不提升"录入发票"出现的概率,
    那个 14,572 的高计数是<b>体量驱动</b>的,不是特异性驱动的。
  </div>

  <h3>按 lift 排序 —— 最有信息量的跨流程关系</h3>
  <p class="muted">lift 高说明该转移远高于随机基线。注意 lift 高而 n_ab 小的条目虽然统计确凿,
  但绝对影响面有限;要结合下一张表一起判断。</p>
  <table>
    <thead><tr><th>源活动</th><th></th><th>目标活动</th><th>转移数</th>
      <th>confidence</th><th>baseline</th><th>lift</th><th>lift 95% CI</th><th>校正后 p</th></tr></thead>
    <tbody>{rule_rows_lift}</tbody>
  </table>

  <h3>按发生量排序 —— 运营影响最大的关系</h3>
  <p class="muted">n_ab 是实际发生的转移次数,代表一旦改动该环节会波及多少单据。</p>
  <table>
    <thead><tr><th>源活动</th><th></th><th>目标活动</th><th>转移数</th>
      <th>confidence</th><th>baseline</th><th>lift</th><th>lift 95% CI</th><th>校正后 p</th></tr></thead>
    <tbody>{rule_rows_vol}</tbody>
  </table>

  <h3>互相触发对 —— 返工环的信号</h3>
  <p>A→B 与 B→A 双双显著时,两者互为驱动,这通常不是干净的"流程 A 触发流程 B",
  而是<b>返工环</b>:A 引发返工引出 B,B 又反过来引发 A。纯频次视角看不出这种结构,
  必须两个方向都做检验才会暴露。</p>
  <table>
    <thead><tr><th>活动 A</th><th></th><th>活动 B</th>
      <th>lift(A→B)</th><th>lift(B→A)</th><th>转移数 A→B / B→A</th></tr></thead>
    <tbody>{bipair_rows}</tbody>
  </table>
</section>

<section>
  <h2>6. 桥接活动 —— 把多个流程串起来的节点</h2>
  <p>一次事件关联多种对象类型的活动。桥接评分 = (关联对象类型数 − 1) × 事件数。
  这些活动是流程交接点,改动它们会同时影响多条流程链。</p>
  {bridge_rows}
</section>

<section>
  <h2>7. 对象生命周期边界</h2>
  {life_rows}
</section>

<section>
  <h2>8. 对象中心流程发现(OCPM)</h2>
  <p>把 OCEL 按对象类型展开,每种对象类型得到一条独立的流程视图。
  这是 OCPM 相比传统流程挖掘最实用的产出:采购订单和发票各自的生命周期
  不再被"拍扁"混杂在一起。</p>

  <h3>各对象类型的流程复杂度</h3>
  {ot_stats}

  {dfg_blocks}
</section>

<section>
  <h2>9. 对象中心 Petri 网(OCPN)结构</h2>
  <p class="muted">OCPN 是对象中心流程的形式化模型。图中每个变迁绑定
  (活动, 对象类型) 组合,但规模大时无法直接阅读,因此这里只给结构摘要。</p>
  <div class="cards">
    <div class="card"><div class="label">库所</div><div class="value">{_num(ocpn.get('num_places'))}</div></div>
    <div class="card"><div class="label">变迁</div><div class="value">{_num(ocpn.get('num_transitions'))}</div></div>
    <div class="card"><div class="label">可见变迁</div><div class="value">{_num(ocpn.get('num_labelled_transitions'))}</div></div>
    <div class="card"><div class="label">静默变迁</div><div class="value">{_num(ocpn.get('num_silent_transitions'))}</div></div>
    <div class="card"><div class="label">弧</div><div class="value">{_num(ocpn.get('num_arcs'))}</div></div>
  </div>
</section>

{insight_block}
{hypothesis_block}
<footer>由 datamind OCEL 分析模块自动生成 · 数据源 {_esc(dataset_description)}</footer>
</div>
</body>
</html>
"""


def write_ocel_report(html_str: str, output_dir: str | Path,
                      filename_prefix: str = "ocel_report") -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"{filename_prefix}_{dt.datetime.now():%Y%m%d_%H%M%S}.html"
    out.write_text(html_str, encoding="utf-8")
    return out
