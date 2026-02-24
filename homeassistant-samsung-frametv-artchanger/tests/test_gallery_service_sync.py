import io
import os
import sys
import tempfile
import threading
import time
import types
import unittest

from PIL import Image

if "samsungtvws" not in sys.modules:
    sys.modules["samsungtvws"] = types.SimpleNamespace(SamsungTVWS=object)

from app.config import Settings
from app.media import MediaService
from app.runtime import RuntimeState
from app.service import GalleryService
from app.store import StateStore, default_state


TV_IP = "192.168.10.170"


class FakeTVClient:
    def __init__(self, snapshot, thumbnail_bytes: bytes, delay_seconds: float = 0.0) -> None:
        self._snapshot = snapshot
        self._thumbnail_bytes = thumbnail_bytes
        self.thumbnail_calls = 0
        self.delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._active_calls = 0
        self.max_parallel_calls = 0

    def snapshot(self, tv_ip: str):
        return self._snapshot

    def get_thumbnail(self, tv_ip: str, content_id: str) -> bytes:
        with self._lock:
            self.thumbnail_calls += 1
            self._active_calls += 1
            if self._active_calls > self.max_parallel_calls:
                self.max_parallel_calls = self._active_calls
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        with self._lock:
            self._active_calls -= 1
        return self._thumbnail_bytes


class FakeTVClientActivation:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot
        self.upload_calls = 0
        self.select_calls = 0

    def snapshot(self, tv_ip: str):
        return self._snapshot

    def upload(self, tv_ip: str, image_bytes: bytes, file_type: str = "JPEG") -> str:
        self.upload_calls += 1
        return "cid-new-upload"

    def select_image(self, tv_ip: str, content_id: str) -> None:
        self.select_calls += 1


class GalleryServiceSyncTests(unittest.TestCase):
    def _sample_image(self, width: int = 1920, height: int = 1080, color=(90, 140, 220)) -> bytes:
        image = Image.new("RGB", (width, height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        return buffer.getvalue()

    def _make_service(self, tv_client: FakeTVClient):
        self.tmp = tempfile.TemporaryDirectory()
        media_dir = os.path.join(self.tmp.name, "media")
        data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(media_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

        settings = Settings(
            tv_ips=[TV_IP],
            media_dir=media_dir,
            data_dir=data_dir,
            state_path=os.path.join(data_dir, "gallery_state.json"),
            automation_token="",
            refresh_interval_seconds=30,
            snapshot_ttl_seconds=20,
            runtime_settings_path=os.path.join(data_dir, "runtime_settings.json"),
        )
        store = StateStore(settings.state_path)
        service = GalleryService(
            settings=settings,
            store=store,
            media_service=MediaService(),
            tv_client=tv_client,
            runtime=RuntimeState(snapshot_ttl_seconds=20),
        )
        return service, store

    def tearDown(self) -> None:
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_refresh_imports_unknown_tv_items(self):
        snapshot = types.SimpleNamespace(
            online=True,
            supported=True,
            available_ids={"cid-001"},
            available_items={"cid-001": {"content_id": "cid-001", "title": "TV art 1"}},
            active_id="cid-001",
            error=None,
        )
        service, store = self._make_service(FakeTVClient(snapshot, self._sample_image()))

        service.bootstrap()
        service._perform_refresh(force_snapshot=True)

        state = store.load()
        self.assertEqual(len(state["assets"]), 1)
        asset = next(iter(state["assets"].values()))
        self.assertEqual(asset["source"], "tv_discovery")
        self.assertIsNone(asset["ha_rel_path"])
        self.assertIn(TV_IP, asset["tv_map"])
        self.assertEqual(asset["tv_map"][TV_IP]["content_id"], "cid-001")
        self.assertTrue(asset["tv_map"][TV_IP]["on_tv"])
        self.assertTrue(asset["tv_map"][TV_IP]["active"])

    def test_read_thumbnail_uses_tv_when_ha_file_is_missing(self):
        snapshot = types.SimpleNamespace(
            online=True,
            supported=True,
            available_ids=set(),
            available_items={},
            active_id=None,
            error=None,
        )
        tv_client = FakeTVClient(snapshot, self._sample_image())
        service, store = self._make_service(tv_client)

        service.bootstrap()
        state = default_state()
        state["assets"]["tv-asset-1"] = {
            "asset_id": "tv-asset-1",
            "filename": "TV Thumb",
            "ha_rel_path": None,
            "source": "tv_discovery",
            "created_at": "2026-02-23T00:00:00+00:00",
            "updated_at": "2026-02-23T00:00:00+00:00",
            "tv_map": {
                TV_IP: {
                    "content_id": "cid-thumb",
                    "on_tv": True,
                    "active": True,
                    "last_seen_at": "2026-02-23T00:00:00+00:00",
                    "error": None,
                }
            },
        }
        store.save(state)

        first = service.read_thumbnail("tv-asset-1")

        deadline = time.time() + 3.0
        second = first
        while time.time() < deadline:
            second = service.read_thumbnail("tv-asset-1")
            if second != first and tv_client.thumbnail_calls >= 1:
                break
            time.sleep(0.05)

        third = service.read_thumbnail("tv-asset-1")

        self.assertGreaterEqual(tv_client.thumbnail_calls, 1)
        self.assertGreater(len(first), 0)
        self.assertNotEqual(first, second)
        self.assertEqual(second, third)

        image = Image.open(io.BytesIO(second))
        self.assertEqual(image.size, (640, 360))

    def test_tv_thumbnail_queue_serializes_different_content_requests(self):
        snapshot = types.SimpleNamespace(
            online=True,
            supported=True,
            available_ids=set(),
            available_items={},
            active_id=None,
            error=None,
        )
        tv_client = FakeTVClient(snapshot, self._sample_image(), delay_seconds=0.2)
        service, store = self._make_service(tv_client)

        service.bootstrap()
        state = default_state()
        state["assets"]["tv-asset-a"] = {
            "asset_id": "tv-asset-a",
            "filename": "A",
            "ha_rel_path": None,
            "source": "tv_discovery",
            "created_at": "2026-02-23T00:00:00+00:00",
            "updated_at": "2026-02-23T00:00:00+00:00",
            "tv_map": {TV_IP: {"content_id": "cid-a", "on_tv": True, "active": False, "error": None}},
        }
        state["assets"]["tv-asset-b"] = {
            "asset_id": "tv-asset-b",
            "filename": "B",
            "ha_rel_path": None,
            "source": "tv_discovery",
            "created_at": "2026-02-23T00:00:00+00:00",
            "updated_at": "2026-02-23T00:00:00+00:00",
            "tv_map": {TV_IP: {"content_id": "cid-b", "on_tv": True, "active": False, "error": None}},
        }
        store.save(state)

        barrier = threading.Barrier(3)
        results = []

        def worker(asset_id: str) -> None:
            barrier.wait()
            payload = service.read_thumbnail(asset_id)
            results.append(payload)

        t1 = threading.Thread(target=worker, args=("tv-asset-a",))
        t2 = threading.Thread(target=worker, args=("tv-asset-b",))
        t1.start()
        t2.start()
        barrier.wait()
        t1.join()
        t2.join()

        deadline = time.time() + 4.0
        while time.time() < deadline and tv_client.thumbnail_calls < 2:
            time.sleep(0.05)

        self.assertEqual(tv_client.thumbnail_calls, 2)
        self.assertEqual(tv_client.max_parallel_calls, 1)
        self.assertEqual(len(results), 2)

    def test_tv_thumbnail_queue_deduplicates_same_content_request(self):
        snapshot = types.SimpleNamespace(
            online=True,
            supported=True,
            available_ids=set(),
            available_items={},
            active_id=None,
            error=None,
        )
        tv_client = FakeTVClient(snapshot, self._sample_image(), delay_seconds=0.2)
        service, store = self._make_service(tv_client)

        service.bootstrap()
        state = default_state()
        state["assets"]["tv-asset-shared"] = {
            "asset_id": "tv-asset-shared",
            "filename": "Shared",
            "ha_rel_path": None,
            "source": "tv_discovery",
            "created_at": "2026-02-23T00:00:00+00:00",
            "updated_at": "2026-02-23T00:00:00+00:00",
            "tv_map": {TV_IP: {"content_id": "cid-shared", "on_tv": True, "active": False, "error": None}},
        }
        store.save(state)

        barrier = threading.Barrier(3)
        results = []

        def worker() -> None:
            barrier.wait()
            payload = service.read_thumbnail("tv-asset-shared")
            results.append(payload)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        barrier.wait()
        t1.join()
        t2.join()

        deadline = time.time() + 3.0
        while time.time() < deadline and tv_client.thumbnail_calls < 1:
            time.sleep(0.05)

        self.assertEqual(tv_client.thumbnail_calls, 1)
        self.assertEqual(tv_client.max_parallel_calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_legacy_candidates_include_share_location(self):
        snapshot = types.SimpleNamespace(
            online=True,
            supported=True,
            available_ids=set(),
            available_items={},
            active_id=None,
            error=None,
        )
        service, _ = self._make_service(FakeTVClient(snapshot, self._sample_image()))

        candidates = service._legacy_uploaded_files_candidates()
        expected = os.path.join("/share", "SamsungFrameTVArtChanger", "uploaded_files.json")
        self.assertIn(expected, candidates)

    def test_legacy_content_mapping_prevents_reupload_duplicates(self):
        snapshot = types.SimpleNamespace(
            online=True,
            supported=True,
            available_ids={"cid-legacy"},
            available_items={"cid-legacy": {"content_id": "cid-legacy", "title": "Legacy mapped art"}},
            active_id=None,
            error=None,
        )
        service, store = self._make_service(FakeTVClientActivation(snapshot))

        local_filename = "photo.jpg"
        local_path = os.path.join(service.settings.media_dir, local_filename)
        with open(local_path, "wb") as handle:
            handle.write(self._sample_image())

        service.bootstrap()
        state = store.load()

        legacy_file = os.path.join(self.tmp.name, "uploaded_files.json")
        with open(legacy_file, "w", encoding="utf-8") as handle:
            handle.write(
                '[{"file":"photo.jpg","remote_filename":"cid-legacy","tv_ip":"192.168.10.170","source":"media_folder"}]'
            )

        service._legacy_uploaded_files_candidates = lambda: [legacy_file]
        changed = service._migrate_legacy_uploaded_files(state)
        self.assertTrue(changed)
        store.save(state)

        assets = state["assets"]
        self.assertEqual(len(assets), 1)
        asset_id = next(iter(assets.keys()))
        asset = assets[asset_id]
        self.assertIn(TV_IP, asset["tv_map"])
        self.assertEqual(asset["tv_map"][TV_IP]["content_id"], "cid-legacy")

        # Keep this unit test deterministic and avoid background refresh touching temp paths.
        service.refresh = lambda force=False, wait=False: store.load()

        result = service.activate_asset(asset_id, tv_ips=[TV_IP], ensure_upload=True, activate=True)
        self.assertTrue(result["results"][TV_IP]["ok"])
        self.assertEqual(service.tv_client.upload_calls, 0)
        self.assertEqual(service.tv_client.select_calls, 1)


if __name__ == "__main__":
    unittest.main()
