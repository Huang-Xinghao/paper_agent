"""Regression checks for bounded reads, shell failures and shared rate limits."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from batteryscientist.agent.loop import tool_schema
from batteryscientist.tools.core import ToolContext, ToolResult, bash, builtin_tools, read
from batteryscientist.tools.literature import search_papers
from batteryscientist.tools.profiles import tools_for_phase
from batteryscientist.tools.wiki.ingest import WikiIngestion


class ToolCallContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ctx = ToolContext(self.root)

    def test_long_unicode_line_is_bounded_and_can_be_reconstructed(self):
        original = ['甲"\\' * 12000, '', 'last line']
        (self.root / 'large.json').write_text('\n'.join(original), encoding='utf-8')
        reconstructed = ['', '', '']
        offset = column = 0
        for _ in range(2000):
            result = read(self.ctx, 'large.json', offset=offset, char_offset=column, max_chars=256)
            self.assertFalse(result.is_error)
            self.assertLessEqual(len(result.output), 256)
            body = result.output.rsplit('\n[read_cursor ', 1)[0]
            for line in body.splitlines():
                number, text = line.split(': ', 1)
                reconstructed[int(number) - 1] += text
            offset = result.metadata['next_offset']
            column = result.metadata['char_offset']
            if offset is None:
                break
        else:
            self.fail('Read cursor did not reach EOF')
        self.assertEqual(reconstructed, original)

    def test_line_pagination_remains_compatible(self):
        (self.root / 'small.txt').write_text('a\nb\nc')
        result = read(self.ctx, 'small.txt', offset=1, limit=1)
        self.assertTrue(result.output.startswith('2: b\n'))
        self.assertEqual(result.metadata['next_offset'], 2)
        self.assertEqual(result.metadata['char_offset'], 0)

    def test_bash_expansion_and_error_propagation(self):
        async def check():
            expanded = await bash(self.ctx, "printf '%s ' {one,two}")
            self.assertFalse(expanded.is_error)
            self.assertEqual(expanded.output, 'one two ')
            for command in ['false; echo hidden', 'false | cat; echo hidden']:
                result = await bash(self.ctx, command)
                self.assertTrue(result.is_error)
                self.assertNotIn('hidden', result.output)
            expected = await bash(self.ctx, 'if false; then echo bad; else echo expected; fi')
            self.assertFalse(expected.is_error)
        asyncio.run(check())

    def test_search_bounds_large_metadata(self):
        paper = {'title': 'Example', 'abstract': 'a' * 90000,
                 'authors': [{'name': 'author' * 100} for _ in range(5000)]}
        with patch('batteryscientist.tools.literature._get_json', return_value={'data': [paper] * 10}):
            result = search_papers(self.ctx, 'Example', source='semantic_scholar')
        self.assertLessEqual(len(result.output), 12000)
        value = json.loads(result.output)
        self.assertTrue(value['truncated'])
        self.assertGreater(value['returned_count'], 0)
        for item in value['papers']:
            self.assertEqual(len(item['authors']), 8)
            self.assertEqual(item['author_count'], 5000)
            self.assertEqual(len(item['abstract']), 1200)

    def test_rate_limit_shared_across_contexts_and_sources_independent(self):
        module = 'batteryscientist.tools.literature'
        with patch(module + '._search_papers', side_effect=HTTPError('url', 429, 'limited', {}, None)) as request:
            self.assertTrue(search_papers(self.ctx, 'first').is_error)
            self.assertTrue(search_papers(self.ctx, 'second').is_error)
            third = search_papers(ToolContext(self.root), 'third')
            self.assertTrue(third.is_error)
            self.assertGreater(json.loads(third.output)['retry_after_seconds'], 0)
            self.assertEqual(request.call_count, 2)
        with patch(module + '._search_papers', return_value=ToolResult('{}')) as request:
            self.assertFalse(search_papers(self.ctx, 'other', source='arxiv').is_error)
            state = self.root / '.batteryscientist/search-cooldown/openalex.json'
            data = json.loads(state.read_text())
            data['until'] = data['updated'] = 0
            state.write_text(json.dumps(data))
            self.assertFalse(search_papers(self.ctx, 'expired').is_error)
            self.assertEqual(request.call_count, 2)

    def test_success_resets_consecutive_rate_limit_failures(self):
        module = 'batteryscientist.tools.literature'
        error = HTTPError('url', 429, 'limited', {}, None)
        with patch(module + '._search_papers', side_effect=[error, ToolResult('{}'), error, ToolResult('{}')]) as request:
            for i in range(4):
                search_papers(self.ctx, str(i))
            self.assertEqual(request.call_count, 4)

    def test_tool_schemas_and_experiment_permissions(self):
        specs = {s.name: s for s in builtin_tools()}
        params = tool_schema(specs['read'])['function']['parameters']['properties']
        self.assertEqual(params['max_chars']['maximum'], 12000)
        self.assertEqual(params['offset']['minimum'], 0)
        wiki = WikiIngestion.__new__(WikiIngestion)
        spec = next(s for s in wiki.tools() if s.name == 'wiki_read_source')
        self.assertEqual(tool_schema(spec)['function']['parameters']['properties']['limit']['maximum'], 40000)
        for phase in ['experiment_plan', 'experiment_bridge']:
            names = {s.name for s in tools_for_phase(phase, tuple(specs.values()))}
            self.assertIn('inspect_compute', names)


if __name__ == '__main__':
    unittest.main()
