"""L4 交互问答闭环 CLI:自然语言问流程挖掘问题。

用法:
    python -m src.llm_main --dataset p2p_sim --question "哪些环节最慢?"
    python -m src.llm_main --dataset p2p_sim --question "有哪些可疑的异常链路?" --mode hypothesis
    python -m src.llm_main --dataset p2p_sim --mode object-model --bpmn data/sim/p2p_sim.bpmn
    python -m src.llm_main --regression eval/llm_regression.yaml

设计取舍:
- **意图路由用规则而不是 LLM**:路由是确定性问题,用 LLM 既贵又不可复现。
  LLM 只用在真正需要语义的地方(解读、提假设、建模)。
- 每次调用的结果走 LLMClient 缓存,重复提问不再花钱。
- 回答里的每个数字都来自 pm4py,并带 evidence 锚点;LLM 编造数字会被
  数字溯源校验拦下并退回规则化结论。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

from .llm_client import build_llm_client

logger = logging.getLogger("datamind.llm")

# 规则化意图路由:关键词 → 分析模式
_ROUTE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("假设", "异常", "可疑", "风险", "违规", "越权", "漏洞"), "hypothesis"),
    (("对象模型", "注册表", "建模", "object model", "registry"), "object-model"),
]


def route_intent(question: str) -> str:
    """按关键词选定分析模式。确定性,不经过 LLM。"""
    q = (question or "").lower()
    for keywords, mode in _ROUTE_KEYWORDS:
        if any(k in q for k in keywords):
            return mode
    return "insight"


# --------------------------------------------------------------------------
# 数据装配
# --------------------------------------------------------------------------

def _load_ocel_bundle(dataset: str, cfg: dict, root: Path):
    from .ocel_cross_process import full_cross_process_report
    from .ocel_loader import load_ocel, object_type_stats, summarize_ocel
    from .llm_context import build_ocel_facts

    ds = cfg["ocel_datasets"][dataset]
    ocel = load_ocel(root / ds["path"], max_objects=ds.get("max_objects"))
    summary = summarize_ocel(ocel)
    cross = full_cross_process_report(ocel, min_trigger_count=ds.get("min_trigger_count", 5))
    facts = build_ocel_facts(
        dataset=dataset, summary=summary, cross=cross,
        object_types=object_type_stats(ocel).to_dict(orient="records"))
    return ocel, facts


def _load_xes_bundle(dataset: str, cfg: dict, root: Path):
    from .conformance import conformance_check
    from .data_loader import filter_complete_lifecycle, load_xes, summarize
    from .llm_context import build_single_facts
    from .performance import full_performance_report
    from .process_discovery import discover_petri_net, get_variants

    ds = cfg["datasets"][dataset]
    df = filter_complete_lifecycle(load_xes(root / ds["xes_file"]))
    summary = summarize(df)
    disc = cfg["discovery"]
    net, im, fm = discover_petri_net(df, algorithm=disc["algorithm"],
                                     noise_threshold=disc["noise_threshold"])
    conformance = conformance_check(df, net, im, fm, method=cfg["conformance"]["method"])
    perf = cfg["performance"]
    performance = full_performance_report(df, sla_days=perf["sla_days"],
                                          top_n=perf["bottleneck_top_n"])
    facts = build_single_facts(
        dataset=dataset, summary=summary,
        process_stats={"num_transitions": len(net.transitions),
                       "num_places": len(net.places)},
        conformance=conformance, performance=performance,
        variants=get_variants(df, top_k=10),
        max_variants=int(_llm_cfg(cfg).get("max_variants", 8)))
    return facts


def _llm_cfg(cfg: dict) -> dict:
    from .llm_client import llm_settings
    return llm_settings(cfg)


# --------------------------------------------------------------------------
# 三条执行路径
# --------------------------------------------------------------------------

def run_insight(dataset: str, cfg: dict, root: Path, client,
                force_rule_fallback: bool = False) -> dict:
    """force_rule_fallback 供回归集使用:LLM 不可用时也要产出规则化结论。"""
    from .insight import generate_insight, maybe_generate_insight

    if dataset in cfg.get("ocel_datasets", {}):
        _, facts = _load_ocel_bundle(dataset, cfg, root)
    else:
        facts = _load_xes_bundle(dataset, cfg, root)

    insight = (generate_insight(facts, client) if force_rule_fallback
               else maybe_generate_insight(facts, client))
    return {
        "mode": "insight",
        "dataset": dataset,
        "scope": facts.scope,
        "result": insight.to_dict() if insight else None,
        "note": ("LLM 未启用,未生成解读(config.yaml 中 llm.enabled=false 或缺 API key)"
                 if insight is None else ""),
    }


def run_hypothesis(dataset: str, cfg: dict, root: Path, client) -> dict:
    from .hypothesis import run_hypothesis_loop

    if dataset not in cfg.get("ocel_datasets", {}):
        return {"mode": "hypothesis", "dataset": dataset, "error":
                "假设验证需要 OCEL 数据(依赖对象上的相邻转移),请选择 ocel_datasets 中的数据集"}
    ocel, facts = _load_ocel_bundle(dataset, cfg, root)
    return {"mode": "hypothesis", "dataset": dataset,
            "result": run_hypothesis_loop(facts, ocel, client)}


def run_object_model(cfg: dict, root: Path, client, *, bpmn_path: str,
                     cases: int = 50, seed: int = 42) -> dict:
    from .bpmn_simulator import SimConfig, simulate
    from .object_model_draft import propose_object_model, validate_draft

    tables = simulate(SimConfig(n_requisitions=cases, seed=seed))
    draft = propose_object_model(bpmn_path, tables, client)
    validation = validate_draft(draft, tables)
    return {
        "mode": "object-model",
        "bpmn": str(bpmn_path),
        "result": {
            "source": draft.source,
            "object_types": [o.name for o in draft.objects],
            "notes": draft.notes,
            "validation": validation,
        },
    }


# --------------------------------------------------------------------------
# 回归集
# --------------------------------------------------------------------------

def run_regression(path: str | Path, cfg: dict, root: Path, client) -> dict:
    """跑 eval/llm_regression.yaml:固定数据集 + 固定断言,防结论漂移。"""
    spec = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    results = []
    for case in spec.get("cases", []):
        cid = case["id"]
        expect = case.get("expect", {}) or {}
        try:
            mode = case.get("mode", "insight")
            if mode == "insight":
                out = run_insight(case["dataset"], cfg, root, client,
                                  force_rule_fallback=True)
                r = out.get("result") or {}
                ok = True
                if expect.get("findings_min") is not None:
                    ok = ok and len(r.get("findings", [])) >= expect["findings_min"]
                if expect.get("source_in"):
                    ok = ok and r.get("source") in expect["source_in"]
                detail = f"source={r.get('source')} findings={len(r.get('findings', []))}"
            elif mode == "hypothesis":
                from .hypothesis import Hypothesis, HypothesisSet, sanitize, verify_all

                ocel, _ = _load_ocel_bundle(case["dataset"], cfg, root)
                hs = HypothesisSet(hypotheses=[
                    Hypothesis(name=h.get("name", "case"), rationale="regression",
                               source_activity=h["source_activity"],
                               target_activity=h["target_activity"])
                    for h in case.get("hypotheses", [])])
                verdicts, dropped = verify_all(hs, ocel)
                ok = bool(verdicts) and all(v.supported for v in verdicts) \
                    == bool(expect.get("expect_supported", True))
                detail = "; ".join(f"{v.hypothesis}:{v.supported}(lift={v.lift})"
                                   for v in verdicts) or f"dropped={dropped}"
            elif mode == "object-model":
                out = run_object_model(cfg, root, client, bpmn_path=case["bpmn"],
                                       cases=case.get("cases", 30))
                validation = out["result"]["validation"]
                source = out["result"]["source"]
                if source == "rule":
                    # 规则化候选必须达标
                    ok = validation["ok"] == bool(expect.get("expect_ok", True))
                else:
                    # LLM 候选允许被校验拦下 —— 那正是护栏的价值;
                    # 但必须给出明确的问题说明,不能只是崩掉。
                    ok = validation["ok"] or bool(validation["issues"])
                detail = f"source={source} ok={validation['ok']} issues={validation['issues']}"
            else:
                ok, detail = False, f"未知 mode {mode}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"异常: {e}"
        results.append({"id": cid, "ok": ok, "detail": detail})

    passed = sum(1 for r in results if r["ok"])
    return {"total": len(results), "passed": passed,
            "failed": len(results) - passed, "cases": results}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run(dataset: str, question: str, cfg: dict, root: Path = Path("."),
        mode: str = "auto", bpmn: str | None = None) -> dict:
    client = build_llm_client(cfg, root=root)
    resolved = mode if mode != "auto" else route_intent(question)
    logger.info("模式: %s(来源=%s)", resolved, "指定" if mode != "auto" else "意图路由")

    if resolved == "hypothesis":
        out = run_hypothesis(dataset, cfg, root, client)
    elif resolved == "object-model":
        if not bpmn:
            return {"error": "object-model 模式需要 --bpmn 指定 BPMN 文件"}
        out = run_object_model(cfg, root, client, bpmn_path=bpmn)
    else:
        out = run_insight(dataset, cfg, root, client)
    out["question"] = question
    out["llm_enabled"] = client.available
    return out


def _print(out: dict) -> None:
    if "error" in out:
        print(f"错误: {out['error']}")
        return
    print(f"\n模式: {out.get('mode')} · 数据集: {out.get('dataset')} · "
          f"LLM: {'启用' if out.get('llm_enabled') else '未启用(规则化降级)'}")
    res = out.get("result")
    if res is None:
        print(out.get("note", "无结果"))
        return
    if out["mode"] == "insight":
        print(f"\n摘要: {res['summary']}")
        for f in res["findings"]:
            print(f"  [{f['severity']}] {f['claim']}")
            print(f"       依据: {', '.join(f['metric_refs']) or '—'} | 证据: {f['evidence'] or '—'}")
        for r in res["recommendations"]:
            print(f"  建议: {r}")
    elif out["mode"] == "hypothesis":
        print(f"\n假设来源: {res['source']} · 成立 {res['supported_count']} 条")
        for h, v in zip(res["hypotheses"], res["verdicts"]):
            print(f"  {'✓ 成立' if v['supported'] else '✗ 不成立'} "
                  f"{h['source_activity']} → {h['target_activity']}")
            print(f"       {v['evidence']}")
        for d in res["dropped"]:
            print(f"  已拦截: {d}")
    elif out["mode"] == "object-model":
        v = res["validation"]
        print(f"\n候选来源: {res['source']} · 对象类型: {', '.join(res['object_types'])}")
        print(f"回灌校验: {'通过' if v['ok'] else '未通过'}")
        for i in v["issues"]:
            print(f"  问题: {i}")
        print(f"新增: {v['diff']['added']} | 移除: {v['diff']['removed']}")
        if v["quality"]:
            q = v["quality"]
            print(f"质量: 平均对象/事件 {q['avg_objects_per_event']} · "
                  f"孤立事件率 {q['isolated_pct']}% · 对象类型 {q['num_object_types']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="datamind · LLM × pm4py 交互问答")
    p.add_argument("--dataset", default="p2p_sim")
    p.add_argument("--question", default="")
    p.add_argument("--mode", default="auto",
                   choices=["auto", "insight", "hypothesis", "object-model"])
    p.add_argument("--bpmn", default=None, help="object-model 模式下的 BPMN 文件路径")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--json", default=None, help="把结构化结果写入该文件")
    p.add_argument("--regression", default=None, help="跑回归集(yaml 路径)")
    a = p.parse_args(argv)

    root = Path(a.config).parent
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    client = build_llm_client(cfg, root=root)

    try:
        if a.regression:
            report = run_regression(a.regression, cfg, root, client)
            for c in report["cases"]:
                print(f"{'PASS' if c['ok'] else 'FAIL'} {c['id']} · {c['detail']}")
            print(f"\n回归结果: {report['passed']}/{report['total']} 通过")
            if a.json:
                Path(a.json).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
            return 0 if report["failed"] == 0 else 1

        out = run(a.dataset, a.question, cfg, root=root, mode=a.mode, bpmn=a.bpmn)
        _print(out)
        if a.json:
            Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                               default=str), encoding="utf-8")
        return 0
    except Exception as e:
        logger.exception("执行失败: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
