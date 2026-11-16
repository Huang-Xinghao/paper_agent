"""Unified resource discovery and real phase isolation, without model/API calls."""
import json
import pickle
from types import SimpleNamespace

import pytest

from paper_agent.agent.loop import PhaseAgent
from paper_agent.integrations.model import ModelResponse, ToolCall
from paper_agent.tools import builtin_tools
from paper_agent.tools import battery_resources as resources
from paper_agent.tools.core import ToolContext


OLD_TOOLS = {'search_battery_resources', 'read_battery_resource',
             'list_battery_data_sources', 'inspect_battery_data'}


@pytest.fixture
def library(tmp_path):
    root = tmp_path / 'resources'
    data = tmp_path / 'datasets'
    data.mkdir()
    sample = data / 'labels.json'
    sample.write_text(json.dumps({'cell_A': {'life': 100, 'cycles': [1, 2, 3]}}))
    resource_id = '电芯/数据集/Tongji_README'
    document = root / (resource_id + '.md')
    document.parent.mkdir(parents=True)
    document.write_text('# Tongji lifetime\n- 数据集根路径：`' + str(data) + '`\n'
                        + '\n'.join(f'line {i}' for i in range(250)))
    other = root / '电芯/数据集/Other.md'
    other.write_text('# Other lifetime\n- 数据集根路径：`' + str(data / 'missing') + '`\n')
    (root / 'data_sources.json').write_text(json.dumps({'sources': [
        {'resource_id': resource_id, 'kind': 'processed', 'path': str(data)},
        {'resource_id': 'ManifestOnly', 'kind': 'raw', 'path': str(tmp_path / 'manifest-only')},
    ]}))
    workspace = tmp_path / 'project'
    workspace.mkdir()
    return SimpleNamespace(root=root, data=data, sample=sample, resource_id=resource_id,
                           workspace=workspace, ctx=ToolContext(workspace, knowledge_roots=(root,)))


def decoded(result):
    assert not result.is_error, result.output
    return json.loads(result.output)


def test_search_and_read_deliver_paths_without_inspecting_data(library, monkeypatch):
    def forbid_samples(*args, **kwargs):
        raise AssertionError('Search/read must not inspect real samples')
    monkeypatch.setattr(resources, 'inspect_data_file', forbid_samples)
    result = decoded(resources.battery_resources(library.ctx, 'search', query='Tongji',
                                                resource_types=['dataset']))
    assert len(result['resources']) == 1
    item = result['resources'][0]
    assert item['resource_id'] == library.resource_id
    assert item['matched_sections']
    assert len(item['data_sources']) == 1  # manifest + README registration deduplicated
    assert item['data_sources'][0]['path'] == str(library.data)
    assert item['data_sources'][0]['exists']

    def forbid_reindex(*args):
        raise AssertionError('Reading a known ID must not reindex all descriptions')
    monkeypatch.setattr(resources, '_index', forbid_reindex)
    first = decoded(resources.battery_resources(library.ctx, 'read', resource_id=library.resource_id))
    second = decoded(resources.battery_resources(library.ctx, 'read', resource_id=library.resource_id,
                                                offset=first['next_offset']))
    assert first['next_offset'] == 200 and first['total_lines'] == 252
    assert second['content'].startswith('201: ')
    assert 'line 249' in second['content'] and second['next_offset'] is None
    assert first['data_sources'] == item['data_sources']


def test_catalog_and_source_pagination_are_stable(library):
    first = decoded(resources.battery_resources(library.ctx, 'search', query='lifetime', top_k=1))
    second = decoded(resources.battery_resources(library.ctx, 'search', query='lifetime', top_k=1,
                                                offset=first['next_offset']))
    assert first['total'] == first['total_sources'] == 2
    assert second['total_sources'] == 2 and second['next_offset'] is None
    assert first['sources'][0] != second['sources'][0]
    assert any(not source['exists'] for source in first['sources'] + second['sources'])
    manifest = decoded(resources.battery_resources(library.ctx, 'search', query='ManifestOnly'))
    assert not manifest['resources']
    assert manifest['sources'][0]['resource_id'] == 'ManifestOnly'
    assert not manifest['sources'][0]['exists']
    catalog = decoded(resources.battery_resources(library.ctx, 'search'))
    assert catalog['total_sources'] == 3
    methods = decoded(resources.battery_resources(library.ctx, 'search', resource_types=['method']))
    assert not methods['resources'] and not methods['sources']


@pytest.mark.parametrize('suffix', ['json', 'csv', 'pkl'])
def test_inspect_registered_samples_without_changing_source(library, suffix):
    path = library.data / ('sample.' + suffix)
    if suffix == 'json':
        path.write_text(json.dumps({'labels': [100, 200]}))
    elif suffix == 'csv':
        path.write_text('cell,life\nA,100\nB,200\nC,300\n')
    else:
        path.write_bytes(pickle.dumps({'labels': [100, 200]}))
    original = path.read_bytes()
    result = decoded(resources.battery_resources(library.ctx, 'inspect', path=str(path)))
    assert result['path'] == str(path) and result['bytes'] == len(original)
    fields = result['summary']['fields']
    if suffix == 'csv':
        assert fields['row_count'] == 3
        assert fields['sample_rows']['length'] == 2
    else:
        assert fields['labels']['length'] == 2
        assert fields['labels']['samples'] == {'0': 100, '1': 200}
    assert path.read_bytes() == original


def test_invalid_actions_paths_and_resource_escapes(library, tmp_path):
    outside = tmp_path / 'outside.json'
    outside.write_text('{"private": true}')
    (library.root / 'escape.md').symlink_to(outside)
    (library.data / 'escape.json').symlink_to(outside)
    calls = [
        {'action': 'unknown'}, {'action': 'read'}, {'action': 'inspect'},
        {'action': 'search', 'path': str(library.sample)},
        {'action': 'search', 'resource_types': ['invalid']},
        {'action': 'search', 'offset': -1},
        {'action': 'read', 'resource_id': '../outside'},
        {'action': 'read', 'resource_id': 'escape'},
        {'action': 'inspect', 'path': str(outside)},
        {'action': 'inspect', 'path': str(library.data / 'escape.json')},
    ]
    for arguments in calls:
        assert resources.battery_resources(library.ctx, **arguments).is_error, arguments
    catalog = decoded(resources.battery_resources(library.ctx, 'search'))
    assert 'escape' not in {item['resource_id'] for item in catalog['resources']}


def test_literature_cannot_call_resource_tool_or_read_external_data(library):
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            names = {item['function']['name'] for item in tools}
            assert not ({'battery_resources', 'bash', 'ls'} | OLD_TOOLS) & names
            if self.calls == 1:
                return ModelResponse(tool_calls=(
                    ToolCall('unavailable', 'battery_resources', {'action': 'search', 'query': 'Tongji'}),
                    ToolCall('read', 'read', {'path': str(library.sample)}),
                    ToolCall('find', 'find', {'path': str(library.data), 'pattern': '*.json'}),
                ))
            results = [message for message in messages if message['role'] == 'tool']
            assert len(results) == 3 and all(message['is_error'] for message in results)
            assert 'Tool not available in phase literature' in results[0]['content']
            return ModelResponse(content='继续文献调研。')

    agent = PhaseAgent(library.workspace, model_client=Model(), phase='literature',
                       knowledge_root=library.root)
    agent.connect_mcp = False
    agent.run('Only research the literature', system_prompt=agent._system_prompt(),
              phase='literature', completion_check=lambda: None)
    assert agent.knowledge_roots == ()
    agent.configure(phase='data_search')
    assert agent.knowledge_roots == (library.root,)
    assert 'battery_resources' in {spec.name for spec in agent.tools}
    agent.configure(phase='literature')
    assert agent.knowledge_roots == ()
    assert 'battery_resources' not in {spec.name for spec in agent.tools}


def test_data_agent_search_read_inspect_and_deliver(library):
    class Model:
        calls = 0
        def complete(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                schema = next(item['function'] for item in tools
                              if item['function']['name'] == 'battery_resources')
                assert schema['parameters']['properties']['action']['enum'] == ['search', 'read', 'inspect']
                arguments = {'action': 'search', 'query': 'Tongji'}
            elif self.calls == 2:
                found = json.loads(messages[-1]['content'])['resources'][0]
                arguments = {'action': 'read', 'resource_id': found['resource_id']}
            elif self.calls == 3:
                read = json.loads(messages[-1]['content'])
                arguments = {'action': 'inspect', 'path': read['data_sources'][0]['path'] + '/labels.json'}
            elif self.calls == 4:
                inspected = json.loads(messages[-1]['content'])
                assert inspected['summary']['fields']['cell_A']['fields']['life'] == 100
                return ModelResponse(tool_calls=(ToolCall('report', 'write', {
                    'path': 'data-search/DATA_REPORT.md', 'content': 'Observed cell_A life=100.',
                }),))
            else:
                return ModelResponse(content='数据报告已保存。')
            return ModelResponse(tool_calls=(ToolCall(str(self.calls), 'battery_resources', arguments),))

    agent = PhaseAgent(library.workspace, model_client=Model(), phase='data_search',
                       knowledge_root=library.root)
    agent.connect_mcp = False
    report = library.workspace / 'data-search/DATA_REPORT.md'
    def completed():
        assert report.read_text() == 'Observed cell_A life=100.'
    agent.run('Understand Tongji', system_prompt=agent._system_prompt(), phase='data_search',
              completion_check=completed)
    assert agent.model.calls == 5


def test_old_tools_are_removed_and_main_uses_unified_entry(library):
    from paper_agent.agent.coordinator import CoordinatorAgent
    assert not OLD_TOOLS & {spec.name for spec in builtin_tools()}
    main = CoordinatorAgent(library.workspace, model_client=object(), project_goal='Research')
    names = {spec.name for spec in main.tools}
    assert 'battery_resources' in names and not OLD_TOOLS & names
