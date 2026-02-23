from __future__ import annotations

import json
import os
import secrets
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.media import MediaService
from app.schemas import ActivateRequest, DeleteRequest, RandomRequest
from app.service import GalleryService
from app.store import StateStore
from app.tv_client import TVClient


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")

settings = load_settings()
store = StateStore(settings.state_path)
media_service = MediaService()
tv_client = TVClient()
service = GalleryService(settings=settings, store=store, media_service=media_service, tv_client=tv_client)
service.bootstrap()

app = FastAPI(title="Frame TV Art Changer", version="2.0.0")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def parse_tv_ips(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_crop(raw: Optional[str]) -> Optional[Dict[str, float]]:
    if not raw:
        return None

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid crop payload: {exc}") from exc

    if not isinstance(loaded, dict):
        raise HTTPException(status_code=400, detail="Invalid crop payload type")

    result: Dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        value = loaded.get(key)
        if value is None:
            continue
        try:
            result[key] = float(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid crop value for {key}") from exc

    return result if result else None


def parse_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def verify_automation_auth(request: Request) -> None:
    configured_token = settings.automation_token.strip()
    if not configured_token:
        raise HTTPException(status_code=503, detail="automation_token is not configured")

    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = header[len(prefix) :].strip()
    if not secrets.compare_digest(token, configured_token):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "tv_count": len(settings.tv_ips),
        "media_dir": settings.media_dir,
    }


@app.get("/api/tvs")
def get_tvs() -> Dict[str, Any]:
    return {"tvs": service.list_tvs()}


@app.get("/api/gallery")
def get_gallery(filter: str = "all", tv_ip: Optional[str] = None) -> Dict[str, Any]:
    return service.list_gallery(filter_name=filter, tv_ip=tv_ip)


@app.post("/api/refresh")
def refresh() -> Dict[str, Any]:
    service.refresh(force=True)
    return service.list_gallery(filter_name="all")


@app.get("/api/thumb/{asset_id}")
def thumb(asset_id: str) -> Response:
    try:
        image = service.read_thumbnail(asset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(content=image, media_type="image/jpeg")


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    crop: Optional[str] = Form(None),
    tv_ips: Optional[str] = Form(None),
    activate: Optional[str] = Form(None),
) -> Dict[str, Any]:
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png"}:
        raise HTTPException(status_code=400, detail="Only .jpg, .jpeg and .png files are supported")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    crop_data = parse_crop(crop)
    selected_tvs = parse_tv_ips(tv_ips)
    should_activate = parse_bool(activate, default=False)

    result = service.upload_image(
        file_bytes=payload,
        crop=crop_data,
        activate=should_activate,
        tv_ips=selected_tvs,
    )

    return {
        "asset": result.asset,
        "duplicate": result.duplicate,
        "activation": result.activation,
    }


@app.post("/api/items/{asset_id}/activate")
def activate(asset_id: str, request: ActivateRequest) -> Dict[str, Any]:
    try:
        return service.activate_asset(
            asset_id,
            tv_ips=request.tv_ips,
            ensure_upload=request.ensure_upload,
            activate=request.activate,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/items/{asset_id}")
def delete(asset_id: str, request: DeleteRequest) -> Dict[str, Any]:
    try:
        return service.delete_asset(asset_id, targets=request.targets, tv_ips=request.tv_ips)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/automation/random")
def automation_random(request: Request, payload: RandomRequest = RandomRequest()) -> Dict[str, Any]:
    verify_automation_auth(request)

    try:
        return service.random_activate(
            tv_ips=payload.tv_ips,
            ensure_upload=payload.ensure_upload,
            activate=payload.activate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})
