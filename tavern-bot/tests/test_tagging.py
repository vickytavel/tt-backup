import os
import unittest
from unittest import mock

from tavernlib import tagging


def _pieces():
    return [
        {"kind": "body", "title": "描述", "text": "他开了二十年诊所，手上有一道旧伤。"},
        {"kind": "greeting", "title": "开场白", "text": "雨很大，你推门进来。"},
        {
            "kind": "world_entry",
            "title": "旧伤来历",
            "text": "疤是早年落下的。",
            "meta": {"keys": ["旧伤"], "enabled": True},
        },
    ]


class TaggingTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {
                "TAVERN_LLM_BASE_URL": "http://127.0.0.1:9999/v1",
                "TAVERN_LLM_MODEL": "test-model",
                "TAVERN_LLM_JSON_MODE": "1",
            },
            clear=False,
        )
        self.env.start()
        os.environ.pop("TAVERN_LLM_API_KEY", None)

    def tearDown(self):
        self.env.stop()

    def test_prompt_contains_axes_and_content(self):
        messages = tagging.build_messages({"title": "雨巷医生"}, _pieces())
        user = messages[1]["content"]
        self.assertIn("### 世界", user)
        self.assertIn("雨巷医生", user)
        self.assertIn("旧伤来历", user)
        self.assertIn("雨很大", user)
        self.assertLess(len(user), 40000)

    def test_alias_folds_and_unknown_drops(self):
        kept, dropped = tagging.normalize(
            [
                {"name": "现代架空", "evidence": "都市"},
                {"name": "BE", "evidence": "结局"},
                {"name": "根本不存在的标签"},
                {"name": "现代"},
            ]
        )
        self.assertEqual([tag["name"] for tag in kept], ["现代"])
        self.assertEqual(kept[0]["bucket"], "world")
        self.assertEqual(len(dropped), 2)

    def test_suggest_parses_fenced_json(self):
        fake = lambda messages: '```json\n{"tags":[{"name":"古代","evidence":"古装"}],"confidence":0.7}\n```'
        result = tagging.suggest({"title": "x"}, _pieces(), client=fake)
        self.assertEqual(result["tags"][0]["name"], "古代")
        self.assertEqual(result["confidence"], 0.7)

    def test_suggest_requires_pieces(self):
        with self.assertRaises(tagging.TaggingError):
            tagging.suggest({"title": "x"}, [], client=lambda m: "{}")

    def test_suggest_requires_config(self):
        os.environ.pop("TAVERN_LLM_BASE_URL", None)
        os.environ.pop("TAVERN_LLM_MODEL", None)
        with mock.patch.dict(os.environ, {"TAVERN_LLM_BASE_URL": "", "TAVERN_LLM_MODEL": ""}):
            with self.assertRaises(tagging.TaggingError):
                tagging.suggest({"title": "x"}, _pieces(), client=lambda m: "{}")

    def test_apply_passes_ai_source(self):
        received = {}

        class FakeStore:
            def set_item_tags(self, item_id, assignments, **kwargs):
                received["item_id"] = item_id
                received["assignments"] = assignments
                received["kwargs"] = kwargs
                return []

        tagging.apply(FakeStore(), 7, [{"bucket": "world", "name": "现代"}])
        self.assertEqual(received["item_id"], 7)
        self.assertEqual(received["assignments"], [{"bucket": "world", "name": "现代"}])
        self.assertEqual(received["kwargs"]["source"], "ai")

    def test_call_sends_configured_user_agent(self):
        """WAF-fronted proxies reject the default Python UA, so it must be explicit."""
        seen: dict[str, str] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"[]"}}]}'

        def fake_urlopen(request, timeout=None):
            seen["ua"] = request.get_header("User-agent")
            seen["auth"] = request.get_header("Authorization")
            seen["url"] = request.full_url
            return FakeResponse()

        env = {
            "TAVERN_LLM_BASE_URL": "https://example.test/v1",
            "TAVERN_LLM_MODEL": "test-model",
            "TAVERN_LLM_API_KEY": "k-123",
            "TAVERN_LLM_USER_AGENT": "opencode/1.0",
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "tavernlib.tagging.urllib.request.urlopen", fake_urlopen
        ):
            tagging._call([{"role": "user", "content": "hi"}])

        self.assertEqual(seen["ua"], "opencode/1.0")
        self.assertEqual(seen["auth"], "Bearer k-123")
        self.assertTrue(seen["url"].endswith("/v1/chat/completions"))

    def test_user_agent_defaults_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TAVERN_LLM_USER_AGENT", None)
            self.assertEqual(tagging.config()["user_agent"], tagging.DEFAULT_USER_AGENT)


if __name__ == "__main__":
    unittest.main()