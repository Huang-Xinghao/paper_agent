"""Single-workbench skill management, under the existing authentication boundary."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from paper_agent import skill_registry as registry

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1200)
    instructions: str = Field(min_length=1, max_length=100000)
    enabled: bool = True
    phases: list[str] = Field(default_factory=list, max_length=20)


class SkillUpdate(SkillDocument):
    expected_etag: str = Field(min_length=64, max_length=64)


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_etag: str = Field(min_length=64, max_length=64)
    version: int = Field(ge=0)


class DeleteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_etag: str = Field(min_length=64, max_length=64)


def boundary(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except registry.SkillConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Skill 或历史版本不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def list_skills():
    return {"items": [{key: value for key, value in item.items() if key != "instructions"}
                      for item in registry.catalog().values()], "phases": registry.PHASES,
            "scope": "workbench", "activation": "next_phase_start"}


@router.post("", status_code=201)
def create_skill(body: SkillDocument):
    return boundary(registry.save, body.model_dump())


@router.get("/{identifier}")
def get_skill(identifier: str):
    def read():
        return registry.catalog()[identifier]
    return boundary(read)


@router.put("/{identifier}")
def update_skill(identifier: str, body: SkillUpdate):
    return boundary(registry.save, body.model_dump(exclude={"expected_etag"}),
                    identifier=identifier, expected_etag=body.expected_etag)


@router.get("/{identifier}/history")
def skill_history(identifier: str):
    return {"items": boundary(registry.history, identifier)}


@router.get("/{identifier}/releases")
def skill_releases(identifier: str):
    return {"items": boundary(registry.builtin_releases, identifier)}


@router.post("/{identifier}/restore")
def restore_skill(identifier: str, body: VersionAction):
    return boundary(registry.restore, identifier, body.version, body.expected_etag)


@router.delete("/{identifier}")
def delete_skill(identifier: str, body: DeleteAction):
    boundary(registry.delete, identifier, body.expected_etag)
    return {"deleted": True}


@router.get("/{identifier}/download")
def download_skill(identifier: str):
    item = get_skill(identifier)
    return Response(registry.export_markdown(item), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{item["id"]}-SKILL.md"'})
