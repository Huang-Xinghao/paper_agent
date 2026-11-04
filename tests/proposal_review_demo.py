"""Isolated UI verification server. All model responses are explicitly simulated."""

import importlib, json, os, sys, threading, time, uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
base = Path(
    os.environ.get(
        "PAPER_AGENT_REVIEW_DEMO_ROOT",
        str(
            Path(__file__).resolve().parents[2]
            / "proposal-ui-test-results"
            / ("demo-" + uuid.uuid4().hex[:8])
        ),
    )
)
base.mkdir(parents=True, exist_ok=True)
projects = base / "projects"
projects.mkdir(exist_ok=True)
os.environ["PAPER_AGENT_WEB_DATA"] = str(base / "runtime")
os.environ["PAPER_AGENT_PROJECT_ROOTS"] = str(projects)
os.environ.pop("PAPER_AGENT_PUBLIC_HOSTS", None)
os.environ.pop("PAPER_AGENT_PASSWORD_HASH", None)
from paper_agent.proposal_review import CANVAS, PROPOSAL, CanvasDraft, json_bytes, sha
from paper_agent.agent.workflow import WorkflowState, WorkflowRunner
from paper_agent.agent import workflow_cli
from paper_agent.api import proposal_revision, worker, store

api = importlib.import_module("paper_agent.api.app")
root = projects / "模拟体验（不调用真实模型）"
root.mkdir(exist_ok=True)
(root / "refine-logs").mkdir(exist_ok=True)
source = "# 研究方案\n\n## 研究目标\n检验健康信息之外，松弛曲线是否有预测增量。\n\n## 研究方法\n比较统计融合和复杂编码器，保持官方标签与固定划分。\n"
(root / PROPOSAL).write_text(source, encoding="utf-8")
state = WorkflowState(
    run_id="browser",
    task_prompt="验证统计融合的研究价值",
    mode="expert",
    current_node="refine",
    status="awaiting_user",
    expert_checkpoint={
        "node": "refine",
        "status": "pending",
        "next_node": "experiment_plan",
        "report": PROPOSAL,
        "files": [PROPOSAL],
        "summary": "模拟交互测试，不调用真实模型",
        "questions": ["保留哪些比较？", "接受什么边界？"],
    },
)
(root / "workflow-state.json").write_bytes(json_bytes(state.to_dict()))
draft = CanvasDraft.model_validate(
    {
        "sourceHash": sha(source.encode()),
        "nodes": [
            {
                "id": "method",
                "position": {"x": 100, "y": 100},
                "data": {
                    "title": "比较方法",
                    "purpose": "检验松弛信息的增量",
                    "input": "早期数据",
                    "steps": "比较统计融合与复杂模型",
                    "output": "预测与误差",
                    "sectionIds": ["method"],
                },
            }
        ],
        "edges": [],
        "changes": [],
        "review": {
            "sections": [
                {
                    "id": "goal",
                    "title": "这项研究要回答什么",
                    "summary": "健康信息之外，松弛曲线是否有预测增量？",
                    "details": "检验健康信息之外，松弛曲线是否有预测增量。",
                    "focus": False,
                    "edited": False,
                },
                {
                    "id": "method",
                    "title": "关键取舍：方法怎样比较",
                    "summary": "比较统计融合和复杂编码器。",
                    "details": "比较统计融合和复杂编码器，保持官方标签与固定划分。",
                    "focus": True,
                    "edited": False,
                },
            ],
            "feedback": "",
        },
    }
)
(root / CANVAS).write_bytes(json_bytes(draft.model_dump()))


def fake_response(**options):
    time.sleep(1)
    payload = json.loads(options["messages"][-1]["content"])
    submitted = payload["submittedDraft"]
    sections = submitted["review"]["sections"]
    for section in sections:
        section["edited"] = False
    feedback = submitted["review"]["feedback"]
    if "模拟失败" in feedback:
        raise RuntimeError("模拟修订失败：草稿和正式方案均未覆盖，可修改意见后重试")
    questions = (
        []
        if "确定" in feedback
        else [
            {
                "id": "order",
                "question": "是否确定先做简单统计比较，再决定是否使用复杂编码器？",
                "sectionIds": ["method"],
            }
        ]
    )
    sections[1]["summary"] = "先比较简单统计方法，再决定是否引入复杂编码器。"
    sections[1][
        "details"
    ] = "先用简单统计融合验证松弛信息的增量，保留强健康对照、官方标签和固定划分；复杂编码器仅在有额外证据时采用。"
    nodes = submitted["nodes"]
    nodes[0]["data"]["steps"] = sections[1]["details"]
    nodes[0]["data"]["needsReview"] = False
    response = {
        "review": {"sections": sections, "feedback": ""},
        "nodes": nodes,
        "edges": submitted["edges"],
        "summary": "已收窄方法路线，并同步调整比较逻辑；仍保留原研究目标与数据边界。",
        "impacts": [
            {
                "kind": "direct",
                "title": "先验证简单方法",
                "detail": "将先做复杂编码器的路线调整为先比较简单统计融合。",
                "sectionIds": ["method"],
            },
            {
                "kind": "related",
                "title": "验证主张随之调整",
                "detail": "不再以复杂结构本身作为贡献，先检验是否有健康信息之外的增量。",
                "sectionIds": ["goal", "method"],
            },
        ],
        "questions": questions,
        "coverage": [
            {"sourceTitle": "研究目标", "sectionIds": ["goal"]},
            {"sourceTitle": "研究方法", "sectionIds": ["method"]},
        ],
    }
    return iter_demo_response(json.dumps(response))


def iter_demo_response(content):
    yield SimpleNamespace(choices=[SimpleNamespace(
        index=0, delta=SimpleNamespace(content=content), finish_reason=None)])
    yield SimpleNamespace(choices=[SimpleNamespace(
        index=0, delta=SimpleNamespace(content=None), finish_reason="stop")])


proposal_revision.ModelClient = lambda **options: SimpleNamespace(
    model="mock",
    client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_response))
    ),
)


class FakePhase:
    def __init__(self, project, **options):
        self.project = project

    def go(self, prompt, *, phase, completion_check):
        assert phase == "experiment_plan"
        (self.project / "refine-logs/EXPERIMENT_PLAN.md").write_text(
            "### 区块 B1：统计融合与强健康对照\n- 优先级：必须执行\n- 保留固定划分与官方标签。",
            encoding="utf-8",
        )
        (self.project / "refine-logs/EXPERIMENT_TRACKER.md").write_text(
            "模拟计划，不执行实验", encoding="utf-8"
        )
        (self.project / WorkflowRunner._checkpoint_path("experiment_plan")).write_text(
            json.dumps(
                {
                    "summary": "模拟实验计划已生成，未执行实验",
                    "questions": ["调整比较顺序吗？", "调整预算吗？"],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(final="模拟实验计划已生成", iterations=1)


workflow_cli.PhaseAgent = FakePhase
wf = importlib.import_module("paper_agent.agent.workflow")
wf.read_plan_blocks = lambda root: []
wf.write_experiment_tracker = lambda *args: None
api.terminal_projects = lambda: set()


def launch(command, **options):
    run_id = command[-1]

    def job():
        time.sleep(0.5)
        store.run_update(run_id, status="running", pid=os.getpid())
        try:
            worker.execute(run_id)
            result = store.read_json(
                store.data_root() / "runs" / run_id / "result.json", {}
            )
            store.run_update(
                run_id, status=result.get("status", "failed"), finished_at=store.now()
            )
        except Exception as exc:
            store.run_update(
                run_id, status="failed", error=str(exc), finished_at=store.now()
            )

    threading.Thread(target=job, daemon=True).start()
    return SimpleNamespace(pid=os.getpid(), wait=lambda: 0)


api.subprocess.Popen = launch


# 仅这个独立测试服务器提供夹具接口；正式 API 不导入本文件。
@api.app.get("/api/proposal-review-test")
def test_marker():
    return {"simulated": True, "modelCalls": 0}


@api.app.post("/api/proposal-review-test/fixtures")
def create_fixture():
    fresh = projects / ("自动测试-" + uuid.uuid4().hex[:8])
    (fresh / "refine-logs").mkdir(parents=True)
    (fresh / PROPOSAL).write_text(source, encoding="utf-8")
    (fresh / CANVAS).write_bytes(json_bytes(draft.model_dump()))
    (fresh / "workflow-state.json").write_bytes(json_bytes(state.to_dict()))
    return api.register(fresh.name, fresh)


import uvicorn

uvicorn.run(api.app, host="0.0.0.0", port=8001)
