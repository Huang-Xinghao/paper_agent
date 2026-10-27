"""Run the real ideas agent, recording model/tool messages without credentials."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_agent import PhaseAgent

root = Path(__file__).resolve().parents[1]
project = root / 'demo-project' / ('ideas-live-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
project.mkdir(parents=True)
log = project / 'trace.jsonl'
def record(kind, **data):
    item = {'time': datetime.now().isoformat(), 'kind': kind, **data}
    with log.open('a') as f:
        f.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')
    print(kind, {k: v for k,v in data.items() if k not in {'messages','response'}}, flush=True)
print('PROJECT', project, flush=True)
started = time.monotonic()
try:
    agent = PhaseAgent(project, phase='ideas', max_iterations=12)
    original = agent.model.complete
    count = 0
    def traced(messages, tools, **kwargs):
        global count
        count += 1
        record('model_start', iteration=count, messages=messages)
        result = original(messages, tools, **kwargs)
        record('model_end', iteration=count, tools=[c.name for c in result.tool_calls],
               response={'content': result.content, 'calls': [{'name': c.name, 'arguments': c.arguments} for c in result.tool_calls]})
        return result
    agent.model.complete = traced
    record('configured', model=agent.model.model, skill_chars=len(agent._skill_prompt()), max_iterations=12)
    state = agent.go('针对锂离子电池寿命预测，生成具有明确创新点、可验证且适合后续真实实验的研究 idea。结合本地电池资源库和可检索文献，比较候选方向并推荐一个。明确数据可用性、关键假设、验证方法和风险。当前只执行 idea 生成，不进入后续工作流节点。', phase='ideas')
    (project/'agent-state.json').write_text(json.dumps(vars(state), ensure_ascii=False, indent=2))
    (project/'final.md').write_text(state.final)
    record('success', iterations=state.iterations, seconds=round(time.monotonic()-started,1))
except Exception as exc:
    record('failed', error=str(exc), seconds=round(time.monotonic()-started,1))
    if 'agent' in globals() and getattr(agent, "last_state", None):
        (project/'agent-state.json').write_text(json.dumps(vars(agent.last_state), ensure_ascii=False, indent=2))
