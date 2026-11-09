"""Single-workbench MCP management; no arbitrary command execution endpoint."""

import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from paper_agent.mcp import registry, runtime


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # Pydantic 默认错误可包含整个输入对象，其中可能有服务密钥。
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "配置格式不正确：请核对必填字段、认证头名称，并确认所选工具仅用于读取或查询"
                    },
                )

        return handler


router = APIRouter(prefix="/api/mcp", tags=["mcp"], route_class=SecretSafeRoute)


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2048)
    enabled: bool = True
    auth_type: Literal["none", "bearer", "api_key"] = "none"
    header_name: str = "X-API-Key"
    phases: list[str] = Field(default_factory=list, max_length=20)
    allowed_tools: list[str] = Field(default_factory=list, max_length=40)
    read_only_confirmed: bool = False
    secret_action: Literal["keep", "replace", "clear"] = "keep"
    secret: str | None = Field(default=None, max_length=8192, repr=False)

    @model_validator(mode="after")
    def validate_policy(self):
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", self.header_name)
            or self.header_name.lower()
            in {
                "host",
                "cookie",
                "content-type",
                "content-length",
                "authorization",
                "connection",
                "accept",
                "origin",
                "referer",
                "transfer-encoding",
                "accept-encoding",
            }
            or self.header_name.lower().startswith(("mcp-", "proxy-", "sec-"))
        ):
            raise ValueError("API Key 请求头名称不允许")
        if self.allowed_tools and not self.read_only_confirmed:
            raise ValueError("启用工具前，请确认所选工具仅用于查询或读取")
        return self


class Update(Document):
    expected_etag: str = Field(min_length=64, max_length=64)


class Version(BaseModel):
    expected_etag: str = Field(min_length=64, max_length=64)


def boundary(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except registry.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "MCP 连接不存在") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("")
def list_connections():
    return {
        "items": registry.catalog(),
        "phases": registry.PHASES,
        "scope": "workbench",
        "credential_ready": registry.credential_ready(),
        "activation": "next_phase_start",
    }


@router.post("", status_code=201)
def create(body: Document):
    return boundary(
        registry.save,
        body.model_dump(exclude={"secret", "secret_action"}),
        secret_action=body.secret_action,
        secret=body.secret,
    )


@router.get("/{identifier}")
def get(identifier: str):
    return boundary(registry.get, identifier)


@router.put("/{identifier}")
def update(identifier: str, body: Update):
    return boundary(
        registry.save,
        body.model_dump(exclude={"secret", "secret_action", "expected_etag"}),
        identifier,
        body.expected_etag,
        secret_action=body.secret_action,
        secret=body.secret,
    )


@router.post("/{identifier}/discover")
async def discover(identifier: str, body: Version):
    try:
        return await runtime.discover(identifier, body.expected_etag)
    except registry.Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except KeyError:
        raise HTTPException(404, "MCP 连接不存在") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.delete("/{identifier}")
def delete(identifier: str, body: Version):
    boundary(registry.remove, identifier, body.expected_etag)
    return {"deleted": True}


@router.get("/{identifier}/calls")
def calls(identifier: str):
    boundary(registry.get, identifier)
    return {"items": registry.calls(identifier)}
