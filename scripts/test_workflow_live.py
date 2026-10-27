"""Exercise the real workflow with live models and durable node/tool traces."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_agent import PhaseAgent, WorkflowRunner
from paper_agent.agent.loop import AgentLoop

root = Path(__file__).resolve().parents[1]
resume = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
run = resume or root / 'demo-project' / ('workflow-live-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
project = run / 'workspace'
project.mkdir(parents=True, exist_ok=True)
log = run / 'trace.jsonl'
started = time.monotonic()

def record(kind, **data):
    item = {'time': datetime.now().isoformat(), 'kind': kind, **data}
    with log.open('a') as f:
        f.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')
    print(kind, {k:v for k,v in data.items() if k not in {'messages','response','arguments','output'}}, flush=True)

print('RUN', run, flush=True)
agent = PhaseAgent(project, max_iterations=12)
prior_messages = None
if resume:
    prior_messages = json.loads((run/'last-agent-state.json').read_text())['messages']
    agent.model.timeout = 300
    agent.model.client = agent.model.client.with_options(timeout=300, max_retries=0)
original_complete = agent.model.complete
count = 0

def traced_complete(messages, tools, **kwargs):
    global count, prior_messages
    count += 1
    if prior_messages is not None:
        # Restore real tool evidence from the timed-out ideas turn, then continue
        # through the normal agent loop. No synthetic tool results are inserted.
        messages[:] = prior_messages + [{"role": "user", "content":
            "上一轮生成报告时请求超时。沿用已检索及核验的证据，先用 write 写 idea-stage/IDEA_REPORT.md，"
            "报告控制在约2000中文字以内，保留候选比较、推荐方向、假设、实验设计和证据边界。不要重新做相同检索。"}]
        prior_messages = None
    record('model_start', phase=agent.phase, call=count, messages=messages)
    response = original_complete(messages, tools, **kwargs)
    record('model_end', phase=agent.phase, call=count, tools=[c.name for c in response.tool_calls], response=response.content)
    return response
agent.model.complete = traced_complete
original_invoke = AgentLoop._invoke

async def traced_invoke(self, spec, arguments):
    record('tool_start', phase=agent.phase, tool=spec.name, arguments=arguments)
    try:
        result = await original_invoke(self, spec, arguments)
        record('tool_end', phase=agent.phase, tool=spec.name, is_error=result.is_error, output=result.output)
        return result
    except Exception as exc:
        record('tool_exception', phase=agent.phase, tool=spec.name, error=str(exc))
        raise
AgentLoop._invoke = traced_invoke

def save_state(state):
    (run/'workflow-state.json').write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))

def on_start(node, state):
    save_state(state)
    record('node_start', node=node, transitions=state.transitions)

def on_end(node, state, result):
    save_state(state)
    if getattr(agent, "last_state", None):
        (run/f'{state.transitions:02d}-{node}-agent.json').write_text(json.dumps(vars(agent.last_state), ensure_ascii=False, indent=2))
    record('node_end', node=node, artifact=result.artifact[:300], metadata=result.metadata)

runner = WorkflowRunner(agent, on_node_start=on_start, on_node_end=on_end)
record('configured', model=agent.model.model, max_iterations=12, skill_chars=len(agent._skill_prompt()), resumed=bool(resume), timeout=agent.model.timeout)
state = runner.run('针对锂离子电池寿命预测，生成具有明确创新点、可验证且适合后续真实实验的研究 idea。结合本地电池资源库和可检索文献，比较候选方向并推荐一个。明确数据可用性、关键假设、验证方法和风险，然后按工作流继续推进。')
save_state(state)
if getattr(agent, "last_state", None):
    (run/'last-agent-state.json').write_text(json.dumps(vars(agent.last_state), ensure_ascii=False, indent=2))
record('finished', status=state.status, node=state.current_node, error=state.error, seconds=round(time.monotonic()-started,1))
