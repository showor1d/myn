"""Web search for MYN's chat: the swappable provider layer, the official server tools,
pause_turn, streaming, the monthly cap and sources. No network: fake API responses."""

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "src" / "core"), str(BASE / "tests")]

import chat
import claude_client
import web_tools
from test_streaming import sse

SEARCH_CALL = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
               "input": {"query": "东京 明天 天气"}}
SEARCH_RESULT = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
    {"type": "web_search_result", "url": "https://www.jma.go.jp/tokyo", "title": "东京天气预报",
     "encrypted_content": "enc==", "page_age": "2026-10-17"}]}
CITED = {"type": "text", "text": "明天东京晴，最高 22 度。", "citations": [
    {"type": "web_search_result_location", "url": "https://www.jma.go.jp/tokyo",
     "title": "东京天气预报", "encrypted_index": "idx==", "cited_text": "晴 22℃"}]}


def reply(content, stop="end_turn", searches=0):
    usage = {"input_tokens": 900, "output_tokens": 40}
    if searches:
        usage["server_tool_use"] = {"web_search_requests": searches}
    return io.BytesIO(json.dumps({"stop_reason": stop, "content": content, "usage": usage},
                                 ensure_ascii=False).encode("utf-8"))


class Providers(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-web-")
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name)

    def test_official_by_default_with_search_and_page_reading(self):
        tools = web_tools.toolset_for(self.config)
        self.assertEqual(tools.provider, "official")
        self.assertEqual(tools.names, ["web_search", "web_fetch"])
        search, fetch = tools.server_tools
        self.assertEqual(search["type"], "web_search_20250305")
        self.assertEqual(search["max_uses"], web_tools.MAX_SEARCHES_PER_ANSWER)
        self.assertEqual(search["user_location"]["timezone"], "Asia/Tokyo")
        self.assertEqual(fetch["type"], "web_fetch_20250910")
        self.assertIn("web_search", tools.note)
        self.assertEqual(tools.client_tools, [])

    def test_settings_round_trip_and_broken_files_fall_back(self):
        web_tools.save_settings(self.config, web_tools.WebSettings("official", 50, False))
        self.assertEqual(web_tools.load_settings(self.config), web_tools.WebSettings("official", 50, False))
        self.assertEqual(web_tools.toolset_for(self.config).names, ["web_search"])
        with self.assertRaises(ValueError):
            web_tools.save_settings(self.config, web_tools.WebSettings("official", -2, True))
        for broken in ("{", "[]", '{"provider": "official", "monthly_limit": "lots"}', '{"surprise": 1}'):
            (self.config / web_tools.SETTINGS_NAME).write_text(broken, encoding="utf-8")
            self.assertEqual(web_tools.load_settings(self.config), web_tools.WebSettings())

    def test_monthly_limit_counts_this_tokyo_month_only(self):
        log = self.config / "claude-usage.jsonl"
        rows = [("2026-10-01T02:00:00+00:00", 3), ("2026-09-30T16:30:00+00:00", 2),  # Oct 1 in Tokyo
                ("2026-09-30T14:00:00+00:00", 7), ("2026-10-10T00:00:00+00:00", None)]
        log.write_text("".join(json.dumps({"at_utc": when, "web_search_requests": count}) + "\n"
                               for when, count in rows) + "not json\n", encoding="utf-8")
        now = datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
        self.assertEqual(web_tools.searches_this_month(log, now), 5)
        web_tools.save_settings(self.config, web_tools.WebSettings("official", 7, True))
        tools = web_tools.toolset_for(self.config, now)
        self.assertEqual(tools.server_tools[0]["max_uses"], 2)  # only 2 left this month
        web_tools.save_settings(self.config, web_tools.WebSettings("official", 5, True))
        tools = web_tools.toolset_for(self.config, now)
        self.assertEqual(tools.names, ["web_fetch"])
        self.assertTrue(tools.limit_reached)
        self.assertIn("limit", tools.note)

    def test_off_and_unknown_providers_mean_no_web_tools(self):
        for provider in ("off", "some-service-removed-later"):
            web_tools.save_settings(self.config, web_tools.WebSettings(provider, 300, True))
            tools = web_tools.toolset_for(self.config)
            self.assertEqual((tools.provider, tools.names), ("off", []))

    def test_sources_from_citations_and_pages_read(self):
        fetched = {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_2", "content": {
            "type": "web_fetch_result", "url": "https://example.com/a",
            "content": {"type": "document", "title": "Article", "source": {}}}}
        found = web_tools.sources([SEARCH_CALL, SEARCH_RESULT, CITED, CITED, fetched,
                                   {"type": "text", "text": "x", "citations": [{"type": "char_location"}]}])
        self.assertEqual(found, [{"url": "https://www.jma.go.jp/tokyo", "title": "东京天气预报"},
                                 {"url": "https://example.com/a", "title": "Article"}])


class ServerToolsInTheClient(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-web-")
        self.addCleanup(temporary.cleanup)
        self.usage = Path(temporary.name) / "claude-usage.jsonl"

    def test_answer_with_search_results_and_usage_count(self):
        payloads, seen = [], []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": "我查一下。"}, SEARCH_CALL, SEARCH_RESULT, CITED],
                         searches=1)

        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        answer = claude_client.send(claude_client.SONNET, "s", "明天天气？", "k", opener=opener,
                                    server_tools=tools, usage_path=self.usage, on_blocks=seen.extend)
        self.assertEqual(answer, "我查一下。明天东京晴，最高 22 度。")
        self.assertEqual(payloads[0]["tools"], tools)
        self.assertEqual(web_tools.sources(seen)[0]["url"], "https://www.jma.go.jp/tokyo")
        record = json.loads(self.usage.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["web_search_requests"], 1)
        self.assertEqual(web_tools.searches_this_month(self.usage), 1)

    def test_paused_turn_is_sent_back_and_continued(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([SEARCH_CALL, SEARCH_RESULT], stop="pause_turn", searches=1)
            return reply([CITED])

        answer = claude_client.send(claude_client.SONNET, "s", "明天天气？", "k", opener=opener,
                                    server_tools=[{"type": "web_search_20250305", "name": "web_search"}])
        self.assertEqual(answer, "明天东京晴，最高 22 度。")
        self.assertEqual(payloads[1]["messages"][-1],
                         {"role": "assistant", "content": [SEARCH_CALL, SEARCH_RESULT]})

    def test_endless_pausing_stops(self):
        opener = lambda request, timeout: reply([SEARCH_CALL, SEARCH_RESULT], stop="pause_turn")
        with self.assertRaises(claude_client.ClaudeUnavailable):
            claude_client.send(claude_client.SONNET, "s", "q", "k", opener=opener,
                               server_tools=[{"type": "web_search_20250305", "name": "web_search"}])

    def test_search_refused_by_the_account_still_answers(self):
        import urllib.error
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                raise urllib.error.HTTPError("u", 400, "web search is not enabled", {}, io.BytesIO(b"{}"))
            return reply([{"type": "text", "text": "明天大概是晴天。"}])

        answer = claude_client.send(claude_client.SONNET, "s", "q", "k", opener=opener,
                                    server_tools=[{"type": "web_search_20250305", "name": "web_search"}])
        self.assertTrue(answer.startswith("明天大概是晴天。"))
        self.assertIn('Web search was unavailable', answer)
        self.assertNotIn("tools", payloads[1])
        # an ordinary bad request without web tools is still an error
        def bad(request, timeout):
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"{}"))
        with self.assertRaises(claude_client.ClaudeUnavailable):
            claude_client.send(claude_client.SONNET, "s", "q", "k", opener=bad)

    def test_speed_settings_are_sent_and_dropped_if_refused(self):
        import urllib.error
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                raise urllib.error.HTTPError("u", 400, "unknown field", {}, io.BytesIO(b"{}"))
            return reply([{"type": "text", "text": "好"}])

        answer = claude_client.send(claude_client.HAIKU, "s", "q", "k", opener=opener, max_tokens=60,
                                    effort="low", thinking={"type": "disabled"})
        self.assertEqual(answer, '好')
        self.assertEqual((payloads[0]["output_config"], payloads[0]["thinking"]),
                         ({"effort": "low"}, {"type": "disabled"}))
        self.assertNotIn("output_config", payloads[1])
        self.assertNotIn("thinking", payloads[1])

    def test_chat_runs_sonnet_at_low_effort(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": "你好"}])

        claude_client.coordinated_reply("s", "你好", "k", opener=opener)
        self.assertEqual(payloads[0]["output_config"], {"effort": claude_client.CHAT_EFFORT})
        self.assertEqual(claude_client.CHAT_EFFORT, "low")

    def test_server_and_client_tool_in_one_turn(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:  # search not run yet, waiting for our tool first
                return reply([SEARCH_CALL, {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                                            "input": {"path": "x"}}], stop="tool_use")
            return reply([SEARCH_RESULT, CITED])

        read_file = {"name": "read_file", "description": "d",
                     "input_schema": {"type": "object", "properties": {}}}
        answer = claude_client.send(
            claude_client.SONNET, "s", "q", "k", opener=opener, read_tools=[read_file],
            tool_executor=lambda name, args: ('{"ok": true}', False),
            server_tools=[{"type": "web_search_20250305", "name": "web_search"}])
        self.assertEqual(answer, "明天东京晴，最高 22 度。")
        self.assertEqual(payloads[1]["messages"][-2]["content"][0], SEARCH_CALL)
        self.assertEqual(payloads[1]["messages"][-1]["content"][0]["tool_use_id"], "toolu_1")

    def test_streamed_search_tells_who_is_searching_and_keeps_citations(self):
        events = [{"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                       "content": [], "usage": {"input_tokens": 9}}},
                  {"type": "content_block_start", "index": 0,
                   "content_block": {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}},
                  {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "input_json_delta", "partial_json": '{"query": "东京'}},
                  {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "input_json_delta", "partial_json": ' 天气"}'}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "content_block_start", "index": 1, "content_block": SEARCH_RESULT},
                  {"type": "content_block_stop", "index": 1},
                  {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 2,
                   "delta": {"type": "citations_delta", "citation": CITED["citations"][0]}},
                  {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "明天晴。"}},
                  {"type": "content_block_stop", "index": 2},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": 5, "server_tool_use": {"web_search_requests": 1}}},
                  {"type": "message_stop"}]
        started, deltas, seen = [], [], []
        answer = claude_client.send(
            claude_client.SONNET, "s", "q", "k", opener=lambda request, timeout: sse(*events),
            on_text=deltas.append, on_tool=started.append, on_blocks=seen.extend,
            server_tools=[{"type": "web_search_20250305", "name": "web_search"}], usage_path=self.usage)
        self.assertEqual((answer, deltas, started), ("明天晴。", ["明天晴。"], ["web_search"]))
        self.assertEqual(seen[0]["input"], {"query": "东京 天气"})
        self.assertEqual(web_tools.sources(seen)[0]["title"], "东京天气预报")
        self.assertEqual(web_tools.searches_this_month(self.usage), 1)


class SwappingProviders(unittest.TestCase):
    """A different search service plugs in without touching the chat code."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="myn-web-")
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "data" / "jarvis.db"
        self.db.parent.mkdir()
        self.session = chat.ChatSession(self.db)

    def ask(self, opener, question="东京明天天气怎么样？"):
        with mock.patch.object(self.session, "_key", return_value="k"), \
                mock.patch.object(claude_client.urllib.request, "urlopen", side_effect=opener):
            return self.session.ask(question)

    def test_official_search_in_a_chat_returns_sources(self):
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([SEARCH_CALL, SEARCH_RESULT, CITED], searches=1)

        result = self.ask(opener)
        self.assertEqual(result["answer"], "明天东京晴，最高 22 度。")
        self.assertEqual(result["web_sources"][0]["url"], "https://www.jma.go.jp/tokyo")
        names = [tool["name"] for tool in payloads[0]["tools"]]
        self.assertIn("web_search", names)
        self.assertIn("web_search", payloads[0]["system"][0]["text"])

    def test_a_new_client_side_provider_just_works(self):
        class FakeSearch(web_tools.Provider):
            id, label, description = "fake", "测试搜索", "for tests"

            def toolset(self, settings, searches_left):
                tools = web_tools.Toolset(provider=self.id, note="You can search with web_search.",
                                          client_tools=[{"name": "web_search", "description": "search",
                                                         "input_schema": {"type": "object", "properties": {
                                                             "query": {"type": "string"}}}}])
                tools.execute = lambda name, args: (json.dumps({"results": ["晴 22℃ " + args["query"]]},
                                                               ensure_ascii=False), False)
                return tools

        web_tools.register(FakeSearch())
        self.addCleanup(web_tools.PROVIDERS.pop, "fake")
        web_tools.save_settings(chat.data_location.config_for_db(self.db),
                                web_tools.WebSettings("fake", 300, True))
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            if len(payloads) == 1:
                return reply([{"type": "tool_use", "id": "toolu_9", "name": "web_search",
                               "input": {"query": "东京"}}], stop="tool_use")
            return reply([{"type": "text", "text": "明天晴。"}])

        result = self.ask(opener)
        self.assertEqual(result["answer"], "明天晴。")
        self.assertNotIn("web_fetch", [tool["name"] for tool in payloads[0]["tools"]])
        tool_result = payloads[1]["messages"][-1]["content"][0]
        self.assertIn("晴 22℃ 东京", tool_result["content"])
        self.assertFalse(tool_result["is_error"])

    def test_turning_search_off_still_chats(self):
        web_tools.save_settings(chat.data_location.config_for_db(self.db), web_tools.WebSettings("off", 300, True))
        payloads = []

        def opener(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return reply([{"type": "text", "text": "你好！"}])

        self.assertEqual(self.ask(opener, "你好")["answer"], "你好！")
        self.assertFalse(any(tool["name"] in ("web_search", "web_fetch") for tool in payloads[0].get("tools", [])))


if __name__ == "__main__":
    unittest.main()
