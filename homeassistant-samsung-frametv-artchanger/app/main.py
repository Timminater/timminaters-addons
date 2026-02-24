from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.errors import AppError, InvalidInputError, UnauthorizedError, error_payload
from app.media import MediaService
from app.request_context import clear_request_id, get_request_id, set_request_id
from app.runtime import RuntimeState
from app.schemas import ActivateRequest, CropPayload, DeleteRequest, DiscoveryRequest, RandomRequest, SettingsUpdateRequest
from app.service import GalleryService
from app.stdin_commands import StdinCommandProcessor
from app.store import StateStore
from app.tv_client import TVClient


_LOGGER = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")

settings = load_settings()
store = StateStore(settings.state_path)
media_service = MediaService()
tv_client = TVClient()
runtime = RuntimeState(snapshot_ttl_seconds=settings.snapshot_ttl_seconds)
service = GalleryService(settings=settings, store=store, media_service=media_service, tv_client=tv_client, runtime=runtime)
stdin_processor = StdinCommandProcessor(service=service)

app = FastAPI(title="Frame TV Art Changer", version="2.1.0")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.on_event("startup")
async def startup_event() -> None:
    service.bootstrap()
    service.trigger_refresh(force=True, wait=False)
    stdin_processor.start()


def request_id_from_request(request: Request) -> str:
    state_value = getattr(request.state, "request_id", None)
    if state_value:
        return state_value

    context_value = get_request_id()
    if context_value:
        return context_value

    return uuid.uuid4().hex


def with_meta(request: Request, data: Dict[str, Any], compatibility: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request_id = request_id_from_request(request)
    meta = service.get_meta(request_id)
    payload: Dict[str, Any] = {
        "data": data,
        "meta": meta,
    }
    if compatibility:
        payload.update(compatibility)
    return payload


def parse_tv_ips(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_crop(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"Invalid crop payload: {exc}") from exc

    if not isinstance(loaded, dict):
        raise InvalidInputError("Invalid crop payload type")

    try:
        payload = CropPayload.model_validate(loaded)
    except Exception as exc:
        raise InvalidInputError(f"Invalid crop payload: {exc}") from exc

    return payload.as_dict()


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
        raise UnauthorizedError("automation_token is not configured")

    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        raise UnauthorizedError("Missing bearer token")

    token = header[len(prefix) :].strip()
    if not secrets.compare_digest(token, configured_token):
        raise UnauthorizedError("Invalid bearer token")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    set_request_id(request_id)

    try:
        response = await call_next(request)
    finally:
        clear_request_id()

    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/api/health")
def health(request: Request) -> Dict[str, Any]:
    data = {
        "ok": True,
        "tv_count": len(settings.tv_ips),
        "media_dir": settings.media_dir,
    }
    return with_meta(request, data, compatibility=data)


@app.get("/api/tvs")
def get_tvs(request: Request) -> Dict[str, Any]:
    tvs = service.list_tvs(trigger_refresh=True)
    data = {"tvs": tvs}
    return with_meta(request, data, compatibility={"tvs": tvs})


@app.get("/api/settings")
def get_settings(request: Request) -> Dict[str, Any]:
    payload = service.get_runtime_settings()
    return with_meta(request, payload, compatibility=payload)


@app.put("/api/settings")
def update_settings(payload: SettingsUpdateRequest, request: Request) -> Dict[str, Any]:
    updated = service.update_runtime_settings(
        tv_ips=payload.tv_ips,
        refresh_interval_seconds=payload.refresh_interval_seconds,
        snapshot_ttl_seconds=payload.snapshot_ttl_seconds,
    )
    return with_meta(request, updated, compatibility=updated)


@app.post("/api/settings/discover")
def discover_settings(payload: DiscoveryRequest, request: Request) -> Dict[str, Any]:
    result = service.discover_supported_tvs(subnet=payload.subnet)
    return with_meta(request, result, compatibility=result)


@app.get("/api/gallery")
def get_gallery(request: Request, filter: str = "all", tv_ip: Optional[str] = None) -> Dict[str, Any]:
    gallery = service.list_gallery(filter_name=filter, tv_ip=tv_ip, trigger_refresh=True)
    return with_meta(request, gallery, compatibility=gallery)


@app.post("/api/refresh")
def refresh(request: Request) -> Dict[str, Any]:
    started = service.trigger_refresh(force=True, wait=False)
    gallery = service.list_gallery(filter_name="all", trigger_refresh=False)
    compatibility = dict(gallery)
    compatibility["refresh_triggered"] = started

    data = {
        "refresh_triggered": started,
        "gallery": gallery,
    }
    return with_meta(request, data, compatibility=compatibility)


@app.get("/api/thumb/{asset_id}")
def thumb(asset_id: str) -> Response:
    image = service.read_thumbnail(asset_id)
    return Response(content=image, media_type="image/jpeg")


@app.post("/api/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    crop: Optional[str] = Form(None),
    tv_ips: Optional[str] = Form(None),
    activate: Optional[str] = Form(None),
) -> Dict[str, Any]:
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png"}:
        raise InvalidInputError("Only .jpg, .jpeg and .png files are supported")

    payload = await file.read()
    if not payload:
        raise InvalidInputError("Uploaded file is empty")

    crop_data = parse_crop(crop)
    selected_tvs = parse_tv_ips(tv_ips)
    should_activate = parse_bool(activate, default=False)

    result = service.upload_image(
        file_bytes=payload,
        crop=crop_data,
        activate=should_activate,
        tv_ips=selected_tvs,
    )

    response = {
        "asset": result.asset,
        "duplicate": result.duplicate,
        "activation": result.activation,
    }
    return with_meta(request, response, compatibility=response)


@app.post("/api/items/{asset_id}/activate")
def activate(asset_id: str, payload: ActivateRequest, request: Request) -> Dict[str, Any]:
    result = service.activate_asset(
        asset_id,
        tv_ips=payload.tv_ips,
        ensure_upload=payload.ensure_upload,
        activate=payload.activate,
    )
    return with_meta(request, result, compatibility=result)


@app.delete("/api/items/{asset_id}")
def delete(asset_id: str, payload: DeleteRequest, request: Request) -> Dict[str, Any]:
    result = service.delete_asset(asset_id, targets=payload.targets, tv_ips=payload.tv_ips)
    return with_meta(request, result, compatibility=result)


@app.post("/api/automation/random")
def automation_random(request: Request, payload: RandomRequest = RandomRequest()) -> Dict[str, Any]:
    verify_automation_auth(request)
    result = service.random_activate(
        tv_ips=payload.tv_ips,
        ensure_upload=payload.ensure_upload,
        activate=payload.activate,
    )
    return with_meta(request, result, compatibility=result)


def _http_exception_to_error(exc: HTTPException) -> Dict[str, Any]:
    message = str(exc.detail)
    if exc.status_code == 401:
        return {"code": "UNAUTHORIZED", "message": message, "retryable": False}
    if exc.status_code == 404:
        return {"code": "NOT_FOUND", "message": message, "retryable": False}
    if exc.status_code in {400, 422}:
        return {"code": "INVALID_INPUT", "message": message, "retryable": False}
    if exc.status_code >= 500:
        return {"code": "INTERNAL_ERROR", "message": message, "retryable": True}
    return {"code": "INVALID_INPUT", "message": message, "retryable": False}


def _error_response(request: Request, status: int, code: str, message: str, retryable: bool) -> JSONResponse:
    request_id = request_id_from_request(request)
    payload = error_payload(code=code, message=message, retryable=retryable, request_id=request_id)
    return JSONResponse(status_code=status, content=payload, headers={"X-Request-ID": request_id})


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    request_id = request_id_from_request(request)
    return JSONResponse(
        status_code=exc.status,
        content=exc.as_dict(request_id=request_id),
        headers={"X-Request-ID": request_id},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(request, 400, "INVALID_INPUT", str(exc), retryable=False)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    mapped = _http_exception_to_error(exc)
    return _error_response(request, exc.status_code, mapped["code"], mapped["message"], mapped["retryable"])


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    _LOGGER.exception("Unhandled exception: %s", exc)
    return _error_response(request, 500, "INTERNAL_ERROR", "Internal server error", retryable=False)
