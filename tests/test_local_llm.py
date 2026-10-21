"""Future local service contract; no installed model or paid API is required."""
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / 'src'), str(BASE / 'src/core')]
import chat
import local_llm


class LocalInterfaceTests(unittest.TestCase):
    config = local_llm.Config('http://127.0.0.1:11434/api/chat', 'future-test-model')

    def test_default_and_partial_configuration_never_connect(self):
        with mock.patch.object(local_llm._transport, 'open') as transport:
            for env in ({}, {'MYN_LOCAL_LLM_MODEL': 'model'},
                        {'MYN_LOCAL_LLM_URL': self.config.url}):
                self.assertIsNone(local_llm.configuration(env))
            with self.assertRaises(local_llm.LocalLLMUnavailable):
                local_llm.answer(None, 'q', [])
            transport.assert_not_called()

    def test_only_explicit_loopback_endpoint_is_accepted(self):
        for url in (self.config.url, 'http://[::1]:1234/api/chat'):
            self.assertEqual(local_llm.configuration({'MYN_LOCAL_LLM_URL': url,
                'MYN_LOCAL_LLM_MODEL': self.config.model}), local_llm.Config(url, self.config.model))
        for url in ('https://example.com/api/chat', 'http://192.168.1.2/api/chat',
                    'http://localhost/api/chat', 'file:///api/chat',
                    'http://user:pass@127.0.0.1/api/chat', 'http://127.0.0.1/api/chat?key=x',
                    'http://127.0.0.1/api/chat#x', 'http://127.0.0.1:0/api/chat',
                    'http://127.0.0.1:99999/api/chat', 'http://127.0.0.1/api/generate',
                    'http://127.0.0.1\n/api/chat', 'http://[::1/api/chat'):
            with self.subTest(url=url):
                self.assertIsNone(local_llm.configuration({'MYN_LOCAL_LLM_URL': url,
                                                          'MYN_LOCAL_LLM_MODEL': 'test-model'}))

    def test_payload_contains_only_supplied_evidence_and_style(self):
        rows = [{'id': 1, 'raw_text': 'today I studied English'}]
        payload = {'done': True, 'message': {'content': json.dumps(
            {'answer': 'English', 'citations': [1], 'grounded': True})}}
        with mock.patch.object(local_llm._transport, 'open', return_value=io.BytesIO(
                json.dumps(payload).encode())) as transport:
            answer, sources = chat._local_rag(self.config, 'what did I study?', rows, 'Be concise')
        request = transport.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(request.full_url, self.config.url)
        self.assertEqual(sent['model'], self.config.model)
        self.assertFalse(sent['stream'])
        self.assertNotIn('tools', sent)
        self.assertNotIn('think', sent)  # no model-specific generation setting
        self.assertIn('Be concise', sent['messages'][0]['content'])
        self.assertEqual(json.loads(sent['messages'][1]['content'])['memories'], rows)
        self.assertEqual([row['id'] for row in sources], [1])

    def test_transport_failures_and_invalid_payloads_are_unavailable(self):
        bodies = [b'[]', b'null', b'{}', b'not-json', b'\xff',
                  b'{"done":false}', b'{"done":true,"message":null}',
                  b'{"done":true,"message":{"content":5}}', b'x'*(local_llm.MAX_RESPONSE+1)]
        for body in bodies:
            with self.subTest(body=body[:70]), mock.patch.object(local_llm._transport, 'open',
                    return_value=io.BytesIO(body)), self.assertRaises(local_llm.LocalLLMUnavailable):
                local_llm.answer(self.config, 'q', [])
        with mock.patch.object(local_llm._transport, 'open', side_effect=OSError('offline')), \
                self.assertRaises(local_llm.LocalLLMUnavailable):
            local_llm.answer(self.config, 'q', [])

    def test_invalid_citation_is_rejected(self):
        raw = json.dumps({'answer': 'invented', 'citations': [999], 'grounded': True})
        with mock.patch.object(local_llm, 'answer', return_value=raw), \
                self.assertRaisesRegex(ValueError, chat.INVALID_ANSWER):
            chat._local_rag(self.config, 'q', [{'id': 1, 'raw_text': 'English'}])

    def test_real_loopback_request_ignores_proxy_and_rejects_redirect(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if len(received) == 1:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"done":true,"message":{"content":"{}"}}')
                else:
                    self.send_response(302)
                    self.send_header('Location', '/redirected')
                    self.end_headers()

            def do_GET(self):
                received.append('unexpected redirect')
                self.send_response(200)
                self.end_headers()

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = local_llm.Config('http://127.0.0.1:%d/api/chat' % server.server_port, 'mock-model')
            with mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                self.assertEqual(local_llm.answer(config, 'q', []), '{}')
                with self.assertRaises(local_llm.LocalLLMUnavailable):
                    local_llm.answer(config, 'q', [])
            self.assertEqual(len(received), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
