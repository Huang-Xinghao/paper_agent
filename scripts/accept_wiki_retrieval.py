"""Fixed production-entry acceptance: 16 existing queries, 4 boundaries, optionally 2 paid evidence runs."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from paper_agent.agent.loop import PhaseAgent
from paper_agent.agent.model_client import load_env_file
from paper_agent.agent.workflow import WorkflowRunner
from paper_agent.agent.wiki_research import validate_packet, render_packet
from paper_agent.tools import ToolContext
from paper_agent.tools.wiki import search_wiki


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def source_manifest(wiki, projects):
    paths = [
        p
        for p in wiki.rglob("*")
        if p.is_file() and p.suffix in {".md", ".json", ".jsonl"}
    ]
    for project in projects:
        paths += [p for p in project.rglob("*.md") if p.is_file()]
        paths += [project / "workflow-state.json"]
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-root", required=True, type=Path)
    parser.add_argument("--projects-root", required=True, type=Path)
    parser.add_argument(
        "--diagnostics",
        type=Path,
        default=REPO / "tests/fixtures/wiki_retrieval_queries_v1.json",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Two paid production evidence runs; no workflow advance",
    )
    parser.add_argument(
        "--repair-offline",
        action="store_true",
        help="Recheck saved evidence and failed boundary harness only; never calls a model",
    )
    args = parser.parse_args()
    load_env_file(REPO / ".env")
    wiki, projects, output = (
        args.wiki_root.resolve(),
        args.projects_root.resolve(),
        args.output.resolve(),
    )
    if args.live and args.repair_offline:
        parser.error("Offline repair cannot run paid requests")
    if output.exists() and not args.repair_offline:
        parser.error(
            "Choose a fresh output folder; existing acceptance evidence is preserved"
        )
    if output.is_relative_to(wiki) or output.is_relative_to(projects):
        parser.error("Output must be outside source Wiki and projects")
    output.mkdir(parents=True, exist_ok=args.repair_offline)
    os.environ["PAPER_AGENT_WIKI_ROOT"] = str(wiki)
    cases = [
        (
            "tongji",
            "e5d4e4ff4dc64ac6",
            "Tongji 电池早期寿命预测 松弛 容量历史 时序方法",
        ),
        (
            "electrolyte",
            "5f90cc4bfce54990",
            "电解液 实验电导率预测 分子预训练 MD配位 辅助监督 泛化",
        ),
    ]
    before = (
        json.loads((output / "source-manifest.json").read_text(encoding="utf-8"))
        if args.repair_offline
        else source_manifest(wiki, [projects / c[1] for c in cases])
    )
    report = (
        json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
        if args.repair_offline
        else {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "query_results": [],
            "boundaries": [],
            "live": [],
        }
    )
    # 固定上一轮已有查询和标签；不因为本轮排名好坏临时更改验收题。
    for case in (
        []
        if args.repair_offline
        else json.loads(args.diagnostics.read_text(encoding="utf-8"))["rows"]
    ):
        started = time.perf_counter()
        result = search_wiki(
            ToolContext(output), case["query"], entity_type="papers", limit=5
        )
        if result.is_error:
            raise RuntimeError(result.output)
        data = json.loads(result.output)
        candidates = [m["wiki_path"] for m in data["matches"]]
        expected = case["expected_papers"]
        row = {
            "case": case["case"],
            "variant": case["variant"],
            "query": case["query"],
            "expected": expected,
            "candidates": candidates,
            "covered": len(set(expected) & set(candidates)),
            "semantic_status": data.get("semantic_status"),
            "degraded": data.get("degraded"),
            "seconds": round(time.perf_counter() - started, 2),
        }
        report["query_results"].append(row)
        dump(output / "acceptance.json", report)
        print(
            json.dumps(
                {
                    "query": row["case"],
                    "variant": row["variant"],
                    "covered": row["covered"],
                    "expected": len(expected),
                }
            ),
            flush=True,
        )
    # 这四项在明确标识的合成库上验证失败边界，不能冒充真实论文评测。
    (output / "synthetic-boundaries").mkdir(exist_ok=True)
    if args.repair_offline:
        report["initial_boundaries"] = report["boundaries"]
        report["boundaries"] = []
    for label, test in [
        ("no_answer", "test_no_answer_and_isolated_graph"),
        ("isolated_graph", "test_isolated_graph_still_allows_text_retrieval"),
        ("updated_text", "test_changed_page_rejected_even_for_absolute_read"),
        ("semantic_unavailable", "test_semantic_failure_is_explicit"),
    ]:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"tests/test_wiki_research.py::{test}",
                "-q",
                f"--basetemp={output / 'synthetic-boundaries' / label}",
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
        )
        suffix = "-repair" if args.repair_offline else ""
        (output / f"boundary-{label}{suffix}.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        report["boundaries"].append(
            {"case": label, "synthetic": True, "passed": result.returncode == 0}
        )
    if args.live:
        for name, project_id, question in cases:
            source, target = projects / project_id, output / "projects" / name
            for relative in (
                "data-search/DATA_REPORT.md",
                "literature/RESEARCH_REPORT.md",
            ):
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, destination)
            task = json.loads(
                (source / "workflow-state.json").read_text(encoding="utf-8")
            )["task_prompt"]
            agent = PhaseAgent(
                target,
                phase="ideas",
                timeout=240,
                on_progress=lambda event, data: print(
                    json.dumps({"event": event, **data}, ensure_ascii=False), flush=True
                ),
            )
            packet = WorkflowRunner(agent)._wiki_context(
                f"真实研究任务：{task}\n当前取证问题：{question}", stage="ideas"
            )
            report["live"].append(
                {
                    "task": name,
                    "project": project_id,
                    "status": packet["status"],
                    "evidence_count": len(packet["evidence"]),
                    "budget": packet["budget"],
                    "artifact_dir": packet["artifact_dir"],
                    "elapsed_seconds": packet["elapsed_seconds"],
                    "model": agent.model.model,
                }
            )
            dump(output / "acceptance.json", report)
    if args.repair_offline:
        for row in report["live"]:
            folder = Path(row["artifact_dir"])
            packet = json.loads((folder / "packet.json").read_text(encoding="utf-8"))
            raw = json.loads((folder / "raw-answer.json").read_text(encoding="utf-8"))
            passages = json.loads(
                (folder / "observed-passages.json").read_text(encoding="utf-8")
            )
            valid, errors = validate_packet(raw, passages, wiki)
            metadata = {
                e["wiki_path"]: {
                    k: e[k] for k in ("quality_records", "source_record") if k in e
                }
                for e in packet["evidence"]
            }
            packet["evidence"] = [
                {**e, **metadata.get(e["wiki_path"], {})} for e in valid
            ]
            packet["validation_errors"] = errors
            packet["gaps"] = [*raw.get("gaps", []), *errors]
            packet["status"] = (
                "insufficient"
                if not valid or raw.get("assessment") != "sufficient"
                else "partial" if packet["gaps"] else "ready_for_review"
            )
            packet["revalidation_note"] = (
                "原响应不变，仅对三个明确同义角色作确定性归一并重新检查原文、已见段落和版本；没有追加模型请求。原始文件保留。"
            )
            dump(folder / "packet-revalidated.json", packet)
            render_packet(packet, folder / "report-revalidated.md", wiki)
            row.update(
                evidence_count=len(valid),
                status=packet["status"],
                revalidated=True,
                validation_errors=errors,
                report=str(folder / "report-revalidated.md"),
            )
    report["sources_unchanged"] = before == source_manifest(
        wiki, [projects / c[1] for c in cases]
    )
    dump(output / "source-manifest.json", before)
    chinese = [r for r in report["query_results"] if r["variant"] == "original"]
    report["summary"] = {
        "queries": len(report["query_results"]),
        "boundaries_passed": sum(r["passed"] for r in report["boundaries"]),
        "expected_papers_covered": sum(r["covered"] for r in report["query_results"]),
        "expected_papers_total": sum(
            len(r["expected"]) for r in report["query_results"]
        ),
        "original_covered": sum(r["covered"] for r in chinese),
        "original_total": sum(len(r["expected"]) for r in chinese),
    }
    dump(output / "acceptance.json", report)
    lines = [
        "# 正式Wiki检索验收",
        "",
        "来源：2026-11-03服务器快照。16条固定查询衡量候选覆盖，不衡量答案准确率；4项工程边界使用合成库。",
        "",
        f"原始数据未改变：{report['sources_unchanged']}。",
        "",
        f"覆盖统计：{report['summary']}。",
        "",
        "|查询|语言版本|已覆盖/标注论文|语义状态|",
        "|---|---|---|---|",
    ]
    lines += [
        f"|{r['case']}|{r['variant']}|{r['covered']}/{len(r['expected'])}|{r['semantic_status']}|"
        for r in report["query_results"]
    ]
    lines += ["", "## 工程边界", ""] + [
        f"- {r['case']}：{'通过' if r['passed'] else '失败'}（合成库）"
        for r in report["boundaries"]
    ]
    lines += ["", "## 真实任务取证", ""]
    for row in report["live"]:
        lines += [
            f"- {row['task']}：{row['status']}，{row['evidence_count']}条可追溯摘录；模型{row['budget']['model_calls']}/3、工具{row['budget']['tool_calls']}/6、图{row['budget']['graph_calls']}/1；{row['elapsed_seconds']}秒。",
            f"  详细依据：{row.get('report', row['artifact_dir'] + '/report.md')}",
        ]
    if args.repair_offline:
        lines += [
            "",
            "首次四项边界未运行成功：pytest 临时目录的父目录缺失。修复验收脚本后仅重跑四项边界，原错误日志保留。真实响应曾使用旧提示未列清的角色同义词，离线重新校验，未追加模型请求。",
        ]
    lines += [
        "",
        "逐字、已见来源和版本一致不等于科学推论已经验证。真实任务只取证，不训练、不批准、不推进原项目。",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    if not report["sources_unchanged"] or not all(
        r["passed"] for r in report["boundaries"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
