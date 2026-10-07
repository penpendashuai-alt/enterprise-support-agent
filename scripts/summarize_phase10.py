"""Offline, standard-library-only delivery tables from frozen experiment records."""

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = "unknown"
FIELDS = [
    "phase",
    "group",
    "dataset",
    "split",
    "mode",
    "environment",
    "config_source",
    "source",
    "metric",
    "unit",
    "sample_n",
    "numerator",
    "denominator",
    "value",
    "definition",
]
DEFINITIONS = {
    "document_recall_at_5_chunks": "Mean relevant-document coverage after top-5 chunks are deduplicated to documents; answerable healthy queries only",
    "document_mrr_at_5_chunks": "Mean reciprocal first relevant document rank after top-5 chunk deduplication",
    "all_required_evidence": "Fraction of answerable healthy queries retaining every locator in at least one gold alternative",
    "selected_evidence_coverage": "Mean best-alternative required locator coverage after threshold/budget selection",
    "evidence_accepted_without_answer_label": "Fraction of unanswerable healthy queries returning any evidence; not final hallucination rate",
    "candidate_evidence_recall": "Mean best-alternative gold locator coverage in all candidates",
    "evidence_recall_at_5": "Mean best-alternative gold locator coverage in top-5 chunks",
    "evidence_mrr_at_5": "Mean reciprocal first matching gold locator rank in top-5 chunks",
    "false_refusal_proxy": "Fraction of answerable healthy queries with no selected evidence; retrieval proxy only",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), "Expected finite number")
    return value


def ratio(numerator, denominator):
    number(numerator)
    number(denominator)
    require(denominator > 0 and 0 <= numerator <= denominator, "Invalid ratio denominator/count")
    return numerator / denominator


def quantile(values, p):
    return sorted(values)[int((len(values) - 1) * p)] if values else None


def strict_sum(rows, key):
    values = [r.get(key) for r in rows]
    return None if any(v is None for v in values) else sum(number(v) for v in values)


class Sources:
    def __init__(self, root):
        self.root = root
        self.files = {}

    def read(self, name):
        data = (self.root / name).read_bytes()
        self.files[name] = {
            "sha256": hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(),
            "hash_contract": "UTF-8 text with CRLF normalized to LF",
            "experiment_commit": UNKNOWN,
        }
        value = json.loads(data)
        if isinstance(value, dict):
            self.files[name]["recorded_versions"] = {
                k: value[k]
                for k in (
                    "dataset_sha256",
                    "snapshot_id",
                    "retrieval_code_sha256",
                    "source_sha256",
                    "prompt_sha256",
                    "model",
                    "config",
                )
                if k in value
            }
        return value

    def binary(self, name, expected):
        digest = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        require(digest == expected, f"Raw journal hash mismatch: {name}")
        self.files[name] = {
            "sha256": digest,
            "hash_contract": "exact bytes",
            "experiment_commit": UNKNOWN,
        }


def row(context, metric, value, n, definition, *, unit="ratio", num=None, den=None):
    require(type(n) is int and n >= 0, "Missing/invalid sample size")
    if value is not None:
        number(value)
    return dict(
        context,
        metric=metric,
        value=UNKNOWN if value is None else value,
        sample_n=n,
        numerator=UNKNOWN if num is None else num,
        denominator=UNKNOWN if den is None else den,
        unit=unit,
        definition=definition,
    )


def context(phase, group, dataset, split, mode, environment, config, source):
    return dict(
        zip(
            FIELDS[:8],
            [phase, group, dataset, split, mode, environment, config, source],
            strict=True,
        )
    )


def retrieval(sources):
    prefix = "evaluation/results/phase5/"
    source = prefix + "comparison.json"
    comparison = sources.read(source)
    frozen = sources.read(prefix + "frozen-v1.json")
    dataset = sources.read("evaluation/datasets/hybrid_v1.json")
    sources.read(prefix + "environment.json")
    require(comparison["default"] == frozen["provisional_default"] == "dense20", "Default changed")
    rows, timing = [], []
    for split in ("dev", "heldout"):
        report = sources.read(prefix + split + "-v1.json")
        require(report["dataset_sha256"] == frozen["dataset_sha256"], "Dataset hash mismatch")
        sources.binary(prefix + report["raw_journal"], report["raw_sha256"])
        cases = [c for c in dataset["cases"] if c["split"] == split]
        require(len(comparison["splits"][split]) == 7, "Expected seven retrieval groups")
        for group, data in comparison["splits"][split].items():
            require(
                data["n"] == len(cases) and data["normal_n"] == data["n"],
                "Incomplete retrieval group",
            )
            ctx = context(
                5,
                group,
                "hybrid_v1",
                split,
                "real_retrieval",
                prefix + "environment.json",
                prefix + "frozen-v1.json#groups/" + group,
                source,
            )
            for metric, d in data["metrics"].items():
                require(metric in DEFINITIONS, f"Unknown retrieval metric: {metric}")
                answerable = metric != "evidence_accepted_without_answer_label"
                expected = sum(c["answerable"] == answerable for c in cases)
                require(d["n"] == expected, f"Wrong metric denominator: {metric}")
                require(0 <= number(d["mean"]) <= 1, "Invalid retrieval mean")
                rows.append(
                    row(
                        ctx,
                        metric,
                        d["mean"],
                        data["n"],
                        DEFINITIONS[metric],
                        num=d["mean"] * d["n"],
                        den=d["n"],
                    )
                )
            for stage, d in sorted(data["latency_all_requests"].items()):
                for metric in ("p50", "p95_nearest_rank"):
                    timing.append(
                        row(
                            ctx,
                            stage + ":" + metric,
                            d[metric],
                            d["n"],
                            "Component stage; p50=median, p95=ceil(.95*n)-th sample; never pooled across groups",
                            unit="seconds",
                        )
                    )
    gen_source = prefix + "generation-summary.json"
    generation = sources.read(gen_source)
    sources.read(prefix + "generation-review.json")
    sources.read(prefix + "generation-v1.json")
    for group, d in generation["groups"].items():
        ctx = context(
            5,
            group,
            "hybrid_v1_selected_9",
            "selected_not_independent",
            "real_chat",
            prefix + "environment.json",
            prefix + "frozen-v1.json#groups/" + group,
            gen_source,
        )
        for metric, denominator in (
            ("complete", d["n"]),
            ("unsupported_claim", d["n"]),
            ("wrong_answer_on_no_answer", d["no_answer_n"]),
            ("supported_points", d["required_points"]),
        ):
            rows.append(
                row(
                    ctx,
                    metric,
                    ratio(d[metric], denominator),
                    d["n"],
                    "Non-independent development-assistant generation review; separate from retrieval metrics",
                    num=d[metric],
                    den=denominator,
                )
            )
    return rows, timing


def batch_metrics(batch):
    requests = batch["requests"]
    require(len(requests) == batch["size"], "Batch denominator differs from raw requests")
    success = [r for r in requests if r["success"]]
    rejected = [r for r in requests if not r["success"] and r["status"] == 429]
    require(len(success) == batch["success"], "Inconsistent success count")
    seconds = number(batch["seconds"])
    require(seconds > 0, "Invalid batch duration")
    values = [number(r["seconds"]) for r in success]
    caches = [r["retrieval"]["cache"] for r in success if r["retrieval"].get("cache")]
    return {
        "success": (len(success), "requests"),
        "rejected_429": (len(rejected), "requests"),
        "other_failures": (len(requests) - len(success) - len(rejected), "requests"),
        "successful_rps": (len(success) / seconds, "requests/second"),
        "p50_success": (quantile(values, 0.5), "seconds"),
        "p95_success": (quantile(values, 0.95), "seconds"),
        "cache_observed": (len(caches), "requests"),
        "cache_hits": (sum(c.get("status") == "hit" for c in caches), "requests"),
        **{f"backend_{k}": (v, "calls") for k, v in batch["backend_calls"].items()},
    }


def latency(sources):
    rows = []
    environment = "evaluation/phase7/environment.json"
    sources.read(environment)
    for name in (
        "baseline-matrix-v2",
        "cache-matrix-v2",
        "control-matrix-v2",
        "full-matrix",
        "baseline-repeat",
        "cache-repeat",
        "control-repeat",
        "full-repeat",
    ):
        source = f"evaluation/phase7/final/service-{name}.json"
        data = sources.read(source)
        for i, batch in enumerate(data["batches"]):
            ctx = context(
                7,
                f"{name}:batch{i}:c{batch['concurrency']}:{batch['workload']}",
                "phase7_synthetic_workloads",
                "finite_batch",
                "deterministic_service",
                environment,
                source + "#controls",
                source + f"#batches/{i}",
            )
            metrics = batch_metrics(batch)
            for metric, (value, unit) in metrics.items():
                rows.append(
                    row(
                        ctx,
                        metric,
                        value,
                        batch["size"],
                        "Raw closed-loop batch; success-only quantiles floor((n-1)*p); 429 excluded from successful throughput; cache_observed is cache denominator",
                        unit=unit,
                        den=metrics["success"][0]
                        if metric in ("p50_success", "p95_success")
                        else metrics["cache_observed"][0]
                        if metric == "cache_hits"
                        else batch["seconds"]
                        if metric == "successful_rps"
                        else batch["size"],
                    )
                )
    source = "evaluation/phase7/live-retrieval.json"
    data = sources.read(source)
    for mode in ("baseline", "miss", "hit"):
        selected = [
            r
            for r in data["cases"]
            if (r["result"].get("cache", {}).get("status") or "baseline") == mode
        ]
        require(bool(selected), "Missing real retrieval group")
        ctx = context(
            7,
            mode,
            "phase7_live_retrieval",
            "repeated_queries",
            "real_retrieval",
            environment,
            source,
            source,
        )
        for metric, value, unit in (
            ("mean_seconds", statistics.mean(r["seconds"] for r in selected), "seconds"),
            ("p95_seconds", quantile([r["seconds"] for r in selected], 0.95), "seconds"),
            (
                "embedding_requests",
                strict_sum([r["result"]["usage"] for r in selected], "requests"),
                "calls",
            ),
        ):
            rows.append(
                row(
                    ctx,
                    metric,
                    value,
                    len(selected),
                    "Retrieval component only; floor quantile; missing usage remains unknown",
                    unit=unit,
                )
            )
    source = "evaluation/phase7/live-agent.json"
    data = sources.read(source)
    for enabled in (False, True):
        selected = [
            r
            for r in data["cases"]
            if r["case"].startswith("paired_") and r["cache_enabled"] == enabled
        ]
        require(len(selected) == 4, "Expected four real Agent samples per arm")
        ctx = context(
            7,
            "full" if enabled else "baseline_flags",
            "phase7_live_agent",
            "paired_4_per_arm",
            "real_chat",
            environment,
            source + "#rag",
            source,
        )
        rows.append(
            row(
                ctx,
                "mean_seconds",
                statistics.mean(r["seconds"] for r in selected),
                len(selected),
                "Whole Agent request; no demonstrated end-to-end speedup; citation validity is not semantic success",
                unit="seconds",
            )
        )
    return rows


def agent_tasks(sources):
    rows = []
    dataset = "evaluation/datasets/agent_v1.json"
    sources.read(dataset)
    for name in (
        "evaluation/datasets/phase8_regressions.json",
        "evaluation/phase8/design-manifest.json",
        "evaluation/phase8/candidate-freeze.json",
        "evaluation/phase8/draft-audit.json",
    ):
        sources.read(name)
    for split in ("dev", "heldout", "regression"):
        for variant in ("baseline", "candidate"):
            if split == "heldout":
                runs = sorted(
                    p
                    for p in (sources.root / "evaluation/phase8/heldout").glob(variant + "-*")
                    if p.is_dir()
                )
                require(bool(runs), "Missing heldout run provenance")
            else:
                suffix = "-v2" if (split, variant) == ("dev", "baseline") else ""
                runs = [sources.root / f"evaluation/phase8/runs/{split}-{variant}{suffix}"]
            review = "reviews.v2.json" if split == "dev" else "reviews.json"
            for run in runs:
                for name in ("manifest.json", review):
                    sources.read((run / name).relative_to(sources.root).as_posix())
            source = f"evaluation/phase8/final-v2/{split}-{variant}.json"
            d = sources.read(source)
            n = d["task_count"]
            require(len(d["tasks"]) == n, "Missing Agent tasks")
            ctx = context(
                8,
                variant,
                "phase8_regressions" if split == "regression" else "agent_v1",
                split,
                "real_agent_non_independent_review",
                "evaluation/phase8/README.md",
                ";".join(
                    (run / "manifest.json").relative_to(sources.root).as_posix() for run in runs
                ),
                source,
            )
            structural = sum(t["structural"]["passed"] for t in d["tasks"])
            rows.append(
                row(
                    ctx,
                    "structural_pass",
                    ratio(structural, n),
                    n,
                    "Frozen structural contract only, not semantic task success",
                    num=structural,
                    den=n,
                )
            )
            for metric in (
                "confirmed_task_success_lower_bound",
                "citation_support",
                "unsupported_answer_rate",
                "review_coverage",
            ):
                r = d[metric]
                value = ratio(r["numerator"], r["denominator"]) if r["denominator"] else None
                require(r["value"] == value, "Inconsistent stored Agent ratio")
                rows.append(
                    row(
                        ctx,
                        metric,
                        value,
                        n,
                        "Frozen contract; metric-specific denominator; non-independent review",
                        num=r["numerator"],
                        den=r["denominator"],
                    )
                )
            if "supplemental_draft_audit" not in d:
                require(split == "regression", "Missing supplemental draft audit")
                audit = {
                    "tasks": d["tasks"],
                    "audited_task_success_lower_bound": d["confirmed_task_success_lower_bound"],
                }
            else:
                audit = d["supplemental_draft_audit"]
            r = audit["audited_task_success_lower_bound"]
            require(r["denominator"] == n and len(audit["tasks"]) == n, "Invalid audit denominator")
            successes = sum(t["outcome"] == "confirmed_success" for t in audit["tasks"])
            require(successes == r["numerator"], "Audit count mismatch")
            if "supplemental_draft_audit" in d:
                rows.append(
                    row(
                        ctx,
                        "supplemental_draft_audited_success",
                        ratio(successes, n),
                        n,
                        "Post-experiment supplemental draft audit, not preregistered heldout metric",
                        num=successes,
                        den=n,
                    )
                )
            for cause, count in sorted(
                Counter(
                    t["primary_cause"] or t["outcome"]
                    for t in audit["tasks"]
                    if t["outcome"] != "confirmed_success"
                ).items()
            ):
                rows.append(
                    row(
                        ctx,
                        "failure:" + cause,
                        count,
                        n,
                        "Frozen-regression primary cause; mutually exclusive per task"
                        if split == "regression"
                        else "Supplemental-audit primary cause; mutually exclusive per task",
                        unit="tasks",
                        num=count,
                        den=n,
                    )
                )
    for version in ("", "-v2"):
        source = f"evaluation/phase9/draft-review{version}.json"
        d = sources.read(source)
        ctx = context(
            9,
            "drafts" + (version or "-v1"),
            "phase9_draft_12",
            "development_regression",
            "real_chat_non_independent_review",
            "evaluation/phase9/README.md",
            source,
            source,
        )
        require(
            d["generated"] + d["clarification"] == d["cases"], "Draft task denominator mismatch"
        )
        rows.append(
            row(
                ctx,
                "faithful_generated",
                ratio(d["faithful_generated"], d["generated"]),
                d["cases"],
                "Generated drafts only; clarification excluded; not 12 completed tasks",
                num=d["faithful_generated"],
                den=d["generated"],
            )
        )
        rows.append(
            row(
                ctx,
                "clarification",
                d["clarification"],
                d["cases"],
                "Did not generate a draft",
                unit="tasks",
                num=d["clarification"],
                den=d["cases"],
            )
        )
    return rows


def as_csv(rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def build(root=ROOT):
    sources = Sources(root)
    retrieval_rows, timings = retrieval(sources)
    timings += latency(sources)
    tasks = agent_tasks(sources)
    manifest = {
        "schema_version": 1,
        "inputs": sources.files,
        "metric_definitions": DEFINITIONS,
        "unknown_policy": "unknown is never zero; absent required denominator is an error",
        "provenance_note": "Exact historical run commit is unknown unless recorded. Original source hashes/config/dataset are retained in referenced reports; do not substitute Phase10 HEAD for experiment commit.",
        "outputs": {
            "retrieval.csv": len(retrieval_rows),
            "latency.csv": len(timings),
            "agent_tasks.csv": len(tasks),
        },
    }
    lines = [
        "# Benchmark 汇总",
        "",
        "由 `python scripts/summarize_phase10.py` 离线生成；`--check` 校验已提交结果，`--output <新目录>` 生成独立副本。仅使用标准库，不读取 .env，不导入运行时，不连接外部服务。",
        "",
        "## 检索对照",
        "",
        "Phase 5 七组、每组 dev 68 / heldout 60 题。表中为 heldout；文档 Recall/MRR 与必要条款覆盖、无答案返回证据、生成有据性分别统计。Dense20 是 dev 选择的默认方案，没有证明优于旧 Dense5。",
        "",
        "| 配置 | 必要证据完整覆盖 | 无答案返回证据 |",
        "| --- | --- | --- |",
    ]
    for group in sorted({r["group"] for r in retrieval_rows if r["split"] == "heldout"}):
        selected = {
            r["metric"]: r
            for r in retrieval_rows
            if r["group"] == group and r["split"] == "heldout"
        }
        cells = [
            f"{selected[k]['value']:.3f} (n={selected[k]['denominator']})"
            for k in ("all_required_evidence", "evidence_accepted_without_answer_label")
        ]
        lines.append(f"| {group} | {' | '.join(cells)} |")
    lines += [
        "",
        "## Agent 任务",
        "",
        "| 分组 | 冻结契约 | 草稿事实补审后 |",
        "| --- | --- | --- |",
    ]
    for variant in ("baseline", "candidate"):
        selected = {
            r["metric"]: r
            for r in tasks
            if r["phase"] == 8 and r["split"] == "heldout" and r["group"] == variant
        }
        cells = [
            f"{selected[k]['numerator']}/{selected[k]['denominator']}"
            for k in ("confirmed_task_success_lower_bound", "supplemental_draft_audited_success")
        ]
        lines.append(f"| {variant} | {' | '.join(cells)} |")
    lines += [
        "",
        "没有证明候选提示词整体质量提升。冻结契约与事后补审不能混作同一评分；自建合成任务、公开技术摘要与非独立语义审阅限制泛化结论。Phase 9 每轮 11/11 已生成草稿忠实，另 1/12 澄清，不是 12/12 任务完成。",
        "",
        "## 延迟与工程范围",
        "",
        "latency.csv 按原实验、批次和组件分开列出；不平均 P95，不把 429 拒绝算成功吞吐。Phase 7 的组件缓存减少外部检索调用，但不避免所有 LLM 调用；四个真实 Agent 样本/组没有证明端到端提速。未知计数保持 unknown；TTFT 未在这些对照中测量，不以整次请求延迟替代。",
        "",
        "375 项通过是 Phase 9 仓库整体软件测试快照，包含上游测试，不是全部新增测试或模型质量分数。部署验证、确定性流程与真实模型质量分别见 [评测说明](../docs/evaluation.md)。",
        "",
        "历史 0.223848 元仅为 Phase 9 有返回 usage 的估价，不是物理 HTTP 尝试账单。此次汇总不做价格重估、不合并费用、不产生新模型调用。",
        "",
        "## 来源与口径",
        "",
        "每行 CSV 给出实验组、数据集/划分、模式、环境、配置、来源、样本量、分子/分母、单位与定义。均值的 numerator 是分项得分之和，不一定是整数题数。sources.json 记录所有输入摘要及指标定义；缺失实验提交明确 unknown，以记录内源码摘要补充定位。",
        "",
        "检索复用 Phase 5 冻结阈值离线汇总，不重新选择阈值；Agent 复用 Phase 8 final-v2 的评分与补审，来源清单包含运行 manifest、审阅与草稿补审。可用 `python scripts/rebuild_phase8.py --output <新目录>` 独立重建评分。Phase 7 从原始 requests 复算成功分母、吞吐和分位数。原始资产保持只读。",
        "",
    ]
    return {
        "retrieval.csv": as_csv(retrieval_rows),
        "latency.csv": as_csv(timings),
        "agent_tasks.csv": as_csv(tasks),
        "results.md": "\n".join(lines),
        "sources.json": json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    require(
        output != ROOT and not output.is_relative_to(ROOT / "evaluation"),
        "Output must not overwrite source evidence",
    )
    results = build()
    if args.check:
        require(
            all(
                (output / n).exists() and (output / n).read_text(encoding="utf-8") == v
                for n, v in results.items()
            ),
            "Derived benchmark differs; regenerate and inspect changes",
        )
    else:
        output.mkdir(parents=True, exist_ok=True)
        for name, value in results.items():
            (output / name).write_text(value, encoding="utf-8", newline="\n")
    print("Benchmark check passed" if args.check else "Wrote five offline benchmark files")


if __name__ == "__main__":
    main()
