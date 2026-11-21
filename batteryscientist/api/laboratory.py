"""HTTP access to the same laboratory service used by agents."""

from fastapi import APIRouter, HTTPException, Request
from batteryscientist.laboratory.models import SnapshotView, SourceView

router = APIRouter(prefix="/api/laboratory", tags=["laboratory"])


@router.get("/sources", response_model=list[SourceView])
async def laboratory_sources(request: Request):
    return request.app.state.laboratory.sources()


@router.get("/sources/{source_id}/snapshot", response_model=SnapshotView)
async def laboratory_snapshot(source_id: str, request: Request):
    try:
        return await request.app.state.laboratory.snapshot(source_id)
    except KeyError:
        raise HTTPException(404, "实验室设备来源不存在") from None


# Retain existing read-only URLs while clients migrate.
@router.get("/bts", response_model=SnapshotView, deprecated=True)
async def bts_snapshot(request: Request):
    return await request.app.state.laboratory.snapshot("bts")


@router.get("/land", response_model=SnapshotView, deprecated=True)
async def land_snapshot(request: Request):
    return await request.app.state.laboratory.snapshot("land")
