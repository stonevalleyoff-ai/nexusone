"""Faux serveur compatible OpenAI, calqué sur la passerelle de colibrì (c/openai_server.py, v2.2.0).

Il reproduit ses règles utiles aux tests : un message système seulement en tête (gabarit
Qwen3.6), HTTP 400 pour `tools` ou `response_format` sur un moteur qui ne les gère pas,
clé d'accès facultative, flux SSE avec ping `reasoning_content`, appels d'outils en fin de
flux, puis l'usage et `[DONE]`.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


class FakeColibri:
    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []
        self.model_id = 'qwen36-35b'
        self.arch = 'qwen36'
        self.context_length = 32768
        self.chunks = ['Bon', 'jour', ' !']
        self.tool_calls: list[tuple[str, dict]] = []   # demandés à la 1re requête qui propose des outils
        self.always_tool: tuple[str, dict] | None = None   # modèle qui réclame toujours cet outil
        self.tools_supported = True
        self.format_supported = True
        self.api_key: str | None = None
        self.delay = 0.0
        self.status: int | None = None   # forcer un statut d'erreur (429, 503…)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def _error(self, code, message, param=None, error_code=None):
                self._send(code, {'error': {'message': message, 'type': 'invalid_request_error',
                                            'param': param, 'code': error_code}})

            def _authed(self):
                return fake.api_key is None or self.headers.get('Authorization') == 'Bearer ' + fake.api_key

            def do_GET(self):
                fake.requests.append((self.path, None))
                if self.path == '/health':
                    payload = {'status': 'ok'}
                    if self._authed():
                        payload['arch'] = fake.arch
                    return self._send(200, payload)
                if not self._authed():
                    return self._error(401, 'Invalid API key.', error_code='invalid_api_key')
                if self.path == '/v1/models':
                    return self._send(200, {'object': 'list', 'data': [
                        {'id': fake.model_id, 'object': 'model', 'owned_by': 'colibri',
                         'capabilities': ['chat', 'systemone'], 'context_length': fake.context_length},
                        {'id': 'qwen-image-2.1', 'object': 'model', 'capabilities': ['image_generation']}]})
                return self._error(404, 'Not found.')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                fake.requests.append((self.path, body))
                if not self._authed():
                    return self._error(401, 'Invalid API key.', error_code='invalid_api_key')
                if self.path != '/v1/chat/completions':
                    return self._error(404, 'Not found.')
                if fake.status:
                    return self._error(fake.status, 'The server is busy.', error_code='queue_full')
                messages = body.get('messages', [])
                for index, message in enumerate(messages):
                    if message.get('role') == 'system' and index > 0:
                        return self._error(400, 'System message must be at the beginning.', f'messages.{index}.role')
                    if message.get('role') == 'tool' and not message.get('tool_call_id'):
                        return self._error(400, 'A tool message needs a `tool_call_id`.', f'messages.{index}')
                    for call in message.get('tool_calls') or []:
                        if not isinstance(call.get('function', {}).get('arguments'), str):
                            return self._error(400, '`arguments` must be a JSON string.', f'messages.{index}')
                uses_tools = body.get('tools') or any(m.get('role') == 'tool' or m.get('tool_calls') for m in messages)
                if uses_tools and not fake.tools_supported:
                    return self._error(400, f'`tools` are not supported by the {fake.arch} engine.', 'tools',
                                       'unsupported_parameter')
                if body.get('response_format') and not fake.format_supported:
                    return self._error(400, f'`response_format` grammars are not supported by the {fake.arch} '
                                            'engine yet.', 'response_format', 'unsupported_parameter')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True

                def event(choices, usage=None):
                    data = {'id': 'chatcmpl-test', 'object': 'chat.completion.chunk', 'created': 0,
                            'model': fake.model_id, 'choices': choices, 'usage': usage}
                    self.wfile.write(f'data: {json.dumps(data)}\n\n'.encode())
                    self.wfile.flush()

                try:
                    event([{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}])
                    event([{'index': 0, 'delta': {'reasoning_content': ''}, 'finish_reason': None}])  # ping
                    time.sleep(fake.delay)
                    calls, fake.tool_calls = (fake.tool_calls, []) if body.get('tools') else ([], fake.tool_calls)
                    if fake.always_tool:
                        calls = [fake.always_tool]
                    if calls:
                        for i, (name, args) in enumerate(calls):
                            event([{'index': 0, 'delta': {'tool_calls': [{
                                'index': i, 'id': f'call_{i}', 'type': 'function',
                                'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'finish_reason': None}])
                        finish = 'tool_calls'
                    else:
                        for text in fake.chunks:
                            event([{'index': 0, 'delta': {'content': text}, 'finish_reason': None}])
                        finish = 'stop'
                    event([{'index': 0, 'delta': {}, 'finish_reason': finish}])
                    event([], {'prompt_tokens': 42, 'completion_tokens': 3, 'total_tokens': 45})
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # le client a annulé : normal

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()

    def chats(self) -> list[dict]:
        return [body for path, body in self.requests if path == '/v1/chat/completions']
