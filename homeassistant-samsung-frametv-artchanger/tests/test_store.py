import os
import tempfile
import unittest

from app.store import StateStore, default_state


class StateStoreTests(unittest.TestCase):
    def test_load_default_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store = StateStore(path)
            self.assertEqual(store.load(), default_state())

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store = StateStore(path)

            state = default_state()
            state["assets"]["abc"] = {
                "asset_id": "abc",
                "filename": "abc.jpg",
                "ha_rel_path": "abc.jpg",
                "tv_map": {},
            }

            store.save(state)
            loaded = store.load()
            self.assertIn("abc", loaded["assets"])
            self.assertEqual(loaded["assets"]["abc"]["filename"], "abc.jpg")


if __name__ == "__main__":
    unittest.main()
