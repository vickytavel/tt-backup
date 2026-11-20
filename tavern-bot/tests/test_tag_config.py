"""Runtime tag-model config: settings table + tagging.config() merge."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tavernlib import tagging
from tavernlib.store import LibraryStore


class TagConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = LibraryStore(self.root)
        self.env = mock.patch.dict(
            os.environ,
            {
                "TAVERN_LIBRARY": str(self.root),
                "TAVERN_LLM_BASE_URL": "http://127.0.0.1:9999/v1",
                "TAVERN_LLM_MODEL": "env-model",
                "TAVERN_LLM_API_KEY": "k",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self._tmp.cleanup()

    def test_settings_roundtrip(self) -> None:
        self.assertEqual(self.store.get_setting("tag_model"), "")
        self.store.set_setting("tag_model", "qwen3.8-flash")
        self.assertEqual(self.store.get_setting("tag_model"), "qwen3.8-flash")
        self.store.set_setting("tag_model", "glm-5.3-flash")
        self.assertEqual(self.store.get_setting("tag_model"), "glm-5.3-flash")
        self.store.set_setting("tag_model", "")
        self.assertEqual(self.store.get_setting("tag_model"), "")

    def test_stored_setting_reads_directly(self) -> None:
        self.store.set_setting("tag_model", "kimi-k3")
        self.assertEqual(tagging._stored_setting("tag_model"), "kimi-k3")

    def test_db_model_overrides_env(self) -> None:
        self.assertEqual(tagging.config()["model"], "env-model")
        self.store.set_setting("tag_model", "minimax-m3")
        self.assertEqual(tagging.config()["model"], "minimax-m3")

    def test_blank_setting_falls_back_to_env(self) -> None:
        self.store.set_setting("tag_model", "")
        self.assertEqual(tagging.config()["model"], "env-model")

    def test_missing_db_degrades_to_env(self) -> None:
        (self.root / "library.db").unlink()
        self.assertEqual(tagging._stored_setting("tag_model"), "")
        self.assertEqual(tagging.config()["model"], "env-model")


if __name__ == "__main__":
    unittest.main()
