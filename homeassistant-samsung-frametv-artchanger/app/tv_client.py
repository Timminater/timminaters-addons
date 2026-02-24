from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Dict, List, Optional, Set

from samsungtvws import SamsungTVWS


@dataclass
class TVSnapshot:
    online: bool
    supported: bool
    available_ids: Set[str]
    available_items: Dict[str, Dict[str, Any]]
    active_id: Optional[str]
    error: Optional[str] = None


class TVClient:
    def __init__(self, timeout: int = 8) -> None:
        self.timeout = timeout

    def _tv(self, tv_ip: str) -> SamsungTVWS:
        return SamsungTVWS(tv_ip, timeout=self.timeout)

    def _thumbnail_tv(self, tv_ip: str) -> SamsungTVWS:
        return SamsungTVWS(tv_ip, timeout=max(self.timeout, 10))

    def _extract_thumbnail_bytes(self, payload: Any) -> Optional[bytes]:
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload) if payload else None

        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, (bytes, bytearray)) and value:
                    return bytes(value)
            return None

        if isinstance(payload, list):
            for item in payload:
                extracted = self._extract_thumbnail_bytes(item)
                if extracted:
                    return extracted
            return None

        return None

    def snapshot(self, tv_ip: str) -> TVSnapshot:
        try:
            tv = self._tv(tv_ip)
            art = tv.art()
            supported = bool(art.supported())
            if not supported:
                return TVSnapshot(
                    online=True,
                    supported=False,
                    available_ids=set(),
                    available_items={},
                    active_id=None,
                )

            raw_items = art.available()
            items: List[Dict[str, Any]] = raw_items if isinstance(raw_items, list) else []
            available_ids: Set[str] = set()
            available_items: Dict[str, Dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                content_id = item.get("content_id")
                if not content_id:
                    continue
                content_key = str(content_id)
                available_ids.add(content_key)
                if content_key not in available_items:
                    available_items[content_key] = dict(item)

            current = art.get_current()
            active_id = current.get("content_id") if isinstance(current, dict) else None
            return TVSnapshot(
                online=True,
                supported=True,
                available_ids=available_ids,
                available_items=available_items,
                active_id=active_id,
            )
        except Exception as exc:  # pragma: no cover - integration behavior
            return TVSnapshot(
                online=False,
                supported=False,
                available_ids=set(),
                available_items={},
                active_id=None,
                error=str(exc),
            )

    def upload(self, tv_ip: str, image_bytes: bytes, file_type: str = "JPEG") -> str:
        tv = self._tv(tv_ip)
        content_id = tv.art().upload(image_bytes, file_type=file_type, matte="none")
        if not content_id:
            raise RuntimeError(f"Upload returned no content_id for {tv_ip}")
        return str(content_id)

    def select_image(self, tv_ip: str, content_id: str) -> None:
        tv = self._tv(tv_ip)
        tv.art().select_image(content_id, show=True)

    def delete_image(self, tv_ip: str, content_id: str) -> bool:
        tv = self._tv(tv_ip)
        return bool(tv.art().delete(content_id))

    def get_thumbnail(self, tv_ip: str, content_id: str) -> bytes:
        last_error: Optional[Exception] = None
        attempts = 2

        for attempt in range(attempts):
            art = self._thumbnail_tv(tv_ip).art()

            try:
                payload = art.get_thumbnail_list([content_id])
                extracted = self._extract_thumbnail_bytes(payload)
                if extracted:
                    return extracted
            except Exception as exc:
                last_error = exc

            if attempt < attempts - 1:
                time.sleep(0.35 * (attempt + 1))

        if last_error:
            raise RuntimeError(f"Thumbnail fetch failed for {content_id} on {tv_ip}: {last_error}") from last_error
        raise RuntimeError(f"No thumbnail bytes returned for {content_id} on {tv_ip}")
