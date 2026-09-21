"""HTML 报告生成器:整合流程图、瓶颈图表、合规统计。"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

logger_msg = "datamind report"


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  background: #f5f7fa;
  color: #1f2937;
  line-height: 1.6;
}}
.container {{
  max-width: 1280px;
  margin: 0 auto;
  padding: 32px 24px;
}}
header {{
  background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%);
  color: #fff;
  padding: 32px 24px;
  border-radius: 12px;
  margin-bottom: 24px;
  box-shadow: 0 4px 20px rgba(30,58,138,0.15);
}}
header h1 {{ margin: 0 0 8px; font-size: 28px; }}
header .subtitle {{ opacity: 0.85; font-size: 14px; }}
section {{
  background: #fff;
  border-radius: 12px;
  padding: 24px;
  margin-bottom: 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}}
section h2 {{
  margin: 0 0 16px;
  font-size: 20px;
  color: #1e3a8a;
  border-left: 4px solid #3b82f6;
  padding-left: 12px;
}}
.cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}}
.card {{
  background: linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);
  border-radius: 10px;
  padding: 18px;
  border: 1px solid #bae6fd;
}}
.card .label {{
  font-size: 12px;
  color: #0369a1;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}
.card .value {{
  font-size: 26px;
  font-weight: 700;
  color: #0c4a6e;
  margin-top: 6px;
}}
.card .unit {{ font-size: 14px; color: #0369a1; margin-left: 4px; }}

/* 流程图容器:保持 SVG 自然尺寸,横向/纵向滚动,避免缩放到文字看不清 */
.diagram-container {{
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fafafa;
  padding: 12px;
  overflow: auto;
  max-height: 760px;
}}
.diagram-container svg {{
  display: block;
  /* 保持 graphviz 输出的自然尺寸;容器滚动 */
  max-width: none;
  height: auto;
}}

.legend {{
  display: flex;
  gap: 16px;
  margin-top: 12px;
  font-size: 13px;
  color: #475569;
}}
.legend-item {{
  display: flex;
  align-items: center;
  gap: 6px;
}}
.legend-dot {{
  width: 14px;
  height: 14px;
  border-radius: 50%;
}}
.legend-rect {{
  width: 18px;
  height: 12px;
  border-radius: 3px;
  border: 1.5px solid #3b82f6;
  background: #dbeafe;
}}

table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}
th, td {{
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid #e5e7eb;
}}
th {{ background: #f9fafb; color: #374151; font-weight: 600; }}
tr:hover {{ background: #f9fafb; }}

.chart-row {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}}
.chart-box {{
  background: #fafafa;
  border-radius: 8px;
  padding: 16px;
  border: 1px solid #e5e7eb;
  position: relative;
  height: 360px;
}}

.badge {{
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}}
.badge-ok {{ background: #dcfce7; color: #166534; }}
.badge-warn {{ background: #fef3c7; color: #92400e; }}
.badge-bad {{ background: #fee2e2; color: #991b1b; }}

footer {{
  text-align: center;
  color: #6b7280;
  font-size: 12px;
  padding: 24px 0;
}}

.warning-box {{
  background: #fef3c7;
  border-left: 4px solid #f59e0b;
  padding: 12px 16px;
  border-radius: 4px;
  color: #92400e;
  margin: 12px 0;
}}
.muted {{ color: #6b7280; font-size: 13px; }}
</style>
</head>
<body>
<div class="container">

<header>
  <h1>{title}</h1>
  <div class="subtitle">{dataset_description} · 生成时间 {generated_at}</div>
</header>

<section>
  <h2>数据概览</h2>
  <div class="cards">
    <div class="card"><div class="label">事件数</div><div class="value">{num_events}<span class="unit">条</span></div></div>
    <div class="card"><div class="label">流程实例</div><div class="value">{num_cases}<span class="unit">个</span></div></div>
    <div class="card"><div class="label">活动类型</div><div class="value">{num_activities}<span class="unit">种</span></div></div>
    <div class="card"><div class="label">资源(审批人)</div><div class="value">{num_resources}<span class="unit">人</span></div></div>
  </div>
  <p class="muted">时间范围:{time_range}</p>
</section>

<section>
  <h2>1. 流程发现</h2>
  <p>基于历史事件日志自动推导真实审批路径。首先看 <b>直接跟随图(DFG)</b>——最易读的流程视图:节点深浅代表执行频次,边上数字代表该流转发生的次数,边越粗说明路径越主流。</p>

  <h3 style="margin: 16px 0 8px; color:#374151; font-size:16px;">直接跟随图(DFG)</h3>
  <div class="diagram-container">
    {dfg_svg}
  </div>
  <div class="legend">
    <div class="legend-item"><span class="legend-dot" style="background:#1e3a8a;"></span>高频活动</div>
    <div class="legend-item"><span class="legend-dot" style="background:#3b82f6;"></span>中频活动</div>
    <div class="legend-item"><span class="legend-dot" style="background:#dbeafe;border:1px solid #94a3b8;"></span>低频活动</div>
    <div class="legend-item"><span class="legend-dot" style="background:#10b981;"></span>结束</div>
  </div>

  <details style="margin-top: 20px;">
    <summary style="cursor:pointer; font-weight:600; color:#1e3a8a; padding:8px 0;">
      展开查看形式化模型(Petri 网,Inductive Miner 推导,噪声阈值 {noise_threshold})
    </summary>
    <p class="muted" style="margin-top:8px;">Petri 网是合规检查所依据的形式化模型。深色小方块是静默变迁(路由节点),蓝色圆角矩形是可观测的审批活动。</p>
    <div class="diagram-container">
      {bpmn_svg}
    </div>
    <div class="legend">
      <div class="legend-item"><span class="legend-dot" style="background:#1e3a8a;"></span>起始状态</div>
      <div class="legend-item"><span class="legend-dot" style="background:#10b981;"></span>终止状态</div>
      <div class="legend-item"><span class="legend-dot" style="background:#fff;border:1.5px solid #94a3b8;"></span>中间状态</div>
      <div class="legend-item"><span class="legend-rect"></span>审批活动</div>
      <div class="legend-item"><span style="width:11px;height:11px;background:#334155;display:inline-block;border-radius:2px;"></span>路由节点(静默变迁)</div>
    </div>
  </details>
  <div class="cards" style="margin-top: 16px;">
    <div class="card"><div class="label">模型活动数</div><div class="value">{model_transitions}</div></div>
    <div class="card"><div class="label">模型节点数</div><div class="value">{model_places}</div></div>
    <div class="card"><div class="label">流程变体数</div><div class="value">{num_variants}</div></div>
    <div class="card"><div class="label">主要起始活动</div><div class="value" style="font-size:16px;">{top_start_activity}</div></div>
  </div>

  <h3 style="margin-top: 24px; color:#374151;">Top 5 流程变体</h3>
  <table>
    <thead><tr><th>#</th><th>流程路径</th><th>案例数</th><th>占比</th></tr></thead>
    <tbody>{variant_rows}</tbody>
  </table>
</section>

<section>
  <h2>2. 合规检查</h2>
  <p>基于上述推导的流程模型,使用 <b>{conformance_method}</b> 对每个流程实例进行合规度评估。</p>
  <div class="cards">
    <div class="card"><div class="label">合规度(fitness)</div><div class="value">{fitness_pct}<span class="unit">%</span></div></div>
    <div class="card"><div class="label">完全拟合</div><div class="value">{num_fitting}<span class="unit">个</span></div></div>
    <div class="card"><div class="label">存在偏离</div><div class="value">{num_non_fitting}<span class="unit">个</span></div></div>
    <div class="card"><div class="label">平均缺失 token</div><div class="value">{avg_missing}</div></div>
  </div>

  <div class="chart-row">
    <div class="chart-box"><canvas id="chart-conformance"></canvas></div>
    <div class="chart-box"><canvas id="chart-case-duration"></canvas></div>
  </div>

  <h3 style="margin-top: 24px; color:#374151;">偏离样本(前 10 条)</h3>
  {non_fitting_table}
</section>

<section>
  <h2>3. 性能与瓶颈分析</h2>
  <p>从耗时、退回、SLA、资源负载四个维度识别审批瓶颈。</p>

  <div class="cards">
    <div class="card"><div class="label">平均审批周期</div><div class="value">{mean_duration_days}<span class="unit">天</span></div></div>
    <div class="card"><div class="label">中位审批周期</div><div class="value">{median_duration_days}<span class="unit">天</span></div></div>
    <div class="card"><div class="label">P90 审批周期</div><div class="value">{p90_duration_days}<span class="unit">天</span></div></div>
    <div class="card">
      <div class="label">SLA 偏离率(>{sla_days}天)</div>
      <div class="value">{sla_breach_rate}<span class="unit">%</span></div>
    </div>
  </div>

  <div class="chart-row">
    <div class="chart-box"><canvas id="chart-bottleneck"></canvas></div>
    <div class="chart-box"><canvas id="chart-rework"></canvas></div>
  </div>

  <h3 style="margin-top: 24px; color:#374151;">活动等待耗时 Top {bottleneck_n}</h3>
  <table>
    <thead><tr><th>活动</th><th>样本数</th><th>平均等待(小时)</th><th>中位</th><th>P90</th><th>最大</th><th>影响评分</th></tr></thead>
    <tbody>{bottleneck_rows}</tbody>
  </table>

  <h3 style="margin-top: 24px; color:#374151;">退回/重复活动(Re-work)</h3>
  <table>
    <thead><tr><th>活动</th><th>重复 case 数</th><th>平均重复次数</th><th>最大重复次数</th><th>总 case</th><th>退回率</th></tr></thead>
    <tbody>{rework_rows}</tbody>
  </table>

  <h3 style="margin-top: 24px; color:#374151;">资源(审批人)负载 Top 20</h3>
  <table>
    <thead><tr><th>资源</th><th>事件数</th><th>参与 case</th><th>活动多样性</th></tr></thead>
    <tbody>{resource_rows}</tbody>
  </table>
</section>

<section>
  <h2>4. 架构建议</h2>
  <ul>
    <li><b>流程规范化:</b>合规度 {fitness_pct}% 意味着 {num_non_fitting} 个 case 偏离了主要路径。建议对高频偏离模式进行根因分析,判断是流程设计不周还是执行违规。</li>
    <li><b>SLA 监控:</b>{sla_breach_rate}% 的 case 超过 {sla_days} 天,需要评估 SLA 阈值合理性,或在系统中加入主动告警与升级机制。</li>
    <li><b>资源均衡:</b>审批人负载差异较大,建议结合流程变体分析和退回率,识别低效审批节点。</li>
    <li><b>流程挖掘落点:</b>本 PoC 输出可接入 BPM 系统监控大屏,作为离线分析层;T+1 调度即可。</li>
  </ul>
</section>

{insight_block}
<footer>由 datamind PoC 自动生成 · 数据基于 {dataset_description}</footer>

</div>

<script>
// 流程图直接嵌入 SVG,无需 JS 渲染
// SVG 由 graphviz 在服务端生成,内置布局

// 合规度饼图
const confFit = {conf_fitting};
const confNonFit = {conf_non_fitting};
if (document.getElementById('chart-conformance')) {{
  new Chart(document.getElementById('chart-conformance'), {{
    type: 'doughnut',
    data: {{
      labels: ['完全拟合', '存在偏离'],
      datasets: [{{
        data: [confFit, confNonFit],
        backgroundColor: ['#10b981', '#ef4444'],
        borderWidth: 2,
        borderColor: '#fff'
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        title: {{ display: true, text: '合规度分布' }},
        legend: {{ position: 'bottom' }}
      }}
    }}
  }});
}}

// Case 时长分布(直方图)
const caseDur = {case_duration_dist_json};
if (document.getElementById('chart-case-duration') && caseDur && caseDur.length > 0) {{
  const labels = caseDur.map(b => b.label);
  const values = caseDur.map(b => b.count);
  new Chart(document.getElementById('chart-case-duration'), {{
    type: 'bar',
    data: {{
      labels: labels,
      datasets: [{{
        label: '案例数',
        data: values,
        backgroundColor: '#3b82f6'
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        title: {{ display: true, text: '审批周期分布(天)' }},
        legend: {{ display: false }}
      }},
      scales: {{
        y: {{ beginAtZero: true }}
      }}
    }}
  }});
}}

// 瓶颈活动
const bottleneckData = {bottleneck_chart_json};
if (document.getElementById('chart-bottleneck') && bottleneckData.length > 0) {{
  const labels = bottleneckData.map(d => d.activity);
  const means = bottleneckData.map(d => d.mean);
  const counts = bottleneckData.map(d => d.count);
  new Chart(document.getElementById('chart-bottleneck'), {{
    type: 'bar',
    data: {{
      labels: labels,
      datasets: [
        {{ label: '平均等待(小时)', data: means, backgroundColor: '#f59e0b', yAxisID: 'y' }},
        {{ label: '事件数', data: counts, backgroundColor: '#3b82f6', yAxisID: 'y1' }}
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        title: {{ display: true, text: '瓶颈活动 Top {bottleneck_n}(平均等待 vs 频次)' }}
      }},
      scales: {{
        y: {{ type: 'linear', position: 'left', title: {{ display: true, text: '小时' }} }},
        y1: {{ type: 'linear', position: 'right', title: {{ display: true, text: '事件数' }}, grid: {{ drawOnChartArea: false }} }}
      }}
    }}
  }});
}}

// 退回率
const reworkData = {rework_chart_json};
if (document.getElementById('chart-rework') && reworkData.length > 0) {{
  new Chart(document.getElementById('chart-rework'), {{
    type: 'bar',
    data: {{
      labels: reworkData.map(d => d.activity),
      datasets: [{{
        label: '退回率',
        data: reworkData.map(d => d.rate),
        backgroundColor: '#ef4444'
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: 'y',
      plugins: {{
        title: {{ display: true, text: '活动退回率(同 case 内重复执行)' }},
        legend: {{ display: false }}
      }},
      scales: {{
        x: {{
          beginAtZero: true,
          ticks: {{ callback: v => (v * 100).toFixed(0) + '%' }}
        }}
      }}
    }}
  }});
}}
</script>
</body>
</html>
"""


def _safe_str(s, default="—"):
    if s is None:
        return default
    return html.escape(str(s))


def _rows_html(records: list[dict], columns: list[tuple[str, str]],
               fmt_map: dict | None = None) -> str:
    """把记录列表渲染成 <tr><td>...</td></tr> 行。"""
    fmt_map = fmt_map or {}
    out = []
    for r in records:
        cells = []
        for key, _label in columns:
            v = r.get(key)
            if key in fmt_map:
                v = fmt_map[key](v)
            elif isinstance(v, float):
                v = round(v, 2)
            cells.append(f"<td>{_safe_str(v)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(out) if out else '<tr><td colspan="{}" class="muted">无数据</td></tr>'.format(len(columns))


def _case_duration_distribution(durations_df, bins=None):
    """审批周期分箱,产出直方图数据。"""
    if bins is None:
        bins = [0, 1, 3, 7, 14, 30, 60, 90, 180, 365, 9999]
    if durations_df.empty or "duration_days" not in durations_df.columns:
        return []
    labels = [f"{bins[i]}-{bins[i+1]}天" for i in range(len(bins)-1)]
    counts = [0] * len(labels)
    for d in durations_df["duration_days"]:
        for i in range(len(bins)-1):
            if bins[i] <= d < bins[i+1]:
                counts[i] += 1
                break
    return [{"label": labels[i], "count": counts[i]} for i in range(len(labels)) if counts[i] > 0]


def build_report(
    *,
    title: str,
    dataset_description: str,
    summary: dict,
    bpmn_xml: str | None,
    dfg_svg: str | None = None,
    process_stats: dict,
    variants: list[dict],
    start_activities: dict,
    conformance: dict,
    performance: dict,
    sla_days: int,
    bottleneck_n: int,
    durations_df=None,
    llm_insight: dict | None = None,
) -> str:
    """组装所有数据,返回完整 HTML 字符串。

    bpmn_xml 参数实际是 Petri 网 SVG(graphviz 渲染),命名保留兼容性。
    dfg_svg 是直接跟随图 SVG(主视图,业务可读)。
    llm_insight 为 InsightResult.to_dict();为 None 时不渲染该章节(向后兼容)。
    """
    from .insight import render_insight_section

    insight_block = render_insight_section(llm_insight, title="5. 智能解读(LLM)")
    diagnostics = conformance
    fitness_pct = round((diagnostics.get("fitness", 0) or 0) * 100, 2)
    num_fitting = diagnostics.get("num_fitting", 0)
    num_non_fitting = diagnostics.get("num_non_fitting", 0)
    avg_missing = round(diagnostics.get("avg_missing_tokens", 0) or 0, 3)
    method_label = "Token Replay" if diagnostics.get("method") == "token_replay" else "Alignments"

    # 变体表
    total_var_cases = sum(v["count"] for v in variants) or 1
    variant_rows = ""
    for i, v in enumerate(variants[:5], 1):
        pct = round(v["count"] / total_var_cases * 100, 1)
        variant_rows += (
            f"<tr><td>{i}</td><td>{_safe_str(v['variant'][:200])}</td>"
            f"<td>{v['count']}</td><td>{pct}%</td></tr>"
        )
    if not variant_rows:
        variant_rows = '<tr><td colspan="4" class="muted">无变体数据</td></tr>'

    # 偏离样本表
    non_fit_samples = diagnostics.get("non_fitting_samples", [])
    if non_fit_samples:
        non_fitting_rows = _rows_html(
            non_fit_samples,
            [("trace", "流程路径"), ("missing", "缺失 token 数")],
        )
    else:
        non_fitting_rows = '<tr><td colspan="2" class="muted">无偏离样本(所有 case 均合规)</td></tr>'

    # 瓶颈表
    bottleneck = performance.get("bottleneck", [])
    bottleneck_rows = ""
    for r in bottleneck[:bottleneck_n]:
        bottleneck_rows += (
            f"<tr><td>{_safe_str(r.get('concept:name'))}</td>"
            f"<td>{int(r['count'])}</td>"
            f"<td>{round(r['mean'], 2)}</td>"
            f"<td>{round(r['median'], 2)}</td>"
            f"<td>{round(r['p90'], 2)}</td>"
            f"<td>{round(r['max'], 2)}</td>"
            f"<td>{int(round(r['impact_score'], 0))}</td></tr>"
        )
    if not bottleneck_rows:
        bottleneck_rows = '<tr><td colspan="7" class="muted">无瓶颈数据</td></tr>'

    # 退回表
    rework = performance.get("rework", [])
    rework_rows = ""
    for r in rework[:10]:
        rate_pct = round((r.get("rework_rate") or 0) * 100, 1)
        rework_rows += (
            f"<tr><td>{_safe_str(r.get('concept:name'))}</td>"
            f"<td>{int(r['rework_cases'])}</td>"
            f"<td>{round(r['avg_repetitions'], 2)}</td>"
            f"<td>{int(r['max_repetitions'])}</td>"
            f"<td>{int(r['total_cases'])}</td>"
            f"<td>{rate_pct}%</td></tr>"
        )
    if not rework_rows:
        rework_rows = '<tr><td colspan="6" class="muted">无退回数据(流程一次性执行率高)</td></tr>'

    # 资源表
    resources = performance.get("resource_load", [])
    resource_rows = ""
    for r in resources[:20]:
        resource_rows += (
            f"<tr><td>{_safe_str(r.get('org:resource'))}</td>"
            f"<td>{int(r['event_count'])}</td>"
            f"<td>{int(r['case_count'])}</td>"
            f"<td>{int(r['activity_diversity'])}</td></tr>"
        )
    if not resource_rows:
        resource_rows = '<tr><td colspan="4" class="muted">无资源数据(日志不含 resource 字段)</td></tr>'

    # 图表数据(Chart.js JSON 注入)
    durations_summary = performance.get("case_durations", {})
    bottleneck_chart = [
        {"activity": r.get("concept:name"), "mean": round(r["mean"], 2), "count": int(r["count"])}
        for r in bottleneck[:bottleneck_n]
    ]
    rework_chart = [
        {"activity": r.get("concept:name"), "rate": r.get("rework_rate") or 0}
        for r in rework[:10]
    ]
    case_dur_dist = _case_duration_distribution(
        durations_df if durations_df is not None else _empty_df_with_durations(performance)
    )

    # 主要起始活动
    top_start = ""
    if start_activities:
        top_start = ", ".join(f"{k} ({v})" for k, v in list(start_activities.items())[:3])

    # 时间范围
    time_range = "—"
    if summary.get("time_range_start") and summary.get("time_range_end"):
        time_range = f"{summary['time_range_start']} → {summary['time_range_end']}"

    html_str = _HTML_TEMPLATE.format(
        title=_safe_str(title),
        dataset_description=_safe_str(dataset_description),
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        num_events=summary.get("num_events", 0),
        num_cases=summary.get("num_cases", 0),
        num_activities=summary.get("num_activities", 0),
        num_resources=summary.get("num_resources", 0),
        time_range=time_range,
        noise_threshold=0.2,
        model_transitions=process_stats.get("num_transitions", 0),
        model_places=process_stats.get("num_places", 0),
        num_variants=len(variants),
        top_start_activity=_safe_str(top_start) or "—",
        variant_rows=variant_rows,
        conformance_method=method_label,
        fitness_pct=fitness_pct,
        num_fitting=num_fitting,
        num_non_fitting=num_non_fitting,
        avg_missing=avg_missing,
        non_fitting_table=(
            f"<table><thead><tr><th>流程路径</th><th>缺失 token 数</th></tr></thead><tbody>{non_fitting_rows}</tbody></table>"
        ),
        mean_duration_days=durations_summary.get("mean_days", 0),
        median_duration_days=durations_summary.get("median_days", 0),
        p90_duration_days=durations_summary.get("p90_days", 0),
        sla_days=sla_days,
        sla_breach_rate=round(performance.get("sla", {}).get("breach_rate", 0) * 100, 2),
        bottleneck_n=bottleneck_n,
        bottleneck_rows=bottleneck_rows,
        rework_rows=rework_rows,
        resource_rows=resource_rows,
        bpmn_svg=bpmn_xml or '<div class="warning-box">未生成 Petri 网,请检查流程发现步骤。</div>',
        dfg_svg=dfg_svg or '<div class="warning-box">未生成 DFG,请检查流程发现步骤。</div>',
        conf_fitting=num_fitting,
        conf_non_fitting=num_non_fitting,
        case_duration_dist_json=json.dumps(case_dur_dist),
        bottleneck_chart_json=json.dumps(bottleneck_chart),
        rework_chart_json=json.dumps(rework_chart),
        insight_block=insight_block,
    )
    return html_str


def _empty_df_with_durations(performance: dict):
    """把 case_durations dict 还原成简单 DataFrame 用于分布统计。"""
    import pandas as pd
    cd = performance.get("case_durations", {})
    if not cd:
        return pd.DataFrame()
    # 因为我们没保留每个 case 的原始数据,这里用五数概括估计分布是粗略的,
    # 直接用性能模块产出的 list 反而更准。但目前接口已固化,简单返回空 df
    # 由上层把详细 durations 传进来。这里保持兼容。
    return pd.DataFrame()


def write_report(html_str: str, output_dir: str | Path, filename_prefix: str = "report") -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"{filename_prefix}_{ts}.html"
    out_path.write_text(html_str, encoding="utf-8")
    return out_path