"""Client HTTP minimal pour un serveur Ollama local, sans dépendance externe.

Garanties :
- seules les adresses http://127.0.0.1, http://localhost et http://[::1] sont acceptées ;
- connexion directe (http.client) : aucun proxy, aucune redirection suivie ;
- tailles de réponse bornées ;
- les modèles « cloud » sont refusés, même s'ils apparaissent dans la liste locale ;
- un appel d'outil n'est accepté que si des outils ont été proposés pour cette requête :
  il est alors renvoyé à l'appelant (qui ne dispose que d'outils en lecture seule),
  jamais exécuté ici.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import re
import socket
import threading
from typing import Any, Callable, Iterator
from urllib import parse

DEFAULT_URL = 'http://127.0.0.1:11434'
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}$')
MAX_JSON_BYTES = 4_000_000
MAX_LINE_BYTES = 1_000_000
MAX_REPLY_CHARS = 60_000
# Silence maximal pendant une génération : sur un PC lent, lire une longue question
# peut prendre plusieurs minutes avant le premier mot.
STREAM_IDLE_TIMEOUT = 600
MEMORY_MESSAGE = ('Mémoire insuffisante pour charger le modèle. Donner plus de mémoire vive à la '
                  'machine (4 Go au moins, 6 Go conseillés en machine virtuelle), fermer des '
                  'applications, ou choisir un modèle plus petit.')
SLOW_MESSAGE = ('Le modèle n\'a rien produit pendant 10 min : cette machine est trop lente pour lui '
                '(choisir un modèle plus petit, ou décocher « Consulter la machine »)')


class OllamaError(Exception):
    """Erreur lisible par l'utilisateur, sans contenu renvoyé par le serveur."""


class UnexpectedToolCall(OllamaError):
    """Le modèle a demandé un outil alors qu'aucun ne lui était proposé."""


class Cancelled(Exception):
    """La génération a été interrompue à la demande de l'utilisateur."""


def local_url(raw: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise OllamaError('Adresse Ollama vide')
    try:
        parts = parse.urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise OllamaError('Adresse Ollama invalide') from exc
    if (parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parts.username or parts.password or port is None
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise OllamaError('Seule une adresse locale http://127.0.0.1:PORT est autorisée')
    host = '[::1]' if parts.hostname == '::1' else parts.hostname
    return f'http://{host}:{port}'


def is_cloud(name: str) -> bool:
    low = name.lower()
    return low.endswith(':cloud') or low.endswith('-cloud')


def model_name(raw: Any) -> str:
    if not isinstance(raw, str) or not MODEL_RE.fullmatch(raw):
        raise OllamaError('Nom de modèle invalide')
    if is_cloud(raw):
        raise OllamaError('Les modèles cloud sont bloqués : NEXUS reste local')
    return raw


@dataclass(frozen=True)
class ModelInfo:
    name: str
    thinking: bool
    context_length: int | None
    tools: bool = False
    chat: bool = True      # faux pour un modèle qui ne dialogue pas (modèle de sens, par exemple)


@dataclass(frozen=True)
class ToolCalls:
    """Demandes d'outils émises par le modèle : [{'function': {'name': …, 'arguments': …}}]."""
    calls: list[dict[str, Any]]


@dataclass(frozen=True)
class Stats:
    tokens: int
    seconds: float
    done_reason: str

    @property
    def tokens_per_second(self) -> float:
        return self.tokens / self.seconds if self.seconds > 0 else 0.0


class OllamaClient:
    name = 'Ollama'          # nom du serveur dans les messages d'erreur
    logs_hint = 'journalctl -u ollama'

    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 300.0):
        self.base_url = local_url(base_url)
        parts = parse.urlsplit(self.base_url)
        self._host, self._port = parts.hostname, parts.port
        if not 1 <= timeout <= 3600:
            raise OllamaError('Délai hors limites (1 à 3600 s)')
        self.timeout = timeout
        # Sockets des requêtes en cours. On garde notre propre référence : http.client
        # remet conn.sock à None quand la réponse se termine par la fermeture de la connexion.
        self._active: dict[http.client.HTTPConnection, socket.socket | None] = {}
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {'Content-Type': 'application/json'}

    def _explain(self, status: int, detail: str) -> OllamaError | None:
        """Erreur lisible pour un statut HTTP donné (détail en minuscules), ou None."""
        if status == 404:
            return OllamaError('Modèle introuvable : vérifier `ollama list`')
        return None

    def _open(self, path: str, payload: dict | None, timeout: float | None, slow: str | None = None):
        """Renvoie (connexion, réponse) ; la connexion est enregistrée pour abort()."""
        slow = slow or f'{self.name} met trop de temps à répondre'
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout or self.timeout)
        try:
            conn.connect()
            with self._lock:
                self._active[conn] = conn.sock
            conn.request('GET' if body is None else 'POST', path, body=body,
                         headers=self._headers())
            resp = conn.getresponse()
        except socket.timeout as exc:
            self._release(conn)
            raise OllamaError(slow) from exc
        except (OSError, http.client.HTTPException) as exc:
            self._release(conn)
            raise OllamaError(f'{self.name} ne répond pas sur {self.base_url}') from exc
        if resp.status != 200:
            try:
                detail = resp.read(4096).decode('utf-8', errors='replace').lower()
            except (OSError, http.client.HTTPException):
                detail = ''
            self._release(conn)
            if 300 <= resp.status < 400:
                raise OllamaError(f'{self.name} a tenté une redirection ; refusée')
            # Le texte du serveur n'est jamais recopié : on le reconnaît, puis on explique.
            explained = self._explain(resp.status, detail)
            if explained is not None:
                raise explained
            if 'memory' in detail or 'oom' in detail or 'alloc' in detail:
                raise OllamaError(MEMORY_MESSAGE)
            raise OllamaError(f'{self.name} a répondu HTTP {resp.status} (erreur interne ; '
                              f'détails : {self.logs_hint})')
        return conn, resp

    def _release(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._active.pop(conn, None)
        conn.close()

    def abort(self) -> None:
        """Coupe immédiatement les requêtes en cours (appelable depuis un autre fil)."""
        with self._lock:
            sockets = [sock for sock in self._active.values() if sock is not None]
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _json(self, path: str, payload: dict | None = None,
              timeout: float | None = None) -> dict[str, Any]:
        conn, resp = self._open(path, payload, timeout)
        try:
            raw = resp.read(MAX_JSON_BYTES + 1)
        except socket.timeout as exc:
            raise OllamaError(f'{self.name} met trop de temps à répondre') from exc
        except (OSError, http.client.HTTPException) as exc:
            raise OllamaError(f'Connexion à {self.name} interrompue') from exc
        finally:
            self._release(conn)
        if len(raw) > MAX_JSON_BYTES:
            raise OllamaError(f'Réponse {self.name} trop volumineuse')
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OllamaError(f'Réponse {self.name} illisible') from exc
        if not isinstance(data, dict):
            raise OllamaError(f'Réponse {self.name} inattendue')
        return data

    # -- API ---------------------------------------------------------------
    def version(self, timeout: float = 5) -> str:
        return str(self._json('/api/version', timeout=timeout).get('version', '?'))

    def models(self, timeout: float = 10) -> list[dict[str, Any]]:
        """Modèles installés localement : [{'name': str, 'size': int}], sans les modèles cloud."""
        listed = self._json('/api/tags', timeout=timeout).get('models')
        if not isinstance(listed, list):
            raise OllamaError('Liste de modèles inattendue')
        result = []
        for entry in listed:
            if not isinstance(entry, dict):
                continue
            name = entry.get('name')
            if (not isinstance(name, str) or not MODEL_RE.fullmatch(name) or is_cloud(name)
                    or entry.get('remote_host') or entry.get('remote_model')):
                continue
            size = entry.get('size')
            result.append({'name': name, 'size': size if isinstance(size, int) and size >= 0 else 0})
        return sorted(result, key=lambda m: m['name'])

    def show(self, model: str) -> ModelInfo:
        data = self._json('/api/show', {'model': model_name(model)}, timeout=30)
        caps = data.get('capabilities')
        caps = caps if isinstance(caps, list) else []
        if 'tools' in caps and 'completion' not in caps:
            raise OllamaError('Modèle inutilisable pour le dialogue')
        context = None
        info = data.get('model_info')
        if isinstance(info, dict):
            for key, value in info.items():
                if key.endswith('.context_length') and isinstance(value, int) and value > 0:
                    context = value
                    break
        return ModelInfo(name=model, thinking='thinking' in caps, context_length=context,
                         tools='tools' in caps, chat='completion' in caps or not caps)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Vecteurs de sens (embeddings) pour une liste de textes."""
        if not texts:
            return []
        data = self._json('/api/embed', {'model': model_name(model), 'input': texts, 'truncate': True},
                          timeout=self.timeout)
        vectors = data.get('embeddings')
        if (not isinstance(vectors, list) or len(vectors) != len(texts)
                or not all(isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v)
                           for v in vectors)):
            raise OllamaError('Réponse d\'embedding inattendue')
        return vectors

    def load(self, model: str, keep_alive: str = '30m') -> None:
        """Charge le modèle en mémoire à l'avance (requête vide)."""
        self._json('/api/generate', {'model': model_name(model), 'keep_alive': keep_alive},
                   timeout=self.timeout)

    def unload(self, model: str) -> None:
        """Libère la mémoire prise par le modèle (sans attendre le délai habituel)."""
        try:
            self._json('/api/generate', {'model': model_name(model), 'keep_alive': 0}, timeout=30)
        except OllamaError:
            pass

    def chat_stream(self, info: ModelInfo, messages: list[dict[str, str]], *,
                    num_ctx: int, num_predict: int = 1024, temperature: float = 0.4,
                    keep_alive: str = '30m', tools: list[dict[str, Any]] | None = None,
                    format: dict[str, Any] | None = None,
                    cancelled: Callable[[], bool] = lambda: False) -> Iterator[str | ToolCalls | Stats]:
        """Produit des morceaux de texte, éventuellement des ToolCalls, puis un Stats final."""
        body: dict[str, Any] = {
            'model': model_name(info.name),
            'messages': messages,
            'stream': True,
            'keep_alive': keep_alive,
            'options': {'num_ctx': num_ctx, 'num_predict': num_predict,
                        'temperature': temperature},
        }
        if info.thinking:
            body['think'] = False  # seulement pour les modèles qui le gèrent
        if tools:
            body['tools'] = tools
        if format:
            body['format'] = format  # sortie JSON contrainte par un schéma
        total = 0
        if cancelled():
            raise Cancelled()
        try:
            conn, resp = self._open('/api/chat', body, STREAM_IDLE_TIMEOUT, slow=SLOW_MESSAGE)
        except OllamaError:
            if cancelled():
                raise Cancelled() from None
            raise
        try:
            while True:
                if cancelled():
                    raise Cancelled()
                try:
                    line = resp.readline(MAX_LINE_BYTES + 1)
                except socket.timeout as exc:
                    raise OllamaError(SLOW_MESSAGE) from exc
                except (OSError, http.client.HTTPException) as exc:
                    if cancelled():
                        raise Cancelled() from exc
                    raise OllamaError('Connexion à Ollama interrompue') from exc
                if not line:
                    if cancelled():
                        raise Cancelled()
                    raise OllamaError('Flux Ollama interrompu')
                if len(line) > MAX_LINE_BYTES:
                    raise OllamaError('Ligne de flux trop longue')
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode('utf-8'))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise OllamaError('Flux Ollama illisible') from exc
                if not isinstance(chunk, dict):
                    raise OllamaError('Flux Ollama inattendu')
                if 'error' in chunk:
                    raise OllamaError('Ollama a signalé une erreur pendant la génération')
                message = chunk.get('message') or {}
                if not isinstance(message, dict):
                    raise OllamaError('Flux Ollama inattendu')
                calls = message.get('tool_calls')
                if calls:
                    if not tools or not isinstance(calls, list) or len(calls) > 8:
                        raise UnexpectedToolCall("Le modèle a demandé un outil non proposé : refusé")
                    yield ToolCalls([c for c in calls if isinstance(c, dict)])
                text = message.get('content') or ''
                if isinstance(text, str) and text:
                    total += len(text)
                    if total > MAX_REPLY_CHARS:
                        raise OllamaError('Réponse trop longue, interrompue')
                    yield text
                if chunk.get('done'):
                    count = chunk.get('eval_count')
                    duration = chunk.get('eval_duration')
                    yield Stats(tokens=count if isinstance(count, int) else 0,
                                seconds=duration / 1e9 if isinstance(duration, (int, float)) else 0.0,
                                done_reason=str(chunk.get('done_reason') or 'stop'))
                    return
        finally:
            self._release(conn)


def pick_model(installed: list[str], preferred: str | None) -> str | None:
    """Le modèle préféré s'il est installé, sinon le premier disponible."""
    if preferred:
        wanted = preferred if ':' in preferred else preferred + ':latest'
        for name in installed:
            if name == wanted or name == preferred:
                return name
    return installed[0] if installed else None
