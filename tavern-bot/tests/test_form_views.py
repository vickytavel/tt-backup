import asyncio
import tempfile
import unittest
from pathlib import Path

import discord

from tavernlib.discord_form import section_view, tag_bucket_view
from tavernlib.store import LibraryStore


def run(coro_fn):
    def wrapper(self):
        return asyncio.run(coro_fn(self))

    return wrapper


class TagPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LibraryStore(Path(self.tmp.name) / "lib")
        self.item = {"id": 1, "type": "character", "tags": []}

    def tearDown(self):
        self.tmp.cleanup()

    def _selects(self, view):
        return [c for c in view.children if isinstance(c, discord.ui.Select)]

    @run
    async def test_large_axis_splits_into_subs(self):
        view = section_view(self.item, "tag:world", self.store)
        self.assertEqual(self._selects(view), [])
        labels = [getattr(c, "label", "") for c in view.children]
        self.assertIn("时代", labels)

    @run
    async def test_sub_shows_select_and_back(self):
        view = section_view(self.item, "tag:world/时代", self.store)
        selects = self._selects(view)
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0].custom_id, "tlib:tagsel:1:world/时代")
        self.assertLessEqual(len(selects[0].options), 25)
        self.assertIn("返回子类", [getattr(c, "label", "") for c in view.children])

    @run
    async def test_small_axis_shows_select_directly(self):
        view = section_view(self.item, "tag:gameplay", self.store)
        selects = self._selects(view)
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0].custom_id, "tlib:tagsel:1:gameplay")
        self.assertNotIn("返回子类", [getattr(c, "label", "") for c in view.children])

    @run
    async def test_selected_tags_sort_first(self):
        choices = self.store.list_tags("gameplay")
        selected = {int(choices[-1]["id"])}
        view = tag_bucket_view(1, "gameplay", choices, selected)
        options = self._selects(view)[0].options
        self.assertEqual(len([o for o in options if o.default]), 1)
        self.assertTrue(options[1].default)


if __name__ == "__main__":
    unittest.main()
