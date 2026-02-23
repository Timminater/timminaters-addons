from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import random
from typing import Any, Dict, Iterable, List, Optional

from app.config import Settings
from app.media import MediaService
from app.store import StateStore
from app.tv_client import TVClient


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UploadResult:
    asset: Dict[str, Any]
    duplicate: bool
    activation: Optional[Dict[str, Any]] = None


class GalleryService:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        media_service: MediaService,
        tv_client: TVClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.media_service = media_service
        self.tv_client = tv_client

        os.makedirs(self.settings.media_dir, exist_ok=True)
        os.makedirs(self.settings.data_dir, exist_ok=True)

    def bootstrap(self) -> None:
        state = self.store.load()
        changed = False

        changed |= self._scan_media_files(state)
        changed |= self._migrate_legacy_uploaded_files(state)

        if changed:
            self.store.save(state)

        self.refresh(force=True)

    def _scan_media_files(self, state: Dict[str, Any]) -> bool:
        changed = False
        assets: Dict[str, Dict[str, Any]] = state["assets"]

        known_paths = {
            entry.get("ha_rel_path")
            for entry in assets.values()
            if isinstance(entry, dict) and entry.get("ha_rel_path")
        }

        for root, _, files in os.walk(self.settings.media_dir):
            for filename in files:
                extension = os.path.splitext(filename)[1].lower()
                if extension not in SUPPORTED_EXTENSIONS:
                    continue

                absolute_path = os.path.join(root, filename)
                rel_path = os.path.relpath(absolute_path, self.settings.media_dir).replace("\\", "/")
                if rel_path in known_paths:
                    continue

                try:
                    with open(absolute_path, "rb") as handle:
                        prepared_bytes, digest = self.media_service.prepare_image(handle.read())
                except Exception:
                    continue

                asset = assets.get(digest)
                if not asset:
                    asset = {
                        "asset_id": digest,
                        "filename": f"{digest}.jpg",
                        "ha_rel_path": rel_path,
                        "source": "scan",
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                        "tv_map": {},
                    }
                    assets[digest] = asset
                    changed = True
                elif not asset.get("ha_rel_path"):
                    asset["ha_rel_path"] = rel_path
                    asset["updated_at"] = utc_now()
                    changed = True

                known_paths.add(rel_path)

        return changed

    def _migrate_legacy_uploaded_files(self, state: Dict[str, Any]) -> bool:
        candidates = [
            os.path.join(self.settings.data_dir, "uploaded_files.json"),
            "/uploaded_files.json",
            os.path.join(self.settings.media_dir, "uploaded_files.json"),
        ]

        legacy_path = next((path for path in candidates if os.path.exists(path)), None)
        if not legacy_path:
            return False

        try:
            with open(legacy_path, "r", encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False

        if not isinstance(entries, list):
            return False

        changed = False
        assets = state["assets"]
        file_name_to_asset_id = {
            os.path.basename(asset.get("ha_rel_path", "")): asset_id
            for asset_id, asset in assets.items()
            if asset.get("ha_rel_path")
        }

        default_tv = self.settings.tv_ips[0] if self.settings.tv_ips else None

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            remote_filename = str(entry.get("remote_filename", "")).strip()
            source_file = str(entry.get("file", "")).strip()
            tv_ip = str(entry.get("tv_ip") or default_tv or "").strip()

            if not remote_filename or not tv_ip:
                continue

            base_name = os.path.basename(source_file)
            asset_id = file_name_to_asset_id.get(base_name)

            if not asset_id:
                legacy_hash = hashlib.sha1(f"{tv_ip}:{remote_filename}:{source_file}".encode("utf-8")).hexdigest()
                asset_id = f"legacy-{legacy_hash[:20]}"
                if asset_id not in assets:
                    assets[asset_id] = {
                        "asset_id": asset_id,
                        "filename": base_name or asset_id,
                        "ha_rel_path": None,
                        "source": "legacy",
                        "created_at": utc_now(),
                        "updated_at": utc_now(),
                        "tv_map": {},
                    }
                    changed = True

            tv_map = assets[asset_id].setdefault("tv_map", {})
            tv_entry = tv_map.setdefault(tv_ip, {})
            tv_entry["content_id"] = remote_filename
            tv_entry.setdefault("on_tv", True)
            tv_entry.setdefault("active", False)
            tv_entry["last_seen_at"] = utc_now()
            assets[asset_id]["updated_at"] = utc_now()
            changed = True

        if changed:
            migrated_path = f"{legacy_path}.migrated"
            try:
                os.replace(legacy_path, migrated_path)
            except OSError:
                pass

        return changed

    def resolve_tv_ips(self, tv_ips: Optional[Iterable[str]] = None) -> List[str]:
        configured = list(self.settings.tv_ips)
        if not tv_ips:
            return configured

        requested = [item for item in tv_ips if item]
        return [ip for ip in requested if ip in configured]

    def refresh(self, force: bool = False) -> Dict[str, Any]:
        state = self.store.load()

        last_refresh = state.get("last_refresh")
        if not force and last_refresh:
            try:
                then = datetime.fromisoformat(last_refresh)
                age = (datetime.now(timezone.utc) - then).total_seconds()
                if age < self.settings.refresh_interval_seconds:
                    return state
            except ValueError:
                pass

        tv_status: Dict[str, Any] = {}
        assets = state["assets"]

        for tv_ip in self.settings.tv_ips:
            snapshot = self.tv_client.snapshot(tv_ip)
            tv_status[tv_ip] = {
                "online": snapshot.online,
                "supported": snapshot.supported,
                "active_content_id": snapshot.active_id,
                "error": snapshot.error,
            }

            for asset in assets.values():
                tv_map = asset.setdefault("tv_map", {})
                tv_entry = tv_map.get(tv_ip)
                if not tv_entry:
                    continue

                content_id = tv_entry.get("content_id")
                on_tv = bool(content_id and content_id in snapshot.available_ids)
                active = bool(content_id and snapshot.active_id and content_id == snapshot.active_id)

                tv_entry["on_tv"] = on_tv
                tv_entry["active"] = active
                tv_entry["last_seen_at"] = utc_now()
                tv_entry["error"] = snapshot.error

        state["tv_status"] = tv_status
        state["last_refresh"] = utc_now()
        self.store.save(state)
        return state

    def _ha_path_for_asset(self, asset: Dict[str, Any]) -> Optional[str]:
        rel_path = asset.get("ha_rel_path")
        if not rel_path:
            return None

        abs_path = os.path.join(self.settings.media_dir, rel_path)
        return abs_path if os.path.exists(abs_path) else None

    def _build_item(self, asset: Dict[str, Any], tv_ip: Optional[str]) -> Dict[str, Any]:
        ha_path = self._ha_path_for_asset(asset)
        on_ha = ha_path is not None

        tv_map: Dict[str, Any] = asset.get("tv_map", {})
        selected_entries = [tv_map.get(tv_ip)] if tv_ip else list(tv_map.values())

        on_tv = any(bool(entry and entry.get("on_tv", entry.get("content_id"))) for entry in selected_entries)
        active = any(bool(entry and entry.get("active")) for entry in selected_entries)
        synced = bool(on_ha and on_tv)

        return {
            "asset_id": asset["asset_id"],
            "filename": asset.get("filename") or asset["asset_id"],
            "on_ha": on_ha,
            "on_tv": on_tv,
            "synced": synced,
            "active": active,
            "source": asset.get("source", "upload"),
            "created_at": asset.get("created_at"),
            "updated_at": asset.get("updated_at"),
            "tv_map": tv_map,
        }

    def list_gallery(self, filter_name: str = "all", tv_ip: Optional[str] = None) -> Dict[str, Any]:
        state = self.refresh(force=False)
        assets = state["assets"]
        items: List[Dict[str, Any]] = []

        for asset in assets.values():
            item = self._build_item(asset, tv_ip)

            if filter_name == "tv" and not item["on_tv"]:
                continue
            if filter_name == "ha" and not item["on_ha"]:
                continue
            if filter_name == "synced" and not item["synced"]:
                continue
            if filter_name == "unsynced" and item["synced"]:
                continue

            items.append(item)

        items.sort(key=lambda x: x.get("updated_at") or "", reverse=True)

        return {
            "items": items,
            "last_refresh": state.get("last_refresh"),
            "tv_status": state.get("tv_status", {}),
        }

    def list_tvs(self) -> List[Dict[str, Any]]:
        state = self.refresh(force=False)
        tv_status = state.get("tv_status", {})

        result = []
        for tv_ip in self.settings.tv_ips:
            status = tv_status.get(tv_ip, {})
            result.append(
                {
                    "ip": tv_ip,
                    "online": bool(status.get("online", False)),
                    "supported": bool(status.get("supported", False)),
                    "active_content_id": status.get("active_content_id"),
                    "error": status.get("error"),
                }
            )
        return result

    def upload_image(
        self,
        file_bytes: bytes,
        crop: Optional[Dict[str, float]] = None,
        activate: bool = False,
        tv_ips: Optional[List[str]] = None,
    ) -> UploadResult:
        prepared_bytes, digest = self.media_service.prepare_image(file_bytes, crop)
        state = self.store.load()
        assets = state["assets"]

        duplicate = digest in assets
        asset = assets.get(digest)

        if not asset:
            filename = f"{digest}.jpg"
            file_path = os.path.join(self.settings.media_dir, filename)
            with open(file_path, "wb") as handle:
                handle.write(prepared_bytes)

            asset = {
                "asset_id": digest,
                "filename": filename,
                "ha_rel_path": filename,
                "source": "upload",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "tv_map": {},
            }
            assets[digest] = asset
        else:
            asset["updated_at"] = utc_now()

        self.store.save(state)

        activation_result = None
        if activate:
            activation_result = self.activate_asset(digest, tv_ips=tv_ips, ensure_upload=True, activate=True)

        return UploadResult(asset=asset, duplicate=duplicate, activation=activation_result)

    def _load_ha_payload(self, asset: Dict[str, Any]) -> bytes:
        path = self._ha_path_for_asset(asset)
        if not path:
            raise FileNotFoundError("Image is not available in Home Assistant media storage")

        with open(path, "rb") as handle:
            raw_bytes = handle.read()

        prepared_bytes, _ = self.media_service.prepare_image(raw_bytes)
        return prepared_bytes

    def activate_asset(
        self,
        asset_id: str,
        tv_ips: Optional[List[str]] = None,
        ensure_upload: bool = True,
        activate: bool = True,
    ) -> Dict[str, Any]:
        selected_tvs = self.resolve_tv_ips(tv_ips)
        state = self.store.load()
        asset = state["assets"].get(asset_id)

        if not asset:
            raise KeyError(f"Unknown asset_id: {asset_id}")

        results: Dict[str, Any] = {}

        for tv_ip in selected_tvs:
            entry = asset.setdefault("tv_map", {}).setdefault(tv_ip, {})
            content_id = entry.get("content_id")

            try:
                if ensure_upload:
                    needs_upload = not content_id

                    if content_id:
                        snapshot = self.tv_client.snapshot(tv_ip)
                        needs_upload = content_id not in snapshot.available_ids

                    if needs_upload:
                        payload = self._load_ha_payload(asset)
                        content_id = self.tv_client.upload(tv_ip, payload, file_type="JPEG")
                        entry["content_id"] = content_id

                if activate and content_id:
                    self.tv_client.select_image(tv_ip, content_id)

                entry["on_tv"] = bool(content_id)
                entry["active"] = bool(activate and content_id)
                entry["last_seen_at"] = utc_now()
                entry["error"] = None
                asset["updated_at"] = utc_now()

                results[tv_ip] = {
                    "ok": True,
                    "content_id": content_id,
                    "activated": bool(activate and content_id),
                }
            except Exception as exc:  # pragma: no cover - integration behavior
                entry["error"] = str(exc)
                entry["last_seen_at"] = utc_now()
                results[tv_ip] = {
                    "ok": False,
                    "content_id": content_id,
                    "error": str(exc),
                }

        self.store.save(state)
        self.refresh(force=True)
        return {
            "asset_id": asset_id,
            "results": results,
        }

    def delete_asset(self, asset_id: str, targets: str, tv_ips: Optional[List[str]] = None) -> Dict[str, Any]:
        state = self.store.load()
        asset = state["assets"].get(asset_id)
        if not asset:
            raise KeyError(f"Unknown asset_id: {asset_id}")

        selected_tvs = self.resolve_tv_ips(tv_ips)
        if not selected_tvs:
            selected_tvs = list(asset.get("tv_map", {}).keys())

        response: Dict[str, Any] = {
            "asset_id": asset_id,
            "targets": targets,
            "tv": {},
            "ha": {"deleted": False, "error": None},
        }

        if targets in {"tv", "both"}:
            for tv_ip in selected_tvs:
                entry = asset.setdefault("tv_map", {}).get(tv_ip)
                content_id = entry.get("content_id") if entry else None

                if not content_id:
                    response["tv"][tv_ip] = {"ok": True, "deleted": False, "reason": "missing_content_id"}
                    continue

                try:
                    deleted = self.tv_client.delete_image(tv_ip, content_id)
                    response["tv"][tv_ip] = {"ok": True, "deleted": bool(deleted)}
                    asset["tv_map"].pop(tv_ip, None)
                except Exception as exc:  # pragma: no cover - integration behavior
                    response["tv"][tv_ip] = {"ok": False, "deleted": False, "error": str(exc)}

        if targets in {"ha", "both"}:
            path = self._ha_path_for_asset(asset)
            if path:
                try:
                    os.remove(path)
                    response["ha"]["deleted"] = True
                except OSError as exc:
                    response["ha"]["error"] = str(exc)
            asset["ha_rel_path"] = None

        has_tv_refs = bool(asset.get("tv_map"))
        has_ha_file = bool(self._ha_path_for_asset(asset))
        if not has_tv_refs and not has_ha_file:
            state["assets"].pop(asset_id, None)
        else:
            asset["updated_at"] = utc_now()

        self.store.save(state)
        self.refresh(force=True)
        return response

    def random_activate(
        self,
        tv_ips: Optional[List[str]] = None,
        ensure_upload: bool = True,
        activate: bool = True,
    ) -> Dict[str, Any]:
        state = self.store.load()

        ha_assets = [
            asset_id
            for asset_id, asset in state["assets"].items()
            if self._ha_path_for_asset(asset)
        ]

        if not ha_assets:
            raise ValueError("No local gallery images are available for random selection")

        selected_asset_id = random.choice(ha_assets)
        result = self.activate_asset(
            selected_asset_id,
            tv_ips=tv_ips,
            ensure_upload=ensure_upload,
            activate=activate,
        )
        result["selected_asset_id"] = selected_asset_id
        return result

    def read_thumbnail(self, asset_id: str) -> bytes:
        state = self.store.load()
        asset = state["assets"].get(asset_id)
        if not asset:
            raise KeyError(f"Unknown asset_id: {asset_id}")

        path = self._ha_path_for_asset(asset)
        if not path:
            return self._placeholder_thumbnail(asset_id)

        with open(path, "rb") as handle:
            return self.media_service.build_thumbnail(handle.read())

    def _placeholder_thumbnail(self, text: str) -> bytes:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (640, 360), (35, 39, 46))
        draw = ImageDraw.Draw(image)
        draw.rectangle([(0, 0), (639, 359)], outline=(84, 93, 105), width=3)
        draw.text((24, 160), f"No HA image\n{text[:18]}", fill=(235, 236, 240))

        from io import BytesIO

        output = BytesIO()
        image.save(output, format="JPEG", quality=82)
        return output.getvalue()
