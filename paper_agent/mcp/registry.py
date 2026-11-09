"""工作台 MCP 配置、加密凭据与阶段快照；不保存活动连接。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from jsonschema import Draft202012Validator

from paper_agent.api.store import connection, now
from paper_agent.tools.profiles import PHASE_TOOL_NAMES

PHASES = {
    key: label
    for key, label in {
        "data_search": "数据检索",
        "literature": "文献研究",
        "ideas": "创意生成",
        "novelty": "创新性检查",
        "research_refine": "研究精炼",
        "experiment_plan": "实验规划",
        "paper_plan": "论文规划",
        "paper_figure": "论文绘图",
        "paper_write": "论文写作",
        "paper_compile": "编译交付",
        "improvement": "审阅改进",
    }.items()
    if key in PHASE_TOOL_NAMES
}
_run = ContextVar("mcp_run", default=None)


class Conflict(ValueError):
    pass


class MCPError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def initialize():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_connections (
            id TEXT PRIMARY KEY, document TEXT NOT NULL, secret TEXT,
            revision INTEGER NOT NULL, catalog TEXT NOT NULL DEFAULT '[]',
            protocol TEXT, discovered_at TEXT, status TEXT NOT NULL DEFAULT 'untested',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mcp_phase_snapshots (
            run_id TEXT NOT NULL, phase TEXT NOT NULL, document TEXT NOT NULL,
            PRIMARY KEY(run_id,phase)
        );
        CREATE TABLE IF NOT EXISTS mcp_calls (
            id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, run_id TEXT,
            phase TEXT, workspace TEXT, tool TEXT NOT NULL, status TEXT NOT NULL,
            duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        """)


def cipher():
    key = os.environ.get("PAPER_AGENT_MCP_CREDENTIAL_KEY", "")
    path = os.environ.get("PAPER_AGENT_MCP_CREDENTIAL_KEY_FILE")
    if not key and path:
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise MCPError(
                "CREDENTIAL_STORE_UNAVAILABLE", "MCP 凭据密钥文件不可读"
            ) from None
    if not key:
        raise MCPError(
            "CREDENTIAL_STORE_UNAVAILABLE", "请先配置 MCP 凭据加密密钥，再保存服务密钥"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise MCPError(
            "CREDENTIAL_STORE_UNAVAILABLE", "MCP 凭据加密密钥格式不正确"
        ) from None


def credential_ready():
    try:
        cipher()
        return True
    except MCPError:
        return False


def _row(identifier, db):
    row = db.execute(
        "SELECT * FROM mcp_connections WHERE id=?", (identifier,)
    ).fetchone()
    if row is None:
        raise KeyError(identifier)
    return dict(row)


def view(row):
    value = json.loads(row["document"])
    return {
        **value,
        "id": row["id"],
        "revision": row["revision"],
        "etag": fingerprint([row["id"], row["revision"]]),
        "has_credential": bool(row["secret"]),
        "tools": json.loads(row["catalog"]),
        "protocol": row["protocol"],
        "discovered_at": row["discovered_at"],
        "status": row["status"],
        "updated_at": row["updated_at"],
    }


def get(identifier):
    initialize()
    with connection() as db:
        return view(_row(identifier, db))


def catalog():
    initialize()
    with connection() as db:
        return [
            view(dict(row))
            for row in db.execute(
                "SELECT * FROM mcp_connections ORDER BY updated_at DESC"
            )
        ]


def save(
    document, identifier=None, expected_etag=None, *, secret_action="keep", secret=None
):
    from .network import validate_endpoint

    document = dict(document)
    if document["allowed_tools"] and not document["read_only_confirmed"]:
        raise ValueError("启用工具前，请确认仅用于读取或查询")
    document["name"] = document["name"].strip()
    if not document["name"]:
        raise ValueError("服务名称不能为空")
    document["url"] = validate_endpoint(document["url"])
    if set(document["phases"]) - PHASES.keys():
        raise ValueError("包含不支持的研究阶段")
    if secret_action not in {"keep", "replace", "clear"}:
        raise ValueError("未知的凭据操作")
    encrypted = None
    if secret_action == "replace":
        if (
            not secret
            or not secret.strip()
            or len(secret) > 8192
            or any(c in secret for c in "\r\n")
        ):
            raise ValueError("请填写有效的单行服务密钥")
        encrypted = cipher().encrypt(secret.encode()).decode()
    initialize()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = _row(identifier, db) if identifier else None
        if old and fingerprint([identifier, old["revision"]]) != expected_etag:
            raise Conflict("配置已在其他页面更新；输入已保留，请加载最新配置后合并")
        if old is None:
            identifier = uuid.uuid4().hex
        old_doc = json.loads(old["document"]) if old else {}
        identity_changed = any(
            old_doc.get(k) != document[k] for k in ("url", "auth_type", "header_name")
        )
        secret_changed = secret_action != "keep"
        existing_tools = (
            json.loads(old["catalog"])
            if old and not identity_changed and not secret_changed
            else []
        )
        available = {tool["name"] for tool in existing_tools if tool["compatible"]}
        if set(document["allowed_tools"]) - available:
            raise ValueError("工具目录或认证已变化，请先保存连接并重新发现工具")
        stored_secret = (
            encrypted
            if secret_action == "replace"
            else (old["secret"] if old and secret_action == "keep" else None)
        )
        if document["auth_type"] == "none":
            stored_secret = None
        # 控制进入模型上下文的目录总量，不因服务数量增长而无限注入 schema。
        for phase in document["phases"] if document["enabled"] else []:
            active = [
                t for t in existing_tools if t["name"] in document["allowed_tools"]
            ]
            for other in db.execute(
                "SELECT * FROM mcp_connections WHERE id!=?", (identifier,)
            ):
                other_doc = json.loads(other["document"])
                if other_doc["enabled"] and phase in other_doc["phases"]:
                    active.extend(
                        t
                        for t in json.loads(other["catalog"])
                        if t["name"] in other_doc["allowed_tools"]
                    )
            if len(active) > 40 or len(dump(active)) > 80000:
                raise ValueError(
                    "该阶段已授权工具超过 40 个或目录 80,000 字符，请减少所选工具"
                )
        revision = old["revision"] + 1 if old else 1
        reset = identity_changed or secret_changed
        db.execute(
            """INSERT OR REPLACE INTO mcp_connections
            (id,document,secret,revision,catalog,protocol,discovered_at,status,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                dump(document),
                stored_secret,
                revision,
                dump(existing_tools),
                old["protocol"] if old and not reset else None,
                old["discovered_at"] if old and not reset else None,
                old["status"] if old and not reset else "untested",
                now(),
            ),
        )
        return view(_row(identifier, db))


def remove(identifier, expected_etag):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(identifier, db)
        if view(row)["etag"] != expected_etag:
            raise Conflict("配置已更新，请刷新后删除")
        db.execute("DELETE FROM mcp_connections WHERE id=?", (identifier,))


def headers(identifier):
    with connection() as db:
        row = _row(identifier, db)
    doc = json.loads(row["document"])
    if doc["auth_type"] == "none":
        return {}
    if not row["secret"]:
        raise MCPError("AUTH_REQUIRED", "请为该连接配置专用服务密钥")
    try:
        secret = cipher().decrypt(row["secret"].encode()).decode()
    except InvalidToken:
        raise MCPError(
            "CREDENTIAL_STORE_UNAVAILABLE", "无法解密服务密钥，请核对加密配置"
        ) from None
    if doc["auth_type"] == "bearer":
        return {"Authorization": "Bearer " + secret}
    return {doc["header_name"]: secret}


def check_schema(schema):
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or len(dump(schema)) > 16000
    ):
        raise ValueError("首版仅支持大小不超过 16 KB 的对象参数结构")

    def walk(value, depth=0):
        if depth > 16:
            raise ValueError("参数结构嵌套过深")
        if isinstance(value, dict):
            if any(
                key in value
                for key in (
                    "$ref",
                    "$dynamicRef",
                    "x-mcp-header",
                    "pattern",
                    "patternProperties",
                )
            ):
                raise ValueError("首版不支持引用、正则表达式或参数映射请求头的结构")
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, depth + 1)

    walk(schema)
    Draft202012Validator.check_schema(schema)


def normalized_tool(tool):
    raw = tool.model_dump(by_alias=True, exclude_none=True)
    name = raw.get("name", "")
    compatible, reason = True, ""
    try:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", name):
            raise ValueError("工具名格式不支持")
        if len(raw.get("description", "")) > 4000:
            raise ValueError("工具描述过长")
        check_schema(raw.get("inputSchema"))
        if raw.get("annotations", {}).get("destructiveHint") is True:
            raise ValueError("首版不支持声明为破坏性操作的工具")
    except Exception as exc:
        compatible, reason = False, str(exc)[:160]
    return {
        "name": name,
        "description": raw.get("description", ""),
        "input_schema": raw.get("inputSchema", {}),
        "annotations": raw.get("annotations", {}),
        "fingerprint": fingerprint(raw),
        "compatible": compatible,
        "reason": reason,
    }


def publish(identifier, expected_etag, tools, protocol):
    if len({t["name"] for t in tools}) != len(tools):
        raise ValueError("服务返回重复工具名，未更新目录")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(identifier, db)
        if view(row)["etag"] != expected_etag:
            raise Conflict("发现期间配置已变化，未覆盖工具目录，请重试")
        old_tools = {t["name"]: t["fingerprint"] for t in json.loads(row["catalog"])}
        doc = json.loads(row["document"])
        # 只有定义完全相同的工具保留授权；新增和变更工具必须重新选择。
        doc["allowed_tools"] = [
            t["name"]
            for t in tools
            if t["compatible"]
            and t["name"] in doc["allowed_tools"]
            and old_tools.get(t["name"]) == t["fingerprint"]
        ]
        db.execute(
            """UPDATE mcp_connections SET document=?,catalog=?,protocol=?,discovered_at=?,
            status='available',revision=revision+1,updated_at=? WHERE id=?""",
            (dump(doc), dump(tools), protocol, now(), now(), identifier),
        )
        return view(_row(identifier, db))


def mark_failure(identifier, revision, code):
    with connection() as db:
        db.execute(
            "UPDATE mcp_connections SET status=? WHERE id=? AND revision=?",
            (code, identifier, revision),
        )


def snapshot(phase):
    initialize()
    run_id = _run.get()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if run_id:
            row = db.execute(
                "SELECT document FROM mcp_phase_snapshots WHERE run_id=? AND phase=?",
                (run_id, phase),
            ).fetchone()
            if row:
                return json.loads(row[0])
        items = []
        for row in db.execute("SELECT * FROM mcp_connections"):
            item = view(dict(row))
            if item["enabled"] and phase in item["phases"] and item["allowed_tools"]:
                item.pop("has_credential", None)
                item["tools"] = [
                    t for t in item["tools"] if t["name"] in item["allowed_tools"]
                ]
                items.append(item)
        if run_id:
            db.execute(
                "INSERT INTO mcp_phase_snapshots VALUES(?,?,?)",
                (run_id, phase, dump(items)),
            )
        return items


@contextmanager
def use_run(identifier):
    token = _run.set(identifier)
    try:
        yield
    finally:
        _run.reset(token)


def reserve_call(identifier, phase, workspace, tool):
    run_id = _run.get()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            run_id
            and db.execute(
                "SELECT count(*) FROM mcp_calls WHERE run_id=? AND phase=?",
                (run_id, phase),
            ).fetchone()[0]
            >= 6
        ):
            raise MCPError("BUDGET_EXHAUSTED", "本阶段 MCP 调用已达到 6 次限额")
        audit_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO mcp_calls VALUES(?,?,?,?,?,?,?,?,?)",
            (
                audit_id,
                identifier,
                run_id,
                phase,
                str(workspace),
                tool,
                "running",
                0,
                now(),
            ),
        )
        return audit_id


def audit(identifier, phase, workspace, tool, status, duration_ms, audit_id=None):
    with connection() as db:
        if audit_id:
            db.execute(
                "UPDATE mcp_calls SET status=?,duration_ms=? WHERE id=?",
                (status, duration_ms, audit_id),
            )
            return
        db.execute(
            "INSERT INTO mcp_calls VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                identifier,
                _run.get(),
                phase,
                str(workspace),
                tool,
                status,
                duration_ms,
                now(),
            ),
        )


def calls(identifier):
    with connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM mcp_calls WHERE connection_id=? ORDER BY created_at DESC LIMIT 50",
                (identifier,),
            )
        ]
