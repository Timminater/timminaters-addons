from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Set

from samsungtvws import SamsungTVWS


@dataclass
class TVSnapshot:
    online: bool
    supported: bool
    available_ids: Set[str]
    active_id: Optional[str]
    error: Optional[str] = None


class TVClient:
    def __init__(self, timeout: int = 8) -> None:
        self.timeout = timeout

    def _tv(self, tv_ip: str) -> SamsungTVWS:
        return SamsungTVWS(tv_ip, timeout=self.timeout)

    def snapshot(self, tv_ip: str) -> TVSnapshot:
        try:
            tv = self._tv(tv_ip)
            art = tv.art()
            supported = bool(art.supported())
            if not supported:
                return TVSnapshot(online=True, supported=False, available_ids=set(), active_id=None)

            items = art.available("MY-C0002")
            available_ids = {str(item.get("content_id")) for item in items if item.get("content_id")}

            current = art.get_current()
            active_id = current.get("content_id") if isinstance(current, dict) else None
            return TVSnapshot(online=True, supported=True, available_ids=available_ids, active_id=active_id)
        except Exception as exc:  # pragma: no cover - integration behavior
            return TVSnapshot(online=False, supported=False, available_ids=set(), active_id=None, error=str(exc))

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
