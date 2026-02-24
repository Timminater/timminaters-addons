from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import ipaddress
import json
import logging
import os
import queue
import random
import socket
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from app.config import Settings
from app.errors import InvalidInputError, NoRandomAssetsError, NotFoundError, OperationError, classify_operation_exception
from app.media import MediaService
from app.runtime import RuntimeState
from app.store import StateStore
from app.tv_client import TVClient


_LOGGER = logging.getLogger(__name__)
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
        runtime: RuntimeState,
    ) -> None:
        self.settings = settings
        self.store = store
        self.media_service = media_service
        self.tv_client = tv_client
        self.runtime = runtime

        self.thumb_cache_dir = os.path.join(self.settings.data_dir, "cache", "thumbs")
        self.thumb_cache_max_files = 1000
        self.thumb_cache_max_age_seconds = 7 * 24 * 60 * 60
        self.thumb_cleanup_every = 100
        self._thumb_reads = 0
        self._thumb_lock = threading.Lock()
        self._tv_thumb_queue: queue.Queue[Dict[str, Any]] = queue.Queue()
        self._tv_thumb_pending: set[str] = set()
        self._tv_thumb_pending_lock = threading.Lock()
        self._tv_thumb_last_failure: Dict[str, float] = {}
        self._tv_thumb_failure_cooldown_seconds = 20.0
        self._tv_thumb_worker = threading.Thread(
            target=self._tv_thumb_worker_loop,
            daemon=True,
            name="tv-thumb-worker",
        )

        os.makedirs(self.settings.media_dir, exist_ok=True)
        os.makedirs(self.settings.data_dir, exist_ok=True)
        os.makedirs(self.thumb_cache_dir, exist_ok=True)
        self._tv_thumb_worker.start()

    def _tv_discovered_asset_id(self, tv_ip: str, content_id: str) -> str:
        digest = hashlib.sha1(f"{tv_ip}:{content_id}".encode("utf-8")).hexdigest()
        return f"tv-{digest[:24]}"

    def _tv_discovered_filename(self, tv_ip: str, content_id: str, remote_item: Dict[str, Any]) -> str:
        title = str(remote_item.get("title") or "").strip()
        if title:
            return title
        return f"TV {tv_ip} {content_id}"

    def bootstrap(self) -> None:
        state = self.store.load()
        changed = False

        changed |= self._scan_media_files(state)
        changed |= self._migrate_legacy_uploaded_files(state)

        if changed:
            self.store.save(state)

        self._cleanup_thumb_cache(force=True)

    def get_runtime_settings(self) -> Dict[str, Any]:
        self._sync_runtime_settings_from_disk()
        return {
            "tv_ips": list(self.settings.tv_ips),
            "refresh_interval_seconds": int(self.settings.refresh_interval_seconds),
            "snapshot_ttl_seconds": int(self.settings.snapshot_ttl_seconds),
        }

    def update_runtime_settings(
        self,
        tv_ips: List[str],
        refresh_interval_seconds: int,
        snapshot_ttl_seconds: int,
    ) -> Dict[str, Any]:
        validated_ips = self._validate_tv_ips(tv_ips)
        if not validated_ips:
            raise InvalidInputError("At least one valid TV IP is required")

        self.settings.tv_ips = validated_ips
        self.settings.refresh_interval_seconds = max(5, int(refresh_interval_seconds))
        self.settings.snapshot_ttl_seconds = max(1, int(snapshot_ttl_seconds))
        self.runtime.snapshot_ttl_seconds = self.settings.snapshot_ttl_seconds
        self.runtime.clear_snapshot_cache()

        self._persist_runtime_settings()
        self.trigger_refresh(force=True, wait=False)
        return self.get_runtime_settings()

    def _persist_runtime_settings(self) -> None:
        os.makedirs(os.path.dirname(self.settings.runtime_settings_path), exist_ok=True)
        payload = {
            "tv_ips": list(self.settings.tv_ips),
            "refresh_interval_seconds": int(self.settings.refresh_interval_seconds),
            "snapshot_ttl_seconds": int(self.settings.snapshot_ttl_seconds),
        }
        temp_path = f"{self.settings.runtime_settings_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(temp_path, self.settings.runtime_settings_path)

    def _sync_runtime_settings_from_disk(self) -> None:
        try:
            with open(self.settings.runtime_settings_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        if not isinstance(payload, dict):
            return

        tv_ips = payload.get("tv_ips")
        if isinstance(tv_ips, list):
            try:
                self.settings.tv_ips = self._validate_tv_ips([str(item) for item in tv_ips])
            except InvalidInputError:
                pass

        refresh_interval = payload.get("refresh_interval_seconds")
        if refresh_interval is not None:
            try:
                self.settings.refresh_interval_seconds = max(5, int(refresh_interval))
            except (TypeError, ValueError):
                pass

        snapshot_ttl = payload.get("snapshot_ttl_seconds")
        if snapshot_ttl is not None:
            try:
                ttl = max(1, int(snapshot_ttl))
                self.settings.snapshot_ttl_seconds = ttl
                self.runtime.snapshot_ttl_seconds = ttl
            except (TypeError, ValueError):
                pass

    def discover_supported_tvs(self, subnet: Optional[str] = None) -> Dict[str, Any]:
        self._sync_runtime_settings_from_disk()
        candidates = self._build_scan_candidates(subnet=subnet)
        results: List[Dict[str, Any]] = []

        if not candidates:
            return {"subnet": subnet, "candidates": 0, "found": []}

        scanner = TVClient(timeout=2)

        def probe(ip: str) -> Optional[Dict[str, Any]]:
            if not self._port_open(ip, 8001, timeout=0.2):
                return None
            snapshot = scanner.snapshot(ip)
            if snapshot.online and snapshot.supported:
                return {
                    "ip": ip,
                    "online": snapshot.online,
                    "supported": snapshot.supported,
                    "active_content_id": snapshot.active_id,
                }
            return None

        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = {executor.submit(probe, ip): ip for ip in candidates}
            for future in as_completed(futures):
                try:
                    item = future.result()
                except Exception:
                    item = None
                if item:
                    results.append(item)

        results.sort(key=lambda item: item["ip"])
        return {"subnet": subnet, "candidates": len(candidates), "found": results}

    def _build_scan_candidates(self, subnet: Optional[str]) -> List[str]:
        if subnet:
            try:
                network = ipaddress.ip_network(subnet, strict=False)
            except ValueError as exc:
                raise InvalidInputError(f"Invalid subnet: {subnet}") from exc
            hosts = [str(ip) for ip in network.hosts()]
            return hosts[:1024]

        networks = set()

        for tv_ip in self.settings.tv_ips:
            try:
                ip = ipaddress.ip_address(tv_ip)
            except ValueError:
                continue
            networks.add(ipaddress.ip_network(f"{ip}/24", strict=False))

        try:
            probe_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe_socket.connect(("8.8.8.8", 80))
            local_ip = probe_socket.getsockname()[0]
            probe_socket.close()
            networks.add(ipaddress.ip_network(f"{local_ip}/24", strict=False))
        except OSError:
            pass

        candidates: List[str] = []
        seen = set()
        for network in networks:
            for ip in network.hosts():
                text = str(ip)
                if text in seen:
                    continue
                seen.add(text)
                candidates.append(text)

        return candidates[:1024]

    def _port_open(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _validate_tv_ips(self, tv_ips: List[str]) -> List[str]:
        cleaned: List[str] = []
        seen = set()
        for raw in tv_ips:
            value = str(raw).strip()
            if not value or value in seen:
                continue
            try:
                ipaddress.ip_address(value)
            except ValueError:
                raise InvalidInputError(f"Invalid TV IP address: {value}")
            seen.add(value)
            cleaned.append(value)
        return cleaned

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

    def _legacy_uploaded_files_candidates(self) -> List[str]:
        # Older add-on releases persisted this file in /share/SamsungFrameTVArtChanger.
        # Keep these paths in migration lookup to preserve TV content_id mappings and
        # avoid unnecessary re-uploads that can create duplicates on TV.
        raw_candidates = [
            os.path.join(self.settings.data_dir, "uploaded_files.json"),
            os.path.join("/share", "SamsungFrameTVArtChanger", "uploaded_files.json"),
            os.path.join("/share", "uploaded_files.json"),
            "/uploaded_files.json",
            os.path.join(self.settings.media_dir, "uploaded_files.json"),
        ]

        unique: List[str] = []
        seen = set()
        for path in raw_candidates:
            norm = os.path.normpath(path)
            if norm in seen:
                continue
            seen.add(norm)
            unique.append(path)
        return unique

    def _migrate_legacy_uploaded_files(self, state: Dict[str, Any]) -> bool:
        candidates = self._legacy_uploaded_files_candidates()

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
        self._sync_runtime_settings_from_disk()
        configured = list(self.settings.tv_ips)
        if not tv_ips:
            return configured

        requested = [item for item in tv_ips if item]
        return [ip for ip in requested if ip in configured]

    def _is_refresh_due(self, last_refresh: Optional[str]) -> bool:
        if not last_refresh:
            return True

        try:
            then = datetime.fromisoformat(last_refresh)
        except ValueError:
            return True

        age = (datetime.now(timezone.utc) - then).total_seconds()
        return age >= self.settings.refresh_interval_seconds

    def trigger_refresh(self, force: bool = False, wait: bool = False) -> bool:
        self._sync_runtime_settings_from_disk()
        state = self.store.load()
        due = force or self._is_refresh_due(state.get("last_refresh"))

        started = False
        if due:
            started = self.runtime.start_refresh(lambda: self._perform_refresh(force_snapshot=force))

        if wait:
            self.runtime.wait_for_refresh(timeout=45.0)

        return started

    def refresh(self, force: bool = False, wait: bool = False) -> Dict[str, Any]:
        self.trigger_refresh(force=force, wait=wait)
        return self.store.load()

    def _perform_refresh(self, force_snapshot: bool = False) -> None:
        try:
            state = self.store.load()
            assets = state["assets"]
            tv_status: Dict[str, Any] = {}

            for tv_ip in self.settings.tv_ips:
                snapshot, from_cache = self.runtime.get_or_fetch_snapshot(
                    tv_ip,
                    self.tv_client.snapshot,
                    force=force_snapshot,
                )

                tv_status[tv_ip] = {
                    "online": snapshot.online,
                    "supported": snapshot.supported,
                    "active_content_id": snapshot.active_id,
                    "error": snapshot.error,
                    "cached": from_cache,
                }

                existing_by_content: Dict[str, Dict[str, Any]] = {}
                for asset in assets.values():
                    tv_entry = asset.setdefault("tv_map", {}).get(tv_ip)
                    content_id = tv_entry.get("content_id") if tv_entry else None
                    if content_id:
                        existing_by_content[str(content_id)] = asset

                if snapshot.online and snapshot.supported:
                    for content_id, remote_item in snapshot.available_items.items():
                        if not content_id:
                            continue

                        content_key = str(content_id)
                        asset = existing_by_content.get(content_key)
                        if not asset:
                            asset_id = self._tv_discovered_asset_id(tv_ip, content_key)
                            asset = assets.get(asset_id)
                            if not asset:
                                now = utc_now()
                                asset = {
                                    "asset_id": asset_id,
                                    "filename": self._tv_discovered_filename(tv_ip, content_key, remote_item),
                                    "ha_rel_path": None,
                                    "source": "tv_discovery",
                                    "created_at": now,
                                    "updated_at": now,
                                    "tv_map": {},
                                }
                                assets[asset_id] = asset
                            existing_by_content[content_key] = asset

                        tv_entry = asset.setdefault("tv_map", {}).setdefault(tv_ip, {})
                        previous_on_tv = bool(tv_entry.get("on_tv"))
                        previous_active = bool(tv_entry.get("active"))
                        previous_content = tv_entry.get("content_id")

                        tv_entry["content_id"] = content_key
                        tv_entry["on_tv"] = True
                        tv_entry["active"] = bool(snapshot.active_id and content_key == snapshot.active_id)
                        tv_entry["last_seen_at"] = utc_now()
                        tv_entry["error"] = None

                        if (
                            previous_content != content_key
                            or previous_on_tv != tv_entry["on_tv"]
                            or previous_active != tv_entry["active"]
                        ):
                            asset["updated_at"] = utc_now()

                for asset in assets.values():
                    tv_map = asset.setdefault("tv_map", {})
                    tv_entry = tv_map.get(tv_ip)
                    if not tv_entry:
                        continue

                    content_id = tv_entry.get("content_id")
                    if snapshot.online and snapshot.supported:
                        on_tv = bool(content_id and content_id in snapshot.available_ids)
                        active = bool(content_id and snapshot.active_id and content_id == snapshot.active_id)
                        if bool(tv_entry.get("on_tv")) != on_tv or bool(tv_entry.get("active")) != active:
                            asset["updated_at"] = utc_now()
                        tv_entry["on_tv"] = on_tv
                        tv_entry["active"] = active
                    else:
                        tv_entry["active"] = False
                    tv_entry["last_seen_at"] = utc_now()
                    tv_entry["error"] = snapshot.error

            orphan_asset_ids: List[str] = []
            for asset_id, asset in assets.items():
                has_ha_file = bool(self._ha_path_for_asset(asset))
                tv_map = asset.get("tv_map", {})
                has_tv_presence = any(bool(entry and entry.get("on_tv")) for entry in tv_map.values())
                if not has_ha_file and not has_tv_presence:
                    orphan_asset_ids.append(asset_id)

            for asset_id in orphan_asset_ids:
                assets.pop(asset_id, None)
                self._cleanup_asset_thumbs(asset_id)

            refreshed_at = utc_now()
            state["tv_status"] = tv_status
            state["last_refresh"] = refreshed_at
            self.store.save(state)
            self.runtime.set_last_refresh(refreshed_at)
        except Exception as exc:
            _LOGGER.exception("refresh job failed: %s", exc)

    def get_meta(self, request_id: str) -> Dict[str, Any]:
        state = self.store.load()
        last_refresh = state.get("last_refresh") or self.runtime.get_last_refresh()
        refresh_in_progress = self.runtime.is_refresh_in_progress()
        stale = self._is_refresh_due(last_refresh) or refresh_in_progress

        return {
            "stale": stale,
            "refresh_in_progress": refresh_in_progress,
            "last_refresh": last_refresh,
            "request_id": request_id,
        }

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

    def list_gallery(self, filter_name: str = "all", tv_ip: Optional[str] = None, trigger_refresh: bool = True) -> Dict[str, Any]:
        if trigger_refresh:
            self.refresh(force=False, wait=False)
        state = self.store.load()

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

    def list_tvs(self, trigger_refresh: bool = True) -> List[Dict[str, Any]]:
        if trigger_refresh:
            self.refresh(force=False, wait=False)
        state = self.store.load()
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
                    "cached": bool(status.get("cached", False)),
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
        self._cleanup_asset_thumbs(digest)

        activation_result = None
        if activate:
            activation_result = self.activate_asset(digest, tv_ips=tv_ips, ensure_upload=True, activate=True)

        self.refresh(force=False, wait=False)
        return UploadResult(asset=asset, duplicate=duplicate, activation=activation_result)

    def _load_ha_payload(self, asset: Dict[str, Any]) -> bytes:
        path = self._ha_path_for_asset(asset)
        if not path:
            raise NotFoundError("Image is not available in Home Assistant media storage")

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
            raise NotFoundError(f"Unknown asset_id: {asset_id}")

        results: Dict[str, Any] = {}

        for tv_ip in selected_tvs:
            entry = asset.setdefault("tv_map", {}).setdefault(tv_ip, {})
            content_id = entry.get("content_id")

            try:
                snapshot, _ = self.runtime.get_or_fetch_snapshot(tv_ip, self.tv_client.snapshot, force=False)
                if not snapshot.online:
                    raise OperationError("TV is offline", code="TV_OFFLINE", status=503, retryable=True)
                if not snapshot.supported:
                    raise OperationError("TV does not support art mode", code="TV_UNSUPPORTED", status=409, retryable=False)

                if ensure_upload:
                    needs_upload = not content_id
                    if content_id:
                        needs_upload = content_id not in snapshot.available_ids

                    if needs_upload:
                        payload = self._load_ha_payload(asset)
                        content_id = self.tv_client.upload(tv_ip, payload, file_type="JPEG")
                        entry["content_id"] = content_id
                        self.runtime.clear_snapshot_cache(tv_ip)

                if activate and content_id:
                    self.tv_client.select_image(tv_ip, content_id)
                    self.runtime.clear_snapshot_cache(tv_ip)

                entry["on_tv"] = bool(content_id)
                entry["active"] = bool(activate and content_id)
                entry["last_seen_at"] = utc_now()
                entry["error"] = None
                asset["updated_at"] = utc_now()

                results[tv_ip] = {
                    "ok": True,
                    "content_id": content_id,
                    "activated": bool(activate and content_id),
                    "code": None,
                    "retryable": False,
                }
            except Exception as exc:  # pragma: no cover - integration behavior
                classified = classify_operation_exception(exc)
                entry["error"] = classified["message"]
                entry["last_seen_at"] = utc_now()
                results[tv_ip] = {
                    "ok": False,
                    "content_id": content_id,
                    "error": classified["message"],
                    "code": classified["code"],
                    "retryable": classified["retryable"],
                }

        self.store.save(state)
        self.refresh(force=True, wait=False)
        return {
            "asset_id": asset_id,
            "results": results,
        }

    def delete_asset(self, asset_id: str, targets: str, tv_ips: Optional[List[str]] = None) -> Dict[str, Any]:
        state = self.store.load()
        asset = state["assets"].get(asset_id)
        if not asset:
            raise NotFoundError(f"Unknown asset_id: {asset_id}")

        selected_tvs = self.resolve_tv_ips(tv_ips)
        if not selected_tvs:
            selected_tvs = list(asset.get("tv_map", {}).keys())

        response: Dict[str, Any] = {
            "asset_id": asset_id,
            "targets": targets,
            "tv": {},
            "ha": {"deleted": False, "error": None, "code": None, "retryable": False},
        }

        if targets in {"tv", "both"}:
            for tv_ip in selected_tvs:
                entry = asset.setdefault("tv_map", {}).get(tv_ip)
                content_id = entry.get("content_id") if entry else None

                if not content_id:
                    response["tv"][tv_ip] = {
                        "ok": True,
                        "deleted": False,
                        "reason": "missing_content_id",
                        "code": None,
                        "retryable": False,
                    }
                    continue

                try:
                    deleted = self.tv_client.delete_image(tv_ip, content_id)
                    response["tv"][tv_ip] = {
                        "ok": True,
                        "deleted": bool(deleted),
                        "code": None,
                        "retryable": False,
                    }
                    asset["tv_map"].pop(tv_ip, None)
                    self.runtime.clear_snapshot_cache(tv_ip)
                except Exception as exc:  # pragma: no cover - integration behavior
                    classified = classify_operation_exception(exc)
                    response["tv"][tv_ip] = {
                        "ok": False,
                        "deleted": False,
                        "error": classified["message"],
                        "code": classified["code"],
                        "retryable": classified["retryable"],
                    }

        if targets in {"ha", "both"}:
            path = self._ha_path_for_asset(asset)
            if path:
                try:
                    os.remove(path)
                    response["ha"]["deleted"] = True
                except OSError as exc:
                    classified = classify_operation_exception(exc)
                    response["ha"]["error"] = classified["message"]
                    response["ha"]["code"] = "DELETE_FAILED"
                    response["ha"]["retryable"] = classified["retryable"]
            asset["ha_rel_path"] = None
            self._cleanup_asset_thumbs(asset_id)

        has_tv_refs = bool(asset.get("tv_map"))
        has_ha_file = bool(self._ha_path_for_asset(asset))
        if not has_tv_refs and not has_ha_file:
            state["assets"].pop(asset_id, None)
        else:
            asset["updated_at"] = utc_now()

        self.store.save(state)
        self.refresh(force=True, wait=False)
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
            raise NoRandomAssetsError()

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
            raise NotFoundError(f"Unknown asset_id: {asset_id}")

        path = self._ha_path_for_asset(asset)
        if path:
            try:
                mtime = int(os.path.getmtime(path))
                cache_file = os.path.join(self.thumb_cache_dir, f"{asset_id}-{mtime}.jpg")
                if os.path.exists(cache_file):
                    self._register_thumb_read()
                    with open(cache_file, "rb") as handle:
                        return handle.read()

                with open(path, "rb") as handle:
                    thumbnail = self.media_service.build_thumbnail(handle.read())

                temp_path = f"{cache_file}.tmp"
                with open(temp_path, "wb") as handle:
                    handle.write(thumbnail)
                os.replace(temp_path, cache_file)

                self._cleanup_asset_thumbs(asset_id, keep=cache_file)
                self._register_thumb_read()
                return thumbnail
            except OSError:
                pass

        tv_thumb = self._read_thumbnail_from_tv(asset)
        if tv_thumb:
            self._register_thumb_read()
            return tv_thumb

        self._register_thumb_read()
        return self._placeholder_thumbnail(asset_id)

    def _read_thumbnail_from_tv(self, asset: Dict[str, Any]) -> Optional[bytes]:
        tv_map = asset.get("tv_map", {})
        candidates: List[tuple] = []
        for tv_ip, entry in tv_map.items():
            if not isinstance(entry, dict):
                continue
            content_id = entry.get("content_id")
            if not content_id:
                continue
            score = (
                1 if entry.get("active") else 0,
                1 if entry.get("on_tv", True) else 0,
            )
            candidates.append((score, tv_ip, str(content_id)))

        candidates.sort(reverse=True)

        for _, tv_ip, content_id in candidates:
            cache_key = hashlib.sha1(f"{tv_ip}:{content_id}".encode("utf-8")).hexdigest()[:16]
            cache_file = os.path.join(self.thumb_cache_dir, f"{asset['asset_id']}-tv-{cache_key}.jpg")
            if os.path.exists(cache_file):
                with open(cache_file, "rb") as handle:
                    return handle.read()

            self._enqueue_tv_thumbnail_fetch(
                asset_id=str(asset["asset_id"]),
                tv_ip=tv_ip,
                content_id=content_id,
                cache_file=cache_file,
            )

        return None

    def _enqueue_tv_thumbnail_fetch(
        self,
        asset_id: str,
        tv_ip: str,
        content_id: str,
        cache_file: str,
    ) -> None:
        key = f"{asset_id}:{tv_ip}:{content_id}"
        now = time.monotonic()
        with self._tv_thumb_pending_lock:
            if key in self._tv_thumb_pending:
                return
            last_failure = self._tv_thumb_last_failure.get(key)
            if last_failure and (now - last_failure) < self._tv_thumb_failure_cooldown_seconds:
                return
            self._tv_thumb_pending.add(key)

        self._tv_thumb_queue.put(
            {
                "key": key,
                "asset_id": asset_id,
                "tv_ip": tv_ip,
                "content_id": content_id,
                "cache_file": cache_file,
            }
        )

    def _tv_thumb_worker_loop(self) -> None:
        while True:
            job = self._tv_thumb_queue.get()
            key = str(job.get("key"))
            asset_id = str(job.get("asset_id") or "")
            tv_ip = str(job.get("tv_ip") or "")
            content_id = str(job.get("content_id") or "")
            cache_file = str(job.get("cache_file") or "")
            success = False

            try:
                raw_bytes = self.tv_client.get_thumbnail(tv_ip, content_id)
                thumbnail = self.media_service.build_thumbnail(raw_bytes)
                if cache_file:
                    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
                    temp_path = f"{cache_file}.tmp"
                    with open(temp_path, "wb") as handle:
                        handle.write(thumbnail)
                    os.replace(temp_path, cache_file)
                    if asset_id:
                        self._cleanup_asset_thumbs(asset_id, keep=cache_file)
                success = True
            except Exception as exc:
                _LOGGER.warning(
                    "TV thumbnail fetch failed for %s on %s: %s",
                    content_id,
                    tv_ip,
                    exc,
                )
            finally:
                with self._tv_thumb_pending_lock:
                    self._tv_thumb_pending.discard(key)
                    if success:
                        self._tv_thumb_last_failure.pop(key, None)
                    else:
                        self._tv_thumb_last_failure[key] = time.monotonic()
                self._tv_thumb_queue.task_done()

    def _cleanup_asset_thumbs(self, asset_id: str, keep: Optional[str] = None) -> None:
        prefix = f"{asset_id}-"
        try:
            for name in os.listdir(self.thumb_cache_dir):
                if not name.startswith(prefix):
                    continue
                candidate = os.path.join(self.thumb_cache_dir, name)
                if keep and os.path.abspath(candidate) == os.path.abspath(keep):
                    continue
                try:
                    os.remove(candidate)
                except OSError:
                    pass
        except OSError:
            pass

    def _register_thumb_read(self) -> None:
        with self._thumb_lock:
            self._thumb_reads += 1
            should_cleanup = self._thumb_reads % self.thumb_cleanup_every == 0

        if should_cleanup:
            self._cleanup_thumb_cache(force=False)

    def _cleanup_thumb_cache(self, force: bool) -> None:
        try:
            entries = []
            now = datetime.now(timezone.utc).timestamp()
            for name in os.listdir(self.thumb_cache_dir):
                path = os.path.join(self.thumb_cache_dir, name)
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
                age = now - stat.st_mtime
                if age > self.thumb_cache_max_age_seconds:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                entries.append((path, stat.st_mtime))

            if len(entries) > self.thumb_cache_max_files:
                entries.sort(key=lambda item: item[1])
                remove_count = len(entries) - self.thumb_cache_max_files
                for path, _ in entries[:remove_count]:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except OSError:
            if force:
                _LOGGER.warning("thumbnail cache cleanup failed")

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
