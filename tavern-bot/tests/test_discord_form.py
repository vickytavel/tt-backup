"""Regression tests for the Discord questionnaire edit logic.

These cover the layer above LibraryStore: applying modal submissions and
tag-select submissions. The Discord views themselves are not rendered here;
order_tag_choices is tested as the shared source of the 24-option window.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tavernlib.discord_form import apply_modal, apply_tag_select, order_tag_choices
from tavernlib.store import LibraryStore


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = LibraryStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_item(self) -> dict:
        item, _ = self.store.add_file(
            b"form-test",
            type="character",
            subtype=None,
            title="测试卡",
            original_filename="card.json",
        )
        return self.store.get_enriched(int(item["id"]))

    def basic_names(self, item_id: int) -> set[str]:
        item = self.store.get_enriched(item_id)
        return {t["name"] for t in (item.get("tags") or []) if t.get("bucket") == "basic"}

    def genre_names(self, item_id: int) -> set[str]:
        item = self.store.get_enriched(item_id)
        return {t["name"] for t in (item.get("tags") or []) if t.get("bucket") == "genre"}


class BasicModalTests(StoreCase):
    def test_age_gender_address_coexist(self) -> None:
        item = self.make_item()
        item_id = int(item["id"])
        apply_modal(self.store, item, "basic", {"age": "22", "gender": "女", "address": "老师"})
        self.assertEqual(self.basic_names(item_id), {"年龄:22", "女", "称呼:老师"})

    def test_updating_one_field_keeps_the_others(self) -> None:
        item = self.make_item()
        item_id = int(item["id"])
        apply_modal(self.store, item, "basic", {"age": "22", "gender": "女", "address": "老师"})
        # 只改年龄：性别和称呼必须保留（旧实现会在填性别时清空整个 basic 桶）。
        apply_modal(self.store, self.store.get_enriched(item_id), "basic", {"age": "23"})
        self.assertEqual(self.basic_names(item_id), {"年龄:23", "女", "称呼:老师"})
        # 只改性别：年龄、称呼保留，旧的裸性别被替换。
        apply_modal(self.store, self.store.get_enriched(item_id), "basic", {"gender": "男"})
        self.assertEqual(self.basic_names(item_id), {"年龄:23", "男", "称呼:老师"})
        # 只改称呼。
        apply_modal(self.store, self.store.get_enriched(item_id), "basic", {"address": "前辈"})
        self.assertEqual(self.basic_names(item_id), {"年龄:23", "男", "称呼:前辈"})

    def test_custom_gender_replaces_bare_value_only(self) -> None:
        item = self.make_item()
        item_id = int(item["id"])
        apply_modal(self.store, item, "basic", {"age": "22", "gender": "女"})
        apply_modal(self.store, self.store.get_enriched(item_id), "basic", {"gender": "流体"})
        self.assertEqual(self.basic_names(item_id), {"年龄:22", "性别:流体"})

    def test_unrelated_basic_tags_survive(self) -> None:
        item = self.make_item()
        item_id = int(item["id"])
        # 一个不由弹窗管理的 basic 标签。
        self.store.set_item_tags(item_id, [{"bucket": "basic", "name": "特殊标记", "source": "user"}], source="user")
        apply_modal(self.store, self.store.get_enriched(item_id), "basic", {"age": "成年", "gender": "非二元"})
        self.assertEqual(self.basic_names(item_id), {"年龄:成年", "非二元", "特殊标记"})
        # 空提交不应清掉任何东西。
        apply_modal(self.store, self.store.get_enriched(item_id), "basic", {})
        self.assertEqual(self.basic_names(item_id), {"年龄:成年", "非二元", "特殊标记"})


class TagOverflowTests(StoreCase):
    def fill_genre(self, count: int) -> None:
        for i in range(count):
            self.store.ensure_tag("genre", f"标签{i:02d}", source="user")

    def assign(self, item_id: int, names: list[str]) -> dict:
        self.store.set_item_tags(
            item_id,
            [{"bucket": "genre", "name": n, "source": "user"} for n in names],
            replace_buckets=["genre"],
            source="user",
        )
        return self.store.get_enriched(item_id)

    def test_selected_tag_is_inside_the_visible_window(self) -> None:
        self.fill_genre(30)
        item = self.make_item()
        item_id = int(item["id"])
        # 标签29 按名称排在最后，旧实现的下拉前 24 个里看不到它。
        item = self.assign(item_id, ["标签29"])
        selected = {int(t["id"]) for t in item["tags"] if t.get("bucket") == "genre"}
        ordered = order_tag_choices(self.store.list_tags("genre"), selected)
        visible_names = {t["name"] for t in ordered[:24]}
        self.assertIn("标签29", visible_names)
        self.assertEqual(ordered[0]["name"], "标签29")

    def test_submit_keeps_selected_tags_outside_the_window(self) -> None:
        self.fill_genre(30)
        item = self.make_item()
        item_id = int(item["id"])
        all_names = [f"标签{i:02d}" for i in range(30)]
        item = self.assign(item_id, all_names)
        selected = {int(t["id"]) for t in item["tags"] if t.get("bucket") == "genre"}
        choices = self.store.list_tags("genre")
        ordered = order_tag_choices(choices, selected)
        visible = ordered[:24]
        # 只有"已选但落在窗口外"的会被保留；桶里的种子标签等未选项不参与。
        invisible_names = {t["name"] for t in ordered[24:] if int(t["id"]) in selected}
        self.assertEqual(len(invisible_names), 6)
        # 用户只保留可见窗口里的第一个，其余可见的取消勾选。
        keep = str(visible[0]["id"])
        apply_tag_select(self.store, item, "genre", [keep])
        self.assertEqual(self.genre_names(item_id), invisible_names | {visible[0]["name"]})

    def test_small_bucket_still_replaces_normally(self) -> None:
        self.fill_genre(5)
        item = self.make_item()
        item_id = int(item["id"])
        item = self.assign(item_id, ["标签00", "标签01"])
        choices = self.store.list_tags("genre")
        picked = [str(t["id"]) for t in choices if t["name"] in {"标签02", "标签03"}]
        apply_tag_select(self.store, item, "genre", picked)
        self.assertEqual(self.genre_names(item_id), {"标签02", "标签03"})
        # 显式清空仍然清空整个桶。
        apply_tag_select(self.store, self.store.get_enriched(item_id), "genre", ["__clear__"])
        self.assertEqual(self.genre_names(item_id), set())


if __name__ == "__main__":
    unittest.main()
