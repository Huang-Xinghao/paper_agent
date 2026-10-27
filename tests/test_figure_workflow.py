"""The agent generates once; the runner verifies current-call receipts."""
import asyncio
import json
import hashlib
from types import SimpleNamespace

import pytest

from paper_agent.agent.workflow import WorkflowRunner, WorkflowState
from paper_agent.tools import ToolContext, invoke


@pytest.mark.parametrize('mode', ['success', 'missing', 'failed', 'changed', 'escape',
                                 'old_draft', 'wrong_delivery', 'missing_declaration'])
def test_figure_receipt_contract(tmp_path, monkeypatch, mode):
    from paper_agent.mcp.image_client import ImageMCPClient
    monkeypatch.setenv('PAPER_AGENT_ENABLE_IMAGE_FIGURE', 'true')
    calls = []
    image = 'figures/figure_1.png'
    (tmp_path / 'PAPER_PLAN.md').write_text('Method diagram and measured plots')
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/metrics.json').write_text('{"mae": 0.5}')
    (tmp_path / 'figures').mkdir()
    # A stale file must not satisfy the generation requirement.
    (tmp_path / image).write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 100)

    def generate(self, prompt, **kwargs):
        calls.append(kwargs)
        if mode == 'failed':
            return {'status': 'failed', 'error': '403 Forbidden'}
        (tmp_path / kwargs['output_path']).write_bytes(b'\x89PNG\r\n\x1a\n' + b'y' * 100)
        return {'status': 'done', 'jobId': 'img-test', 'model': 'test-image-model'}
    monkeypatch.setattr(ImageMCPClient, 'generate_start', generate)

    class Agent:
        project = tmp_path
        phases = []

        def go(self, prompt, *, phase, completion_check=None):
            self.phases.append(phase)
            (tmp_path / 'figures/figure_plan.md').write_text('Figure 1')
            (tmp_path / 'figures/latex_includes.tex').write_text(r'\includegraphics{figures/figure_1.png}')
            (tmp_path / 'figures/figure_manifest.json').write_text(json.dumps(
                {'figure_1': {'asset': image, 'preview': image}}))
            messages = []
            if mode != 'missing':
                result = asyncio.run(invoke('generate_image', ToolContext(tmp_path),
                                            prompt='Method diagram', output_path=image))
                payload = json.loads(result.output)
                if mode == 'escape':
                    payload['output_path'] = '../outside.png'
                messages = [{'role': 'tool', 'name': 'generate_image',
                             'is_error': result.is_error, 'content': json.dumps(payload)}]
            if mode == 'changed':
                (tmp_path / image).write_bytes(b'\x89PNG\r\n\x1a\n' + b'z' * 100)
            if mode == 'old_draft':
                # Deleted or subsequently edited unused drafts cannot block final delivery.
                messages.insert(0, dict(role='tool', name='generate_image', content=json.dumps(
                    {'status': 'completed', 'output_path': 'figures/deleted-draft.png', 'sha256': 'old'})))
                messages.insert(1, dict(role='tool', name='generate_image', content=json.dumps(
                    {'status': 'completed', 'output_path': image, 'sha256': 'previous-version'})))
            if mode == 'wrong_delivery':
                (tmp_path / 'figures/figure_manifest.json').write_text(json.dumps(
                    {'figure_1': {'asset': 'figures/other.png', 'preview': 'figures/other.png'}}))
            if mode == 'missing_declaration':
                (tmp_path / 'figures/figure_manifest.json').unlink()
            digest = hashlib.sha256((tmp_path / image).read_bytes()).hexdigest()
            (tmp_path / 'figures/visual_checks.json').write_text(json.dumps({'figures': [dict(
                asset=image, preview=image, asset_sha256=digest, preview_sha256=digest,
                status='passed', findings='Labels readable')]}))
            messages.append(dict(role='tool', name='view_image', content=json.dumps({'path': image, 'sha256': digest})))
            return SimpleNamespace(iterations=1, messages=messages)

    agent = Agent()
    runner = WorkflowRunner(agent)
    if mode in {'success', 'old_draft'}:
        result = runner._run_paper_figure(WorkflowState('test'), 'Write paper')
        assert result.metadata['images'][0]['jobId'] == 'img-test'
        assert result.metadata['images'][0]['sha256']
        assert len(calls) == 1
    else:
        expected = {'missing': 'successful generate_image', 'failed': 'successful generate_image',
                    'changed': 'Image changed', 'escape': 'inside project figures',
                    'wrong_delivery': 'not referenced', 'missing_declaration': 'Declare final Figure 1'}[mode]
        with pytest.raises(RuntimeError, match=expected):
            runner._run_paper_figure(WorkflowState('test'), 'Write paper')
    assert agent.phases == ['paper_figure']


@pytest.mark.parametrize('mode', ['success', 'old_draft', 'unviewed', 'changed_preview', 'changed_pdf',
                                 'unresolved', 'missing_panel', 'failed_view'])
def test_final_figures_require_current_visual_review(tmp_path, mode):
    from PIL import Image
    from paper_agent.agent.loop import ArtifactValidationError

    root = tmp_path / 'figures'
    root.mkdir()
    # Include an appendix via nested TeX: it must not escape the review requirement.
    (root / 'latex_includes.tex').write_text(r'\input{figures/appendix}')
    (root / 'appendix.tex').write_text(r'\includegraphics[width=\textwidth]{figures/result.pdf}')
    (root / 'result.pdf').write_bytes(b'%PDF-final-plot')
    Image.new('RGB', (200, 100), 'white').save(root / 'result.png')
    review = dict(asset='figures/result.pdf', preview='figures/result.png',
                  asset_sha256=hashlib.sha256((root / 'result.pdf').read_bytes()).hexdigest(),
                  preview_sha256=hashlib.sha256((root / 'result.png').read_bytes()).hexdigest(),
                  status='passed', findings='Panel labels are readable; legend does not cover data.',
                  corrections='Moved legend outside the axes.')
    result = asyncio.run(invoke('view_image', ToolContext(tmp_path), path=review['preview']))
    messages = [{'role': 'tool', 'name': 'view_image', 'content': result.output,
                 'is_error': mode == 'failed_view'}]
    if mode == 'old_draft':
        messages.insert(0, {'role': 'tool', 'name': 'view_image',
                           'content': json.dumps({'path': 'figures/deleted-draft.png', 'sha256': 'old'})})
    elif mode == 'unviewed':
        messages = []
    elif mode == 'changed_preview':
        Image.new('RGB', (200, 100), 'red').save(root / 'result.png')
    elif mode == 'changed_pdf':
        (root / 'result.pdf').write_bytes(b'%PDF-edited-after-review')
    elif mode == 'unresolved':
        review['status'] = 'needs_fix'
    elif mode == 'missing_panel':
        (root / 'extra.png').write_bytes((root / 'result.png').read_bytes())
        with (root / 'appendix.tex').open('a') as stream:
            stream.write(r'\includegraphics{figures/extra.png}')
    (root / 'visual_checks.json').write_text(json.dumps({'figures': [review]}))
    runner = WorkflowRunner(SimpleNamespace(project=tmp_path))
    if mode in {'success', 'old_draft'}:
        assert runner._figure_visual_checks(messages) == [review]
    else:
        with pytest.raises(ArtifactValidationError):
            runner._figure_visual_checks(messages)


def test_figure_agent_gets_feedback_and_views_before_finishing(tmp_path, monkeypatch):
    """The real loop sends a failed completion back to the drawing agent."""
    from PIL import Image
    from paper_agent.agent.loop import AgentLoop
    from paper_agent.agent.model_client import ModelClient, ModelResponse, ToolCall
    from paper_agent.tools import builtin_tools

    monkeypatch.setenv('PAPER_AGENT_ENABLE_IMAGE_FIGURE', '0')
    root = tmp_path / 'figures'
    root.mkdir()
    (tmp_path / 'PAPER_PLAN.md').write_text('One measured figure')
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/metrics.json').write_text('{"mae": 0.5}')
    (root / 'latex_includes.tex').write_text(r'\includegraphics{figures/result.png}')
    for name in ('figure_plan.md',):
        (root / name).write_text('Measured result')
    Image.new('RGB', (200, 100), 'white').save(root / 'result.png')
    digest = hashlib.sha256((root / 'result.png').read_bytes()).hexdigest()
    review = dict(asset='figures/result.png', preview='figures/result.png',
                  asset_sha256=digest, preview_sha256=digest, status='passed',
                  findings='Labels and legend are visible with no clipping.')
    (root / 'visual_checks.json').write_text(json.dumps({'figures': [review]}))
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return 'Finished without looking'
        if len(calls) == 2:
            assert 'Completion checks failed' in kwargs['messages'][-1]['content']
            assert 'view_image' in kwargs['messages'][-1]['content']
            return ModelResponse(tool_calls=(ToolCall('inspect', 'view_image',
                                                     {'path': 'figures/result.png'}),))
        assert any(part['type'] == 'image_url' for part in kwargs['messages'][-1]['content'])
        return 'Inspected final image; no layout defects remain'

    loop = AgentLoop(ModelClient(responder=respond), workspace=tmp_path,
                     tools=tuple(t for t in builtin_tools() if t.name == 'view_image'))
    agent = SimpleNamespace(project=tmp_path, last_state=None)

    def go(prompt, *, phase, completion_check):
        def check():
            agent.last_state = loop.last_state
            completion_check()
        return loop.run(prompt, system_prompt='Draw and inspect', phase=phase,
                        completion_check=check)

    agent.go = go
    result = WorkflowRunner(agent)._run_paper_figure(WorkflowState('test'), 'Make figures')
    assert len(calls) == 3
    assert result.metadata['visual_checks'] == [review]


@pytest.mark.parametrize('mode', ['success', 'below_limit', 'missing_source', 'changed',
                                 'unviewed', 'missing_include'])
def test_code_figure_fallback_requires_failures_artifacts_and_visual_review(tmp_path, monkeypatch, mode):
    from PIL import Image
    from paper_agent.mcp.image_client import ImageMCPClient

    monkeypatch.setenv('PAPER_AGENT_ENABLE_IMAGE_FIGURE', 'true')
    calls = []

    def fail(self, prompt, **kwargs):
        calls.append(prompt)
        raise RuntimeError('upstream timeout')

    monkeypatch.setattr(ImageMCPClient, 'generate_start', fail)
    (tmp_path / 'PAPER_PLAN.md').write_text('Architecture and measured plots')
    (tmp_path / 'results').mkdir()
    (tmp_path / 'results/metrics.json').write_text('{"mae": 0.5}')
    root = tmp_path / 'figures'
    root.mkdir()
    png = 'figures/custom_architecture_preview.png'
    pdf = 'figures/figure_1.pdf'
    source = 'figures/draw_figure_1.py'
    Image.new('RGB', (200, 100), 'white').save(tmp_path / png)
    (tmp_path / pdf).write_bytes(b'%PDF-test-diagram')
    (tmp_path / source).write_text('import matplotlib.pyplot as plt\n')

    def digest(path):
        return hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()

    manifest = dict(backend='code', source_path=source, pdf_path=pdf, output_path=png,
                    source_sha256=digest(source), pdf_sha256=digest(pdf), sha256=digest(png))
    (root / 'figure_1_fallback.json').write_text(json.dumps(manifest))
    (root / 'figure_manifest.json').write_text(json.dumps({'figure_1': {'asset': pdf, 'preview': png}}))
    review = dict(asset=pdf, preview=png, asset_sha256=digest(pdf), preview_sha256=digest(png),
                  status='passed', findings='Architecture labels and arrows are visible.')
    (root / 'visual_checks.json').write_text(json.dumps({'figures': [review]}))
    (root / 'latex_includes.tex').write_text('No figure' if mode == 'missing_include'
                                            else r'\includegraphics{figures/figure_1.pdf}')
    for name in ('figure_plan.md',):
        (root / name).write_text('Architecture diagram')
    messages = []
    for _ in range(4 if mode == 'below_limit' else 5):
        result = asyncio.run(invoke('generate_image', ToolContext(tmp_path), prompt='Architecture'))
        messages.append(dict(role='tool', name='generate_image', is_error=result.is_error, content=result.output))
    if mode != 'unviewed':
        result = asyncio.run(invoke('view_image', ToolContext(tmp_path), path=png))
        messages.append(dict(role='tool', name='view_image', is_error=False, content=result.output))
    if mode == 'missing_source':
        (tmp_path / source).unlink()
    elif mode == 'changed':
        Image.new('RGB', (200, 100), 'red').save(tmp_path / png)
    agent = SimpleNamespace(project=tmp_path,
                            go=lambda *args, **kwargs: SimpleNamespace(iterations=6, messages=messages))
    runner = WorkflowRunner(agent)
    if mode == 'success':
        result = runner._run_paper_figure(WorkflowState('test'), 'Write paper')
        assert result.metadata['image_backend'] == 'code'
        assert result.metadata['images'][0]['failure_count'] == 5
        assert result.metadata['visual_checks'] == [review]
    else:
        with pytest.raises(RuntimeError):
            runner._run_paper_figure(WorkflowState('test'), 'Write paper')
    assert len(calls) == (4 if mode == 'below_limit' else 5)
