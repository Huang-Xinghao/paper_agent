"""Versioned workbench skills. Text never grants tools or changes workflow gates."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .api.store import connection, now

BUILTIN_ROOT = Path(__file__).resolve().parent / "skills"
# 与运行时阶段保持同一映射；方案修订是独立的无工具任务。
SKILL_BY_PHASE = {
    "literature": "research-lit", "data_search": "data-search", "ideas": "idea-creator",
    "novelty": "novelty-check", "research_refine": "research-refine",
    "experiment_plan": "experiment-plan", "experiment_bridge": "experiment-bridge",
    "paper_plan": "paper-plan", "paper_figure": "paper-figure", "paper_write": "paper-write",
    "paper_compile": "paper-compile", "improvement": "auto-paper-improvement-loop",
}
PHASES = {
    "literature": "文献研究", "data_search": "数据检索", "ideas": "创意生成",
    "novelty": "新颖性检查", "research_refine": "研究精炼", "proposal_revision": "方案修订",
    "experiment_plan": "实验规划", "experiment_bridge": "实验执行", "paper_plan": "论文规划",
    "paper_figure": "论文绘图", "paper_write": "论文写作", "paper_compile": "编译交付",
    "improvement": "审阅改进",
}
_active_snapshot = ContextVar("paper_agent_skill_snapshot", default=None)
_active_run = ContextVar("paper_agent_skill_run", default=None)
_UNSET = object()


class SkillConflict(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def initialize():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS skill_library (
            id TEXT PRIMARY KEY, head INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS skill_revisions (
            skill_id TEXT NOT NULL REFERENCES skill_library(id), version INTEGER NOT NULL,
            document TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(skill_id, version)
        );
        CREATE TABLE IF NOT EXISTS skill_run_snapshots (
            run_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS skill_builtin_releases (
            skill_id TEXT NOT NULL, release_id TEXT NOT NULL, document TEXT NOT NULL,
            PRIMARY KEY(skill_id, release_id)
        );
        CREATE TABLE IF NOT EXISTS skill_phase_snapshots (
            run_id TEXT NOT NULL, phase TEXT NOT NULL, snapshot TEXT NOT NULL,
            PRIMARY KEY(run_id, phase)
        );
        """)


def _resources(root):
    result = {}
    for folder in ("references", "templates"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                result[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return result


def _builtins():
    result = {}
    manifest = BUILTIN_ROOT / "release.json"
    release_version = json.loads(manifest.read_text(encoding="utf-8"))["version"] if manifest.is_file() else "unversioned"
    for path in sorted(BUILTIN_ROOT.glob("*/SKILL.md")):
        if path.is_symlink() or not path.resolve().is_relative_to(BUILTIN_ROOT.resolve()):
            continue
        raw = path.read_text(encoding="utf-8")
        frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.S)
        fields = {}
        if frontmatter:
            fields = dict(re.findall(r"^(name|description):\s*(.+)$", frontmatter[1], re.M))
        identifier = path.parent.name
        required = [phase for phase, name in SKILL_BY_PHASE.items() if name == identifier]
        item = {
            "id": identifier, "title": PHASES[required[0]] if required else identifier,
            "description": fields.get("description", identifier).strip('"\''),
            "instructions": raw[frontmatter.end():].lstrip() if frontmatter else raw,
            "enabled": True, "phases": required, "version": 0,
            "updated_at": None, "source": "builtin", "required_phases": required,
        }
        resources = _resources(path.parent)
        # 指纹包含正文和模板；漏递增发布号时，内容变化仍然可识别。
        digest = _hash(_json({"skill": item, "resources": resources}))
        item["builtin_release"] = {"id": f"{release_version}:{digest}", "version": release_version, "digest": digest}
        item["resources"] = resources
        result[identifier] = item
    return result


def _finish(item, builtins):
    item = dict(item)
    base = builtins.get(item["id"])
    item["source"] = "builtin" if base else "custom"
    item["required_phases"] = base["required_phases"] if base else []
    differs = bool(base and any(item[key] != base[key] for key in (
        "title", "description", "instructions", "enabled", "phases")))
    # 旧定制没有基准记录时明确标为未知，不伪造它基于当前发布版。
    follows = bool(base and (item.get("follows_builtin") or (item["version"] == 0)))
    item["customized"] = bool(base and not follows and (differs or item.get("base_release")))
    item["follows_builtin"] = follows
    item["builtin_release"] = base["builtin_release"] if base else None
    item["base_release"] = base["builtin_release"] if follows else item.get("base_release")
    item["update_available"] = bool(base and item["customized"] and item["base_release"] and
                                    item["base_release"]["id"] != base["builtin_release"]["id"])
    item["etag"] = _hash(_json(item))
    item["content_hash"] = _hash(item["instructions"])
    return item


def _catalog(db):
    builtins = _builtins()
    for identifier, item in builtins.items():
        db.execute("INSERT OR IGNORE INTO skill_builtin_releases VALUES(?,?,?)",
                   (identifier, item["builtin_release"]["id"], _json(item)))
    items = {key: {k: v for k, v in item.items() if k != "resources"} for key, item in builtins.items()}
    for row in db.execute("""SELECT l.id,l.deleted,r.document,r.version,r.created_at
            FROM skill_library l JOIN skill_revisions r
            ON r.skill_id=l.id AND r.version=l.head"""):
        if row["deleted"]:
            items.pop(row["id"], None)
            continue
        document = json.loads(row["document"])
        if document.get("follows_builtin") and row["id"] in items:
            document = {**items[row["id"]], "follows_builtin": True}
        items[row["id"]] = {**document, "id": row["id"],
                            "version": row["version"], "updated_at": row["created_at"]}
    return {key: _finish(value, builtins) for key, value in items.items()}


def catalog():
    initialize()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        return _catalog(db)


def _validate(document, base=None):
    document = {key: document[key] for key in ("title", "description", "instructions", "enabled", "phases")}
    for key, limit in (("title", 100), ("description", 1200), ("instructions", 100000)):
        if key != "instructions":
            document[key] = document[key].strip()
        if not document[key].strip() or len(document[key]) > limit:
            raise ValueError(f"{key} 不能为空，且不能超过 {limit} 个字符")
    if len(document["instructions"].encode("utf-8")) > 128 * 1024:
        raise ValueError("Skill 正文不能超过 128 KiB")
    document["phases"] = list(dict.fromkeys(document["phases"]))
    if set(document["phases"]) - set(PHASES):
        raise ValueError("包含未知的适用阶段")
    required = (base or {}).get("required_phases", [])
    if required and (not document["enabled"] or not set(required) <= set(document["phases"])):
        raise ValueError("流程必需 Skill 必须启用并保留默认阶段；可以修改正文或恢复内置版")
    return document


def save(document, *, identifier=None, expected_etag=None, baseline=_UNSET, follow_builtin=False):
    initialize()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        items = _catalog(db)
        if identifier is None:
            if sum(item["source"] == "custom" for item in items.values()) >= 100:
                raise ValueError("当前工作台最多保存 100 个自定义 Skill")
            identifier, current = "user-" + uuid.uuid4().hex[:16], None
        else:
            current = items.get(identifier)
            if not current:
                raise KeyError(identifier)
            if expected_etag != current["etag"]:
                raise SkillConflict("Skill 已被其他页面更新。你的输入已保留，请加载最新版本后再合并保存")
        document = _validate(document, current)
        if current and current["source"] == "builtin":
            document["follows_builtin"] = follow_builtin
            document["base_release"] = (baseline if baseline is not _UNSET else
                                        current["base_release"] if current["customized"] else
                                        current["builtin_release"])
        # 全文加载只用于明确绑定，限制总量，防止误把整个技能库塞进一个阶段。
        for phase in document["phases"]:
            total = sum(len(item["instructions"].encode("utf-8")) for key, item in items.items()
                        if key != identifier and item["enabled"] and phase in item["phases"])
            if document["enabled"] and total + len(document["instructions"].encode("utf-8")) > 256 * 1024:
                raise ValueError(f"{PHASES[phase]} 绑定的 Skill 正文总量不能超过 256 KiB")
        version = current["version"] + 1 if current else 1
        db.execute("INSERT INTO skill_library(id,head) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET head=excluded.head",
                   (identifier, version))
        db.execute("INSERT INTO skill_revisions VALUES(?,?,?,?)", (identifier, version, _json(document), now()))
        return _catalog(db)[identifier]


def history(identifier):
    items = catalog()
    if identifier not in items:
        raise KeyError(identifier)
    with connection() as db:
        revisions = [{**json.loads(row["document"]), "id": identifier, "version": row["version"],
                      "updated_at": row["created_at"]} for row in db.execute(
            "SELECT * FROM skill_revisions WHERE skill_id=? ORDER BY version DESC", (identifier,))]
    if items[identifier]["source"] == "builtin":
        with connection() as db:
            current = db.execute("SELECT document FROM skill_builtin_releases WHERE skill_id=? AND release_id=?",
                                 (identifier, items[identifier]["builtin_release"]["id"])).fetchone()
        builtin = json.loads(current["document"])
        builtin.pop("resources", None)
        builtin["base_release"] = builtin["builtin_release"]
        revisions.append(builtin)
    return revisions


def restore(identifier, version, expected_etag):
    candidate = next((item for item in history(identifier) if item["version"] == version), None)
    if not candidate:
        raise KeyError(version)
    return save(candidate, identifier=identifier, expected_etag=expected_etag,
                baseline=candidate.get("base_release"), follow_builtin=version == 0)


def builtin_releases(identifier):
    items = catalog()
    if identifier not in items or items[identifier]["source"] != "builtin":
        raise KeyError(identifier)
    with connection() as db:
        result = [json.loads(row["document"]) for row in db.execute(
            "SELECT document FROM skill_builtin_releases WHERE skill_id=? ORDER BY rowid DESC", (identifier,))]
    for item in result:
        item["resource_paths"] = sorted(item.pop("resources", {}))
    return result


def delete(identifier, expected_etag):
    initialize()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        item = _catalog(db).get(identifier)
        if not item:
            raise KeyError(identifier)
        if item["source"] == "builtin":
            raise ValueError("内置 Skill 不能删除；可以恢复内置版")
        if item["etag"] != expected_etag:
            raise SkillConflict("Skill 已更新，请刷新后再删除")
        # 保留不可变历史和已经提交的任务快照。
        db.execute("UPDATE skill_library SET deleted=1 WHERE id=?", (identifier,))


def capture_snapshot(db=None):
    if db is None:
        initialize()
        with connection() as handle:
            handle.execute("BEGIN IMMEDIATE")
            return capture_snapshot(handle)
    items = _catalog(db)
    for identifier, item in items.items():
        item["resources"] = {}
        if item["source"] != "builtin":
            continue
        release = item.get("base_release") or item["builtin_release"]
        row = db.execute("SELECT document FROM skill_builtin_releases WHERE skill_id=? AND release_id=?",
                         (identifier, release["id"])).fetchone()
        if not row:
            raise ValueError(f"Skill 内置基准版本缺失：{identifier}")
        item["resources"] = json.loads(row["document"])["resources"]
    return {"version": 1, "captured_at": now(), "skills": items}


def current_snapshot():
    snapshot = _active_snapshot.get()
    return snapshot if snapshot is not None else capture_snapshot()


def capture_for_run(db, run_id):
    # 新任务只登记策略；每个阶段第一次执行时才固定该阶段所用版本。
    db.execute("INSERT INTO skill_run_snapshots VALUES(?,?)",
               (run_id, _json({"version": 2, "policy": "phase_start", "captured_at": now()})))


def run_snapshot(run_id):
    initialize()
    with connection() as db:
        row = db.execute("SELECT snapshot FROM skill_run_snapshots WHERE run_id=?", (run_id,)).fetchone()
    # 升级前的任务保留原提交快照，不能悄悄改变其执行语义。
    return json.loads(row["snapshot"]) if row else capture_snapshot()


def snapshot_for_phase(phase):
    pinned = _active_snapshot.get()
    if pinned is not None:
        return pinned
    run_id = _active_run.get()
    if run_id is None:
        return capture_snapshot()
    policy = run_snapshot(run_id)
    if policy.get("policy") != "phase_start":
        return policy
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT snapshot FROM skill_phase_snapshots WHERE run_id=? AND phase=?",
                         (run_id, phase)).fetchone()
        if row:
            return json.loads(row["snapshot"])
        snapshot = {**capture_snapshot(db), "phase": phase, "policy": "phase_start"}
        db.execute("INSERT INTO skill_phase_snapshots VALUES(?,?,?)", (run_id, phase, _json(snapshot)))
        return snapshot


@contextmanager
def use_run(run_id):
    token = _active_run.set(run_id)
    try:
        yield
    finally:
        _active_run.reset(token)


@contextmanager
def use_snapshot(snapshot):
    token = _active_snapshot.set(snapshot)
    try:
        yield
    finally:
        _active_snapshot.reset(token)


def phase_skills(snapshot, phase, explicit=()):
    items = snapshot["skills"]
    names = list(explicit) or ([SKILL_BY_PHASE[phase]] if phase in SKILL_BY_PHASE else [])
    names += [key for key, item in items.items() if item["enabled"] and phase in item["phases"]]
    result = []
    for name in dict.fromkeys(names):
        item = items.get(name)
        if not item or not item["enabled"]:
            raise ValueError(f"Skill 不存在或已停用：{name}")
        result.append(item)
    return result


def render_context(items):
    return "".join(f"\n## Skill: {item['id']} ({item['title']}, revision={item['version']}, "
                   f"builtin_release={(item.get('base_release') or {}).get('id', 'unknown')}, "
                   f"sha256={item['content_hash']})\n{item['instructions']}\n" for item in items)


def usage(items):
    keys = ("id", "title", "source", "version", "content_hash", "base_release", "builtin_release", "customized")
    return [{key: item.get(key) for key in keys} for item in items]


def export_markdown(item):
    return f"---\nname: {item['id']}\ndescription: {json.dumps(item['description'], ensure_ascii=False)}\n---\n\n{item['instructions']}\n"
