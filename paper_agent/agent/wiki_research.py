"""Bounded Wiki evidence collection shared by the real workflow and acceptance runner."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import time
from uuid import uuid4

from .wiki_prompts import RESEARCH_PROMPT as SYSTEM, research_budget_prompt
from .loop import tool_schema
from .model_client import limited_model_requests
from paper_agent.tools import ToolContext, ToolResult, builtin_tools
from paper_agent.tools.wiki import resolve_wiki_root
from paper_agent.tools.wiki_retrieval import load_page

NAMES = {"search_wiki"}
RESEARCH_PHASES = {"literature", "ideas", "novelty", "research_refine"}
_handoff = ContextVar("wiki_evidence_handoff", default=False)


@contextmanager
def evidence_handoff():
    """取证后写报告不再开放第二条无限搜索路径；其他工具与阶段不受影响。"""
    token = _handoff.set(True)
    try:
        yield
    finally:
        _handoff.reset(token)


def has_evidence_handoff():
    return _handoff.get()


ROLES = {"direct_method", "mechanism", "baseline", "constraint", "counterevidence"}


def _dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _delivery(data, limit):
    """压缩可读材料而非截断JSON，实际送给模型的内容才进入已见证据账本。"""
    data = json.loads(json.dumps(data, ensure_ascii=False))
    if "content" in data and len(data["content"]) > 6000:
        data["content"] = data["content"][:6000]
        data["content_truncated"] = True
    while len(json.dumps(data, ensure_ascii=False)) > limit:
        if isinstance(data.get("content"), str) and len(data["content"]) > 300:
            data["content"] = data["content"][: len(data["content"]) // 2]
            data["content_truncated"] = True
            continue
        shortened = False
        for key in ("matches", "cards", "neighbors"):
            if len(data.get(key, [])) > 1:
                data[key].pop()
                data["delivery_truncated"] = True
                shortened = True
                break
        if not shortened:
            return {
                "error": "Material exceeds remaining context budget; read a smaller section",
                "budget_exhausted": True,
            }
    return data


def observed_passages(data):
    """只保存带页面身份的实际正文，不能用另一个来源的相同字符串证明已读。"""
    records = []

    def add(path, section, text, sha):
        if path and section and text and sha:
            records.append(
                {
                    "wiki_path": path,
                    "section": section,
                    "text": text,
                    "content_sha256": sha,
                }
            )

    for card in data.get("cards", []):
        for field in card.get("fields", []):
            add(
                card.get("wiki_path"),
                field.get("section"),
                field.get("content"),
                card.get("content_sha256"),
            )
    for match in data.get("matches", []):
        for hit in match.get("hits", []):
            for excerpt in hit.get("excerpts", [hit]):
                add(
                    excerpt.get("wiki_path", hit.get("wiki_path")),
                    excerpt.get("section"),
                    excerpt.get("excerpt"),
                    excerpt.get("content_sha256"),
                )
    return records


def validate_packet(answer, passages, wiki):
    evidence, errors = [], []
    items = answer.get("evidence", [])
    if not isinstance(items, list):
        return [], ["evidence 必须是列表"]
    for item in items[:8]:
        try:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("claim"), str)
                or not item["claim"].strip()
            ):
                raise ValueError("证据须为包含明确 claim 的对象")
            # 旧提示未完整列出英文枚举；只兼容真实响应中三个明确同义值，
            # 保留原值用于审计，其他未知角色仍拒绝。
            item = dict(item)
            aliases = {
                "boundary": "constraint",
                "condition": "constraint",
                "counterexample": "counterevidence",
            }
            if item.get("role") in aliases:
                item.update(
                    role=aliases[item["role"]], role_normalized_from=item["role"]
                )
            path = (wiki / item["wiki_path"]).resolve()
            if path.parent != (wiki / "papers").resolve() or not path.is_file():
                raise ValueError("不是可读取的论文来源")
            quote = item["quote"]
            if (
                not isinstance(quote, str)
                or not quote
                or len(quote) > 180
                or item.get("role") not in ROLES
            ):
                raise ValueError("引文长度或证据角色无效")
            page = load_page(path)
            section = next(s for s in page.sections if s.heading == item["section"])
            source = "\n".join(page.lines[section.start : section.end])
            seen = [
                p
                for p in passages
                if p["wiki_path"] == item["wiki_path"]
                and p["section"] == item["section"]
                and quote in p["text"]
            ]
            if not seen or quote not in source:
                raise ValueError("引文没有在该来源栏目中被实际读到")
            if not any(p["content_sha256"] == page.fingerprint for p in seen):
                raise ValueError("引用来源已经更新，需要重新取证")
            index = source.index(quote)
            line = section.start + source[:index].count("\n") + 1
            evidence.append(
                {
                    **item,
                    "content_sha256": page.fingerprint,
                    "start_line": line,
                    "end_line": line + quote.count("\n"),
                    "verification": "quotation_and_seen_version_only",
                }
            )
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            errors.append(
                f"未采用引用 {item.get('wiki_path', '?') if isinstance(item, dict) else '?'}: {exc}"
            )
    return evidence, errors


def render_packet(packet, path, wiki):
    lines = [
        "# Wiki研究依据",
        "",
        packet["summary"],
        "",
        f"状态：{packet['status']}；这是待审依据，不是批准方案。",
        "",
    ]
    for title, key in (
        ("采用与排除", "decisions"),
        ("待验证假设", "hypotheses"),
        ("证据缺口", "gaps"),
    ):
        lines += ["## " + title, ""]
        for item in packet.get(key, []):
            lines += [
                "- "
                + (
                    f"{item.get('wiki_path', '')}：{item.get('decision', '')}；{item.get('reason', '')}"
                    if isinstance(item, dict)
                    else str(item)
                ),
                "",
            ]
    lines += ["## 可追溯证据", ""]
    for e in packet["evidence"]:
        lines += [
            f"- **{e['role']}**：{e['claim']}",
            "",
            "> " + e["quote"],
            "",
            f"  [{e['wiki_path']} / {e['section']}]({(wiki / e['wiki_path']).as_posix()}:{e['start_line']})；版本 `{e['content_sha256'][:12]}`。",
            "",
        ]
    lines += [
        "## 下一步",
        "",
        packet.get("next_action", ""),
        "",
        "## 调用记录",
        "",
        f"模型 {packet['budget']['model_calls']}/3；工具执行 {packet['budget']['tool_calls']}/6；图扩展 {packet['budget']['graph_calls']}/1。",
        "",
        "逐字和版本检查不代表推论已被科学验证。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def latest_evidence(workspace):
    """人工修订复用最近依据，重新核对版本；不为每次文字编辑再发起收费搜索。"""
    root = Path(workspace) / ".paper-agent/wiki-retrieval"
    files = sorted(root.glob("*/packet.json"), reverse=True)
    if not files:
        return {
            "status": "unavailable",
            "gaps": ["该项目尚无取证包，新增文献主张须先补证。"],
        }
    try:
        packet = json.loads(files[0].read_text(encoding="utf-8"))
        if packet.get("schema_version") != "wiki-research-v1":
            raise ValueError("Unsupported evidence packet")
        # 只能使用当前配置的 Wiki，不信任历史 JSON 内保存的任意绝对路径。
        evidence = packet.get("evidence", [])
        passages = [
            {
                "wiki_path": e["wiki_path"],
                "section": e["section"],
                "text": e["quote"],
                "content_sha256": e["content_sha256"],
            }
            for e in evidence
        ]
        valid, errors = validate_packet(packet, passages, resolve_wiki_root())
        packet["evidence"] = valid
        packet.pop("pages", None)  # 旧版重复摘录不能绕过本次版本检查。
        if errors:
            packet.update(
                status="stale",
                summary="旧依据已变化，旧摘要与采用建议暂不可用。",
                decisions=[],
            )
            packet["gaps"] = [*packet.get("gaps", []), *errors]
        packet["reuse_note"] = (
            "历史依据仅供参考；人工改变任务条件后必须重新判断适用性。缺失支持列为待决策，不自行补造。"
        )
        return packet
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "unavailable", "gaps": [f"历史取证包无法核验：{exc}"]}


def collect_wiki_evidence(
    workspace: Path, model, query: str, *, stage="ideas", on_progress=None
):
    """正常工作流和真实验收共用此入口；每次收集独立留痕，不修改Wiki。"""
    workspace = Path(workspace).resolve()
    wiki = resolve_wiki_root()
    folder = (
        workspace
        / ".paper-agent/wiki-retrieval"
        / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    )
    folder.mkdir(parents=True)
    tools = {spec.name: spec for spec in builtin_tools() if spec.name in NAMES}
    ctx = ToolContext(workspace)
    calls, passages, graph, errors, model_calls = [], [], [], [], []
    warnings, sources = [], {}
    versions = {}
    delivered = 0
    graph_count = 0
    started = time.perf_counter()

    def progress(kind, **data):
        with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": kind, **data}, ensure_ascii=False) + "\n")
        if on_progress:
            on_progress(
                kind, {"phase": "wiki_retrieval", "research_stage": stage, **data}
            )

    def execute(name, arguments):
        nonlocal delivered, graph_count
        if name not in tools or len(calls) >= 6:
            return {"error": "Tool unavailable or retrieval budget exhausted; finish with explicit gaps",
                    "budget_exhausted": True}
        try:
            arguments = dict(arguments)
            arguments["limit"] = max(1, min(int(arguments.get("limit", 5)), 5))
        except (ValueError, TypeError):
            return {"error": "Invalid tool arguments; no action executed"}
        arguments["entity_type"] = "papers"
        arguments["include_relations"] = graph_count < 1
        calls.append({"name": name, "arguments": arguments})
        progress("wiki_tool_start", tool=name, call=len(calls))
        try:
            result = tools[name].handler(ctx, **arguments)
            try:
                data = json.loads(result.output)
            except (ValueError, TypeError):
                data = {"error" if result.is_error else "output": result.output}
        except Exception as exc:
            result, data = ToolResult(str(exc), True), {"error": str(exc)}
        if result.is_error:
            errors.append(f"{name}: {data.get('error', result.output)}")
        warnings.extend(data.get("warnings", []))
        expansions = int(data.get("relation_expansions", 0)) if not result.is_error else 0
        data = _delivery(
            data, min(12000, max(250, 36000 - delivered - 250 * (6 - len(calls))))
        )
        if data.get("budget_exhausted"):
            warnings.append(
                "取证正文预算已耗尽，部分工具结果没有交付模型；未读内容不可引用。"
            )
        delivered += len(json.dumps(data, ensure_ascii=False))
        records = observed_passages(data) if not result.is_error else []
        passages.extend(records)
        versions.update({p["wiki_path"]: p["content_sha256"] for p in records})
        for source in [data, *data.get("matches", []), *data.get("cards", [])]:
            if source.get("wiki_path"):
                sources[source["wiki_path"]] = {
                    k: source[k]
                    for k in ("quality_records", "source_record")
                    if k in source
                }
        if not result.is_error:
            graph_count += expansions
            if data.get("relations"):
                graph.append({"neighbors": data["relations"]})
        progress(
            "wiki_tool_end",
            tool=name,
            is_error=result.is_error,
            delivered_chars=delivered,
        )
        return data

    seed = execute(
        "search_wiki",
        {
            "query": query[:500],
            "purpose": "overview" if stage == "literature" else "method",
        },
    )
    handoffs = []
    for name in ("data-search/DATA_REPORT.md", "literature/RESEARCH_REPORT.md"):
        path = workspace / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            handoffs.append(
                {"path": name, "content": text[:8000], "truncated": len(text) > 8000}
            )
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": query[:8000],
                    "stage": stage,
                    "handoffs": handoffs,
                    "initial_candidates": seed,
                },
                ensure_ascii=False,
            ),
        },
    ]
    answer = {}
    try:
        with limited_model_requests(3, 4000):
            for iteration in range(1, 4):
                schemas = (
                    [tool_schema(s) for s in tools.values()]
                    if iteration < 3 and len(calls) < 6
                    else []
                )
                progress("wiki_model_start", iteration=iteration)
                mark = time.perf_counter()
                record = {"iteration": iteration, "status": "started"}
                model_calls.append(record)
                try:
                    response = model.complete(messages, schemas)
                except Exception as exc:
                    record.update(
                        status="error",
                        error=str(exc),
                        seconds=round(time.perf_counter() - mark, 2),
                    )
                    raise
                raw = getattr(response, "raw", None)
                usage = (
                    raw.get("usage")
                    if isinstance(raw, dict)
                    else getattr(raw, "usage", None)
                )
                if hasattr(usage, "model_dump"):
                    usage = usage.model_dump()
                record.update(
                    status="completed",
                    seconds=round(time.perf_counter() - mark, 2),
                    usage=usage,
                )
                progress("wiki_model_end", iteration=iteration)
                if not response.tool_calls:
                    messages.append({"role": "assistant", "content": response.content})
                    text = response.content.strip()
                    if text.startswith("```"):
                        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        raise ValueError("Evidence packet must be an object")
                    answer = parsed
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.name,
                                    "arguments": json.dumps(c.arguments),
                                },
                            }
                            for c in response.tool_calls
                        ],
                    }
                )
                for call in response.tool_calls:
                    data = (
                        execute(call.name, call.arguments)
                        if schemas
                        else {"error": "Final round has no tools; no action executed"}
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "name": call.name,
                            "content": json.dumps(data, ensure_ascii=False),
                        }
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": research_budget_prompt(3-iteration, 6-len(calls), 1-graph_count),
                    }
                )
    except Exception as exc:
        errors.append(f"取证未完整完成: {type(exc).__name__}: {exc}")
    evidence, rejected = validate_packet(answer, passages, wiki)
    errors.extend(rejected)
    for key, fallback in (
        ("summary", "当前材料不足以形成可靠的研究依据。"),
        ("next_action", "核对取证缺口后再决定研究方案。"),
    ):
        if not isinstance(answer.get(key, fallback), str):
            errors.append(f"{key} 必须是字符串")
            answer[key] = fallback
    for key in ("gaps", "decisions", "hypotheses"):
        if not isinstance(answer.get(key, []), list):
            errors.append(f"{key} 必须是列表")
            answer[key] = []
    decisions = []
    for decision in answer.get("decisions", [])[:10]:
        if (
            isinstance(decision, dict)
            and isinstance(decision.get("decision"), str)
            and decision.get("decision") in {"use", "adapt", "exclude"}
            and isinstance(decision.get("reason"), str)
            and decision["reason"].strip()
            and isinstance(decision.get("wiki_path"), str)
            and decision.get("wiki_path") in versions
        ):
            decisions.append(decision)
        else:
            errors.append("未采用缺少来源或理由的候选决策")
    gaps = [str(g) for g in answer.get("gaps", [])] + errors
    if not answer:
        gaps.append("未取得结构化结论；已有材料与调用轨迹已保留，需要后续按缺口补充。")
    status = (
        "insufficient"
        if not evidence or answer.get("assessment") != "sufficient"
        else "partial" if gaps else "ready_for_review"
    )
    packet = {
        "schema_version": "wiki-research-v1",
        "status": status,
        "root": str(wiki),
        "summary": str(answer.get("summary", "当前材料不足以形成可靠的研究依据。")),
        "summary_verification": "model_interpretation_not_scientifically_verified",
        "evidence": [{**e, **sources.get(e["wiki_path"], {})} for e in evidence],
        "decisions": decisions,
        "hypotheses": answer.get("hypotheses", [])[:10],
        "gaps": gaps,
        "next_action": answer.get("next_action", "核对取证缺口后再决定研究方案。"),
        "warnings": list(dict.fromkeys(warnings)),
        "validation_errors": errors,
        "relations": graph,
        "budget": {
            "model_calls": len(model_calls),
            "tool_calls": len(calls),
            "graph_calls": graph_count,
            "max_model_calls": 3,
            "max_tool_calls": 6,
            "max_graph_calls": 1,
            "delivered_chars": delivered,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "artifact_dir": str(folder),
        "pages": [
            {
                "path": str(wiki / e["wiki_path"]),
                "content": e["quote"],
                "content_sha256": e["content_sha256"],
            }
            for e in evidence
        ],
    }
    _dump(folder / "transcript.json", messages)
    _dump(folder / "tool-calls.json", calls)
    _dump(folder / "model-calls.json", model_calls)
    _dump(folder / "observed-passages.json", passages)
    _dump(folder / "raw-answer.json", answer)
    _dump(folder / "packet.json", packet)
    render_packet(packet, folder / "report.md", wiki)
    progress(
        "wiki_retrieval_end",
        status=status,
        budget=packet["budget"],
        artifact_dir=str(folder),
    )
    return packet
