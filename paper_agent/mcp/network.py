"""只连接已校验地址；DNS 校验和拨号使用同一组 IP，禁止重定向与环境代理。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

import httpcore2
import httpx2
from httpcore2._backends.anyio import AnyIOBackend

from .registry import MCPError

MAX_RESPONSE = 2 * 1024 * 1024


def canonical_url(url):
    try:
        parts = urlsplit(url.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError()
        host = parts.hostname.encode("idna").decode().lower()
        if "%" in host or "\\" in url or any(ord(c) < 33 for c in url):
            raise ValueError()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        authority = f"[{host}]" if ":" in host else host
        default = 443 if parts.scheme == "https" else 80
        if port != default:
            authority += f":{port}"
        return urlunsplit((parts.scheme, authority, parts.path or "/", "", ""))
    except (ValueError, UnicodeError):
        raise MCPError(
            "INVALID_ENDPOINT", "请使用无账号、查询参数及片段的完整 HTTP(S) 服务地址"
        ) from None


def trusted_endpoints():
    try:
        values = json.loads(os.environ.get("PAPER_AGENT_MCP_TRUSTED_ENDPOINTS", "[]"))
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError()
        return {canonical_url(v) for v in values}
    except (ValueError, TypeError):
        raise MCPError(
            "NETWORK_POLICY_INVALID", "管理员配置的 MCP 内网端点列表无效"
        ) from None


def validate_endpoint(url):
    normalized = canonical_url(url)
    parts = urlsplit(normalized)
    trusted = normalized in trusted_endpoints()
    if not trusted and (parts.scheme != "https" or (parts.port or 443) != 443):
        raise MCPError(
            "NETWORK_DENIED",
            "公网 MCP 仅支持 HTTPS 443；内网或其他端口须由管理员登记完整端点",
        )
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if not trusted and address and not address.is_global:
        raise MCPError("NETWORK_DENIED", "该服务地址位于受限网络")
    return normalized


async def resolve_target(url):
    normalized = validate_endpoint(url)
    parts = urlsplit(normalized)
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parts.hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            5,
        )
        addresses = list(dict.fromkeys(record[4][0] for record in records))
    except (OSError, TimeoutError):
        raise MCPError("DNS_FAILED", "无法解析 MCP 服务地址") from None
    trusted = normalized in trusted_endpoints()
    if not addresses or (
        not trusted and any(not ipaddress.ip_address(ip).is_global for ip in addresses)
    ):
        raise MCPError("NETWORK_DENIED", "服务解析到受限网络地址，连接已阻止")
    return normalized, addresses


class PinnedBackend(AnyIOBackend):
    def __init__(self, hostname, addresses):
        self.hostname, self.addresses = hostname, addresses

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if host != self.hostname:
            raise MCPError("NETWORK_DENIED", "连接目标与校验地址不一致")
        # 拨号只使用刚刚校验过的字面 IP；TLS 仍由 httpcore 使用原主机名验证。
        for index, ip in enumerate(self.addresses):
            try:
                return await super().connect_tcp(
                    ip, port, timeout, local_address, socket_options
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout):
                if index == len(self.addresses) - 1:
                    raise


class LimitedStream(httpx2.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        count = 0
        async for chunk in self.stream:
            count += len(chunk)
            if count > MAX_RESPONSE:
                raise MCPError("RESULT_TOO_LARGE", "MCP 响应超过 2 MB 限额")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class SafeTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, url, addresses, before_request=None):
        super().__init__(trust_env=False, retries=0)
        self.url, self.before_request = url, before_request
        # httpx2 暂未暴露 network_backend 参数；将兼容依赖锁定并覆盖底层池。
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=2,
            max_keepalive_connections=2,
            retries=0,
            network_backend=PinnedBackend(urlsplit(url).hostname, addresses),
        )

    async def handle_async_request(self, request):
        if canonical_url(str(request.url)) != self.url:
            raise MCPError("NETWORK_DENIED", "MCP 请求不能跳转或切换端点")
        if self.before_request:
            self.before_request()
        response = await super().handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise MCPError(
                "REDIRECT_DENIED", "服务要求重定向，请核实并填写最终 MCP 地址"
            )
        if response.status_code in {401, 403}:
            await response.aclose()
            raise MCPError(
                "AUTH_REQUIRED",
                "服务拒绝认证，请检查密钥或授权范围；OAuth 服务尚不支持",
            )
        # 禁止压缩，防止小体积压缩包解码成超大响应。
        if response.headers.get("content-encoding", "identity") != "identity":
            await response.aclose()
            raise MCPError(
                "UNSUPPORTED_ENCODING", "服务忽略了 identity 编码要求，响应未读取"
            )
        response.stream = LimitedStream(response.stream)
        return response


async def http_client(url, headers, before_request=None):
    normalized, addresses = await resolve_target(url)
    return httpx2.AsyncClient(
        transport=SafeTransport(normalized, addresses, before_request),
        headers={**headers, "Accept-Encoding": "identity"},
        trust_env=False,
        follow_redirects=False,
        timeout=httpx2.Timeout(30, connect=8),
    )
