"""MCP Streamable HTTP JSON client for the stateless BTS Windows bridge.

Uses existing httpx dependency. This client deliberately requires JSON responses;
it does not claim to support arbitrary SSE/stateful MCP servers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def configuration():
    path = Path(os.environ.get('BTS_MCP_CONFIG', '~/.config/paper-agent/bts-mcp.json')).expanduser()
    config = json.loads(path.read_text(encoding='utf-8'))
    url = config['url']
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username:
        raise ValueError('BTS endpoint must use the loopback SSH tunnel')
    return url, config['token']


async def call_bts(name: str, arguments: dict) -> dict:
    url, token = configuration()
    headers = {'Authorization': 'Bearer ' + token,
               'Accept': 'application/json, text/event-stream'}
    # Never retry a tool call automatically: the result may have been lost after execution.
    async with httpx.AsyncClient(headers=headers, timeout=90, trust_env=False) as client:
        async def rpc(method, params, request_id=None):
            body = {'jsonrpc': '2.0', 'method': method, 'params': params}
            if request_id is not None:
                body['id'] = request_id
            try:
                response = await client.post(url, json=body)
            except httpx.TransportError:
                raise RuntimeError('BTS MCP transport failed; outcome may be unknown. Reuse the operation idempotency key.') from None
            if response.status_code >= 400:
                # Do not include request headers / credentials in exception text.
                raise RuntimeError(f'BTS MCP HTTP {response.status_code}')
            if request_id is None:
                return None
            if 'application/json' not in response.headers.get('content-type', ''):
                raise RuntimeError('This adapter requires MCP JSON responses')
            data = response.json()
            if data.get('id') != request_id or data.get('jsonrpc') != '2.0':
                raise RuntimeError('Mismatched MCP response')
            if 'error' in data:
                raise RuntimeError('BTS MCP protocol error: ' + str(data['error'].get('message', 'unknown')))
            return data['result']

        init = await rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                         'clientInfo': {'name': 'paperagent-bts', 'version': '0.1.0'}}, 1)
        version = init['protocolVersion']
        if version not in ('2025-03-26', '2025-06-18', '2025-11-25'):
            raise RuntimeError('Unsupported MCP protocol version')
        client.headers['MCP-Protocol-Version'] = version
        await rpc('notifications/initialized', {})
        return await rpc('tools/call', {'name': name, 'arguments': arguments}, 2)
