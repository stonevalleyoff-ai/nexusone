"""Faux serveur Ollama minimal pour les tests (aucun modèle réel)."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


def fake_vector(text: str, dims: int = 64) -> list[float]:
    """« Embedding » déterministe : sac de mots haché. Assez pour tester la mécanique."""
    import hashlib
    vector = [0.0] * dims
    for word in text.lower().split():
        word = ''.join(c for c in word if c.isalnum())
        if len(word) > 2:
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % dims] += 1.0
    return vector


class FakeOllama:
    def __init__(self):
        self.requests: list[tuple[str, dict | None]] = []
        self.tags = [{'name': 'llama3.2:1b', 'size': 1_300_000_000},
                     {'name': 'qwen3:4b', 'size': 2_600_000_000},
                     {'name': 'gpt-oss:120b-cloud', 'size': 0},
                     {'name': 'remote:latest', 'size': 1, 'remote_host': 'https://ollama.com'}]
        self.capabilities = {'llama3.2:1b': ['completion'], 'qwen3:4b': ['completion', 'thinking']}
        self.chunks = ['Bon', 'jour', ' !']
        self.extra: dict = {}
        self.redirect = False
        self.chat_error = ''
        self.json_reply: dict | None = None  # réponse aux requêtes avec « format » (classement)
        self.delay = 0.0  # attente avant le premier morceau (lecture de la question)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append((self.path, None))
                if fake.redirect:
                    self.send_response(302)
                    self.send_header('Location', 'http://example.com/')
                    self.end_headers()
                elif self.path == '/api/version':
                    self._send(200, {'version': '9.9.9'})
                elif self.path == '/api/tags':
                    self._send(200, {'models': fake.tags})
                else:
                    self._send(404, {'error': 'not found'})

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length) or b'{}')
                fake.requests.append((self.path, body))
                if self.path == '/api/show':
                    caps = fake.capabilities.get(body.get('model'))
                    if caps is None:
                        return self._send(404, {'error': 'model not found'})
                    return self._send(200, {'capabilities': caps,
                                            'model_info': {'llama.context_length': 131072}})
                if self.path == '/api/embed':
                    return self._send(200, {'embeddings': [fake_vector(t) for t in body.get('input', [])]})
                if self.path == '/api/generate':
                    return self._send(200, {'done': True})
                if self.path == '/api/chat' and fake.chat_error:
                    return self._send(500, {'error': fake.chat_error})
                if self.path == '/api/chat' and body.get('format') and fake.json_reply is not None:
                    self.send_response(200)
                    self.end_headers()
                    for line in ({'message': {'role': 'assistant', 'content': json.dumps(fake.json_reply)}, 'done': False},
                                 {'message': {'role': 'assistant', 'content': ''}, 'done': True}):
                        self.wfile.write(json.dumps(line).encode() + b'\n')
                    return None
                if self.path == '/api/chat':
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-ndjson')
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(fake.delay)
                    try:
                        for text in fake.chunks:
                            message = {'role': 'assistant', 'content': text, **fake.extra}
                            self.wfile.write(json.dumps({'message': message, 'done': False}).encode() + b'\n')
                        self.wfile.write(json.dumps({'message': {'role': 'assistant', 'content': ''},
                                                     'done': True, 'done_reason': 'stop',
                                                     'eval_count': 3, 'eval_duration': 1_500_000_000}).encode() + b'\n')
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # le client a annulé : normal
                    return None
                return self._send(404, {'error': 'not found'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()

    def last(self, path: str) -> dict | None:
        for p, body in reversed(self.requests):
            if p == path:
                return body
        return None
