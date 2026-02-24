import unittest

from app.runtime import RuntimeState


class RuntimeStateTests(unittest.TestCase):
    def test_snapshot_cache_hit_and_miss(self):
        runtime = RuntimeState(snapshot_ttl_seconds=20)
        calls = {"count": 0}

        def fetcher(tv_ip):
            calls["count"] += 1
            return {"ip": tv_ip, "value": calls["count"]}

        first, from_cache_first = runtime.get_or_fetch_snapshot("192.168.1.10", fetcher, force=False)
        second, from_cache_second = runtime.get_or_fetch_snapshot("192.168.1.10", fetcher, force=False)

        self.assertFalse(from_cache_first)
        self.assertTrue(from_cache_second)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(first["value"], second["value"])

    def test_forced_refresh_bypasses_cache(self):
        runtime = RuntimeState(snapshot_ttl_seconds=20)
        calls = {"count": 0}

        def fetcher(tv_ip):
            calls["count"] += 1
            return {"ip": tv_ip, "value": calls["count"]}

        runtime.get_or_fetch_snapshot("192.168.1.10", fetcher, force=False)
        result, from_cache = runtime.get_or_fetch_snapshot("192.168.1.10", fetcher, force=True)

        self.assertFalse(from_cache)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(result["value"], 2)


if __name__ == "__main__":
    unittest.main()
