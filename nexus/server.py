"""NEXUS en serveur maison : la même IA, joignable depuis le téléphone.

`nexus serveur` fait tourner NEXUS sans écran et sert une petite appli web. Le téléphone
ne contient rien : questions, documents et mémoire restent sur le PC de la maison.

Ce qui est tenu ici :
- seuls les appareils appairés (nexus/access.py) accèdent à l'API ; l'appli elle-même
  (page, script, style) est publique, elle ne contient aucune donnée ;
- sur le réseau, tout passe en HTTPS avec un certificat créé sur ce PC ;
- les requêtes qui changent quelque chose exigent un en-tête que seul le script de
  l'appli peut poser (un autre site ouvert dans le navigateur ne peut rien déclencher) ;
- un document n'est jamais servi par son chemin, mais par son numéro dans la
  bibliothèque ; un dépôt n'écrase jamais un fichier existant ;
- une réponse commencée continue si le téléphone se déconnecte : elle est enregistrée
  dans la conversation et se retrouve au retour.
"""
from __future__ import annotations

import codecs
from datetime import date, datetime, timedelta
import hashlib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
from queue import Empty, Queue
import secrets
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib import parse

from . import __version__
from .access import Access
from .alerts import KEEPALIVE, POLL, ws_accept, ws_frame, ws_parse
from .agenda import REPEATS, french_date, french_time, quick_add
from .autonomy import GESTURES, LEVELS
from .journal import publisher
from .voice import MAX_AUDIO
from .assistant import Assistant, ToolUse, Turn, machine_context
from .config import Settings
from .library import CATEGORIES, IMAGE_SUFFIXES, INBOX, SUFFIXES, money, safe_name
from .memory import Store
from .models import AUTO
from .ollama import OllamaError
from .planner import Runner
from .telemetry import Sample, Telemetry, describe
from .tools import Env, Toolbox
from .watch import Watcher, load_rules_file

WEB = Path(__file__).resolve().parent / 'web'
STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icone.svg': ('icone.svg', 'image/svg+xml'),
}
COOKIE = 'nexus_appareil'
TOUR = object()                 # dans la file des documents : « fais une tournée du dossier d'arrivée »
TOUR_SECONDS = 300
COOKIE_SECONDS = 400 * 24 * 3600
DEFAULT_PORT = 8765
MAX_JSON = 64 * 1024
MAX_LISTENERS = 6               # abonnés aux alertes en même temps ; au-delà, le plus ancien est coupé
MAX_UPLOAD = 50 * 2**20
MAX_TEXT_UPLOAD = 2 * 2**20
MAX_IMAGE_UPLOAD = 25 * 2**20
MAGIC = {'.pdf': (b'%PDF-',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
         '.png': (b'\x89PNG\r\n\x1a\n',)}
MIME = {'.pdf': 'application/pdf', '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}
SECURITY_HEADERS = {
    'Content-Security-Policy': "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
    # Le micro n'est permis qu'à l'appli elle-même ; caméra et position ne le sont à personne.
    'Permissions-Policy': 'microphone=(self), camera=(), geolocation=()',
}


class Refused(Exception):
    """Requête refusée : (statut HTTP, message pour l'utilisateur)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# -- le cœur : une seule IA pour tous les appareils -----------------------------------------
class Home:
    def __init__(self, settings: Settings, *, client: Any = None, telemetry: Telemetry | None = None):
        self.settings = settings
        self.client = client if client is not None else settings.open_client()
        self.store = Store(settings.db_path)
        self.telemetry = telemetry or Telemetry()
        first = self.telemetry.sample()
        # NEXUS choisit son modèle : un pour dialoguer, un (le meilleur) pour le travail de fond.
        self.chooser = settings.open_chooser(self.client, first.mem_total_mib, first.cores)
        self._roles: tuple[float, dict] | None = None
        self.library = settings.open_library(self.client, lambda: self.chooser.pick('fond').name)
        self.agenda = settings.open_agenda()
        self.mailbox = settings.open_mailbox(self.client, self.chooser)
        toolbox = Toolbox(Env(library=self.library, agenda=self.agenda, mailbox=self.mailbox))
        self.assistant = Assistant(self.client, self.store, num_ctx=settings.num_ctx, toolbox=toolbox,
                                   souvenirs=settings.open_souvenirs(self.client),
                                   skills=settings.open_skills(toolbox), chooser=self.chooser, agenda=self.agenda)
        self.watcher = Watcher(load_rules_file(settings.rules_path))
        self.sample: Sample | None = None
        self.planner = settings.open_planner()
        self.autonomy = settings.open_autonomy(self.library, self.agenda, self.planner)
        self.relay = settings.open_relay(self.planner)      # alertes pour téléphone verrouillé
        self.backup = settings.open_backup()                # sauvegarde chiffrée de la nuit
        # NEXUS se surveille lui-même : moteur, tâches de nuit, courrier, sauvegarde, place, certificat.
        self.updater = settings.open_updater()              # versions de NEXUS : annonce, et passage de témoin
        self.replaced = threading.Event()                   # une autre version a pris la place : ce serveur doit s'arrêter
        self.health = settings.open_health(engine=settings.engine_probe(self.client, self.chooser), planner=self.planner,
                                           mailbox=self.mailbox, backup=self.backup, updater=self.updater)
        self.listeners = 0                                  # abonnés aux alertes en ce moment
        self.voice = settings.open_voice()
        self.press = settings.open_press()
        self.publish = publisher(settings, client=self.client, chooser=self.chooser, library=self.library,
                                 agenda=self.agenda, autonomy=self.autonomy, souvenirs=self.assistant.souvenirs,
                                 mailbox=self.mailbox, health=self.health)
        self.runner = (Runner(self.planner, toolbox=toolbox, skills=self.assistant.skills, autonomy=self.autonomy,
                              souvenirs=self.assistant.souvenirs, journal=self.publish, mailbox=self.mailbox,
                              backup=self.backup, health=self.health, updater=self.updater)
                       if self.planner is not None else None)
        self.busy = threading.Lock()           # une question à la fois : il n'y a qu'un moteur
        self.cancel = threading.Event()
        self.library_lock = threading.Lock()
        self.status = ''                       # « Démarrage du moteur… »
        self.answering: int | None = None      # conversation en cours de réponse
        self.indexing = 0                      # documents reçus pas encore lus
        self._sink: Callable[[dict], None] | None = None
        self._inbox: Queue = Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        if getattr(self.client, 'engine', None) is not None:
            self.client.on_progress = self._progress

    # -- vie du service -------------------------------------------------------------------
    def start(self, warm_up: bool = True) -> None:
        jobs = [('nexus-maison-veille', self._watch), ('nexus-maison-documents', self._index_worker)]
        if warm_up:
            jobs.append(('nexus-maison-preparation', self._prepare))
        for name, target in jobs:
            thread = threading.Thread(name=name, target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        self._stop.set()
        self.cancel.set()
        self._inbox.put(None)
        self.client.abort()
        for thread in self._threads:
            thread.join(10)
        engine = getattr(self.client, 'engine', None)
        if engine is not None:
            engine.release()
        for resource in (self.store, self.library, self.assistant.souvenirs, self.planner, self.agenda, self.autonomy,
                         self.mailbox):
            try:
                if resource is not None:
                    resource.close()
            except Exception:
                pass

    def _progress(self, text: str) -> None:
        self.status = text
        sink = self._sink
        if text and sink is not None:
            sink({'t': 'etat', 'v': text})

    def _watch(self) -> None:
        """Mesures de la machine toutes les 5 s, tâches planifiées toutes les minutes."""
        last_plan, last_tour = 0.0, time.monotonic()
        while not self._stop.is_set():
            try:
                if self.updater.outdated():        # une mise à jour (ou un retour arrière) a changé la version en place
                    self.replaced.set()
                sample = self.telemetry.sample()
                for change in self.watcher.update(sample):
                    self._machine_alert(change)
                self.sample = sample
                if self.runner is not None and time.monotonic() - last_plan >= 60:
                    last_plan = time.monotonic()
                    self.runner.tick()
                if self.autonomy is not None and time.monotonic() - last_tour >= TOUR_SECONDS:
                    last_tour = time.monotonic()
                    self._inbox.put(TOUR)      # un fichier posé à la main dans le dossier d'arrivée est vu aussi
            except Exception:     # la veille ne doit jamais s'arrêter
                pass
            self._stop.wait(5)

    def _machine_alert(self, change) -> None:
        """Une alerte sérieuse sur l'état du PC devient une notification : une fois par jour et par règle."""
        rule = change.alert.rule
        if change.kind != 'raised' or rule.level == 'info' or self.planner is None:
            return
        self.planner.notify(rule.level, 'PC de la maison : ' + change.alert.text, rule.advice, 'machine',
                            once_key=f'machine:{rule.id}:{date.today().isoformat()}')

    def _prepare(self) -> None:
        """Démarre le moteur et lui fait lire ses consignes, pour que la 1re question n'attende pas."""
        try:
            name = self.chooser.pick('dialogue').name
            self.client.load(name)
            self.assistant.warm_up(name, use_tools=self.settings.tools)
        except OllamaError:
            pass
        self.status = ''

    def roles(self) -> dict:
        """Les modèles choisis (dialogue, fond), revus au plus une fois par minute."""
        if self._roles is None or time.monotonic() - self._roles[0] > 60:
            try:
                self._roles = (time.monotonic(), self.chooser.roles())
            except OllamaError:
                return {}
        return self._roles[1]

    # -- état ---------------------------------------------------------------------------
    def state(self, device: dict) -> dict:
        sample = self.sample
        with self.library_lock:
            stats = self.library.stats() if self.library is not None else None
        roles = self.roles()
        talk, deep = roles.get('dialogue'), roles.get('fond')
        return {
            'version': __version__, 'appareil': device['nom'],
            'moteur': self.client.name, 'modele': talk.name if talk else None,
            'choix': talk.reason if talk else '',
            # Un second modèle, meilleur mais plus lent : proposé pour une « réponse approfondie ».
            'modele_approfondi': deep.name if deep and talk and deep.name != talk.name else None,
            'etat': self.status, 'voix': self.voice.installed(),
            'occupe': self.busy.locked(), 'conversation_en_cours': self.answering,
            'machine': describe(sample) if sample is not None else [],
            'alertes': [alert.text for alert in self.watcher.active()],
            'notifications': self.planner.unread() if self.planner is not None else 0,
            'a_ranger': self._to_tidy(),
            'documents': ({'nombre': stats['documents'], 'sans_texte': stats['sans_texte'],
                           'reconnaissance': stats['reconnaissance'],
                           'en_lecture': self.indexing} if stats is not None else None),
        }

    def _to_tidy(self) -> int:
        if self.library is None:
            return 0
        with self.library_lock:
            return self.library.db.execute(
                "SELECT COUNT(*) FROM documents WHERE substr(path, 1, ?) = ? AND (proposal IS NULL OR proposal != path)",
                (len(INBOX) + 1, INBOX + '/')).fetchone()[0]

    # -- dialogue -----------------------------------------------------------------------
    def ask(self, question: str, conversation: int | None, use_tools: bool, deep: bool = False) -> Queue:
        """Lance la réponse en tâche de fond ; renvoie la file de ses événements (None = fin)."""
        question = question.strip()
        if not question:
            raise Refused(400, 'Question vide')
        if conversation is not None and conversation not in {c['id'] for c in self.store.conversations()}:
            raise Refused(404, 'Conversation introuvable')
        if not self.busy.acquire(blocking=False):
            raise Refused(409, 'NEXUS répond déjà à une autre question : réessayer dans un instant')
        events: Queue = Queue()
        self.cancel.clear()
        self._sink = events.put

        def work() -> None:
            turn = Turn()
            try:
                conv = conversation if conversation is not None else self.store.new_conversation()
                self.answering = conv
                events.put({'t': 'debut', 'conversation': conv})
                context = machine_context(self.sample, self.watcher.active())
                # « auto » : le modèle n'est choisi que si la question en demande un (pas pour une
                # note dans l'agenda ou un souvenir, que le code traite seul).
                for item in self.assistant.reply(conv, AUTO, question, context, cancelled=self.cancel.is_set,
                                                 use_tools=use_tools and self.settings.tools, turn=turn, deep=deep):
                    events.put({'t': 'outil', 'v': item.label} if isinstance(item, ToolUse)
                               else {'t': 'texte', 'v': item})
                events.put({'t': 'fin', 'interrompu': turn.cancelled, 'modele': turn.model or None,
                            'jetons': turn.stats.tokens if turn.stats else 0,
                            'vitesse': round(turn.stats.tokens_per_second, 1) if turn.stats else 0,
                            'sources': turn.sources, 'souvenirs': turn.recalled,
                            'propositions': [{'id': i, 'texte': t} for i, t in turn.proposals]})
            except OllamaError as exc:
                events.put({'t': 'erreur', 'v': str(exc)})
            except Exception as exc:
                events.put({'t': 'erreur', 'v': f'Erreur interne ({type(exc).__name__})'})
            finally:
                self._sink, self.answering, self.status = None, None, ''
                self.busy.release()
                events.put(None)

        threading.Thread(name='nexus-maison-reponse', target=work, daemon=True).start()
        return events

    def stop_answer(self) -> bool:
        if not self.busy.locked():
            return False
        self.cancel.set()
        self.client.abort()
        return True

    # -- documents ----------------------------------------------------------------------
    def _library(self):
        if self.library is None:
            raise Refused(404, f'Pas de dossier de documents sur le PC ({self.settings.library_root})')
        return self.library

    @staticmethod
    def _card(doc: dict) -> dict:
        return {'id': doc['id'], 'titre': doc['title'] or Path(doc['path']).stem, 'chemin': doc['path'],
                'categorie': CATEGORIES.get(doc['category'] or '', ''), 'date': doc['doc_date'],
                'emetteur': doc['issuer'], 'lisible': doc['status'] != 'sans_texte',
                'reconnu': doc.get('ocr') == 1,
                'montant': money(doc['amount_cents']) if doc['amount_cents'] is not None else None}

    def documents(self, query: str) -> dict:
        library = self._library()
        with self.library_lock:
            if not query.strip():
                return {'documents': [self._card(d) for d in library.documents()[:500]]}
            passages = []
            for hit in library.search(query[:300], k=8):
                row = library.db.execute('SELECT id FROM documents WHERE path=?', (hit['path'],)).fetchone()
                passages.append({'id': row[0] if row else None, 'titre': hit['title'] or Path(hit['path']).stem,
                                 'chemin': hit['path'], 'page': hit['page'], 'date': hit['date'],
                                 'extrait': hit['text'][:400]})
            return {'passages': passages, 'par_mots': library.degraded}

    # -- rangement des documents reçus ---------------------------------------------------
    def _received(self, doc: dict) -> bool:
        return doc['path'].startswith(INBOX + '/')

    def tidy_state(self) -> dict:
        """Ce que NEXUS propose pour les documents reçus du téléphone. Rien n'est déplacé ici."""
        library = self._library()
        with self.library_lock:
            documents = library.documents()
            batch = library.last_batch()
        received = [d for d in documents if self._received(d) and d['proposal'] != d['path']]
        elsewhere = sum(1 for d in documents if not self._received(d) and d['proposal'] and d['proposal'] != d['path'])
        return {
            'propositions': [{**self._card(d), 'propose': d['proposal'], 'cle_categorie': d['category'] or ''}
                             for d in received],
            'ailleurs': elsewhere, 'dernier': batch, 'categories': CATEGORIES, 'en_lecture': self.indexing,
        }

    def tidy(self, doc_ids: list[int]) -> dict:
        """Range les documents reçus désignés, à l'endroit proposé. Annulable, jamais d'écrasement."""
        library = self._library()
        with self.library_lock:
            allowed = [d['id'] for d in library.pending() if d['id'] in doc_ids and self._received(d)]
            moved = library.apply(allowed) if allowed else []
        return {'ranges': [{'de': source, 'vers': target} for source, target in moved]}

    def tidy_undo(self) -> dict:
        library = self._library()
        with self.library_lock:
            undone = library.undo()
            failed = list(library.not_undone)
        return {'annules': [{'de': current, 'vers': original} for current, original in undone],
                'non_annules': failed}

    def revise(self, doc_id: int, data: dict) -> dict:
        library = self._library()
        fields = {}
        for key, name in (('categorie', 'category'), ('titre', 'title'), ('emetteur', 'issuer'), ('date', 'doc_date')):
            if key in data:
                if not isinstance(data[key], str) or len(data[key]) > 200:
                    raise Refused(400, 'Valeur refusée')
                fields[name] = data[key]
        try:
            with self.library_lock:
                doc = library.revise(doc_id, **fields)
        except ValueError as exc:
            raise Refused(400, f'Correction refusée : {exc}') from None
        if doc is None:
            raise Refused(404, 'Document introuvable')
        return {**self._card(doc), 'propose': doc['proposal'], 'cle_categorie': doc['category'] or ''}

    def leave(self, doc_id: int) -> dict:
        library = self._library()
        with self.library_lock:
            return {'ok': library.dismiss(doc_id)}

    def document_file(self, doc_id: int) -> Path:
        library = self._library()
        with self.library_lock:
            doc = library.document(doc_id)
        if doc is None:
            raise Refused(404, 'Document introuvable')
        path = (library.root / doc['path']).resolve()
        if library.root not in path.parents or not path.is_file() or path.suffix.lower() not in MIME:
            raise Refused(404, 'Document introuvable')     # sorti du dossier, ou disparu
        return path

    def receive(self, name: str, source, length: int) -> dict:
        """Enregistre un document envoyé par le téléphone, sans jamais rien écraser."""
        library = self._library()
        stem, suffix = os.path.splitext(os.path.basename(name.replace('\\', '/')))
        suffix = suffix.lower()
        if suffix not in SUFFIXES:
            raise Refused(415, 'NEXUS lit les PDF, les photos (.jpg, .png) et les textes (.txt, .md)')
        binary = suffix in MAGIC
        limit = MAX_UPLOAD if suffix == '.pdf' else MAX_IMAGE_UPLOAD if suffix in IMAGE_SUFFIXES else MAX_TEXT_UPLOAD
        if not 0 < length <= limit:
            raise Refused(413, f'Fichier vide ou trop gros (maximum {limit // 2**20} Mo)')
        folder = library.root / INBOX
        folder.mkdir(exist_ok=True)
        partial = folder / f'.{secrets.token_hex(8)}.partiel'    # nom caché : ignoré par l'indexation
        try:
            remaining, head, text_ok = length, b'', True
            decoder = None if binary else codecs.getincrementaldecoder('utf-8')()
            with partial.open('xb') as out:
                while remaining:
                    block = source.read(min(65536, remaining))
                    if not block:
                        raise Refused(400, 'Envoi interrompu')
                    remaining -= len(block)
                    if len(head) < 1024:
                        head += block[:1024 - len(head)]
                    if decoder is not None:
                        try:
                            text_ok = text_ok and '\x00' not in decoder.decode(block)
                        except UnicodeDecodeError:
                            text_ok = False
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if suffix == '.pdf' and b'%PDF-' not in head:
                raise Refused(415, 'Ce fichier n\'est pas un vrai PDF')
            if suffix in IMAGE_SUFFIXES and not head.startswith(MAGIC[suffix]):
                raise Refused(415, 'Ce fichier n\'est pas une vraie image')
            if not binary and not text_ok:
                raise Refused(415, 'Ce fichier n\'est pas un texte lisible')
            base = f'{date.today().isoformat()} {safe_name(stem, 80) or "document"}'
            for attempt in range(1, 200):
                target = folder / (base + ('' if attempt == 1 else f' ({attempt})') + suffix)
                try:
                    os.link(partial, target)      # échoue si le nom est pris : rien n'est écrasé
                    break
                except FileExistsError:
                    continue
            else:
                raise Refused(409, 'Trop de fichiers du même nom aujourd\'hui')
        finally:
            partial.unlink(missing_ok=True)
        relative = target.relative_to(library.root).as_posix()
        self.indexing += 1
        self._inbox.put(relative)
        return {'chemin': relative}

    def _index_worker(self) -> None:
        """Lit les documents reçus (texte, sens), puis les classe : en tâche de fond."""
        while True:
            relative = self._inbox.get()
            if relative is None or self._stop.is_set():
                return
            try:
                if relative is TOUR:
                    if self.autonomy is not None:
                        self.autonomy.tour()
                    continue
                # Hors verrou : lire un scan (reconnaissance de texte) peut prendre du temps, et la
                # recherche doit rester disponible pendant ce temps.
                doc = self.library.index_one(self.library.root / relative)
                if doc is not None and doc['status'] == 'indexé':
                    self.library.classify(doc['id'])   # par le modèle « de fond », choisi à ce moment
                    if self.autonomy is not None:      # puis NEXUS s'en occupe seul, selon les niveaux réglés
                        self.autonomy.handle_document(doc['id'])
            except Exception:     # un document illisible ne doit pas arrêter la file
                pass
            finally:
                if relative is not TOUR:
                    self.indexing = max(0, self.indexing - 1)

    # -- courrier (lecture seule) ---------------------------------------------------------
    def mail_state(self) -> dict:
        if self.mailbox is None:
            raise Refused(404, 'Courrier non configuré')
        return {'boites': [a['nom'] for a in self.mailbox.accounts()], 'importants': self.mailbox.important(hours=72)[:20]}

    # -- journal du matin -----------------------------------------------------------------
    def journal_state(self) -> dict:
        return {'numeros': self.press.editions()[:14], 'reglages': self.press.settings()}

    def journal_compose(self) -> dict:
        result = self.publish(datetime.now())
        return {'jour': result['jour'], 'pdf': result['pdf'] is not None, 'imprime': result['imprime'],
                'notes': result['notes'] + result['edition'].notes}

    def journal_set(self, data: dict) -> dict:
        changes = {key: data[key] for key in ('imprimer', 'actualites') if isinstance(data.get(key), bool)}
        try:
            return {'reglages': self.press.set(**changes)}
        except ValueError as exc:
            raise Refused(400, f'Réglage refusé : {exc}') from None

    def journal_file(self, day: str, suffix: str) -> Path:
        page = self.press.path(day, suffix)
        if page is None:
            raise Refused(404, 'Numéro introuvable')
        return page

    # -- autonomie ------------------------------------------------------------------------
    def activity(self) -> dict:
        if self.autonomy is None:
            raise Refused(404, 'Autonomie non configurée')
        with self.library_lock:
            self.autonomy.prune()
        acts = [{'id': a['id'], 'quand': a['quand'], 'geste': a['geste'], 'resume': a['resume'], 'etat': a['etat'],
                 'source': a['source']} for a in self.autonomy.acts(40)]
        return {'actes': acts, 'niveaux': self.autonomy.levels(), 'gestes': GESTURES, 'choix': LEVELS}

    def activity_change(self, act_id: int, action: str) -> dict:
        if self.autonomy is None:
            raise Refused(404, 'Autonomie non configurée')
        with self.library_lock:
            if action == 'annuler':
                return {'message': self.autonomy.undo(act_id)}
            if action == 'valider':
                return {'message': self.autonomy.confirm(act_id)}
            if action == 'refuser':
                return {'message': 'Proposition écartée.' if self.autonomy.dismiss(act_id) else 'Rien à écarter.'}
        raise Refused(404, 'Adresse inconnue')

    def set_autonomy(self, gesture: Any, level: Any) -> dict:
        if self.autonomy is None:
            raise Refused(404, 'Autonomie non configurée')
        try:
            self.autonomy.set_level(str(gesture), str(level))
        except ValueError as exc:
            raise Refused(400, f'Réglage refusé : {exc}') from None
        return {'niveaux': self.autonomy.levels()}

    # -- agenda -------------------------------------------------------------------------
    def _agenda(self):
        if self.agenda is None:
            raise Refused(404, 'Agenda non configuré')
        return self.agenda

    def agenda_state(self, days: int = 30) -> dict:
        agenda, now = self._agenda(), datetime.now()
        end = datetime.combine(now.date() + timedelta(days=max(1, min(days, 366))), datetime.min.time())
        events = []
        for event in agenda.events(now - timedelta(hours=1), end):
            moment = datetime.fromisoformat(event['quand'])
            events.append({'id': event['id'], 'titre': event['titre'], 'quand': event['quand'],
                           'jour': french_date(moment.date(), now.date()),
                           'heure': '' if event['journee'] else french_time(moment),
                           'lieu': event['lieu'], 'notes': event['notes'],
                           'repetition': REPEATS[event['repetition']] if event['repetition'] else ''})
        todos = []
        for todo in agenda.todos(include_done=True):
            due = date.fromisoformat(todo['echeance']) if todo['echeance'] else None
            todos.append({'id': todo['id'], 'texte': todo['texte'], 'faite': bool(todo['faite']),
                          'pour': french_date(due, now.date()) if due else '',
                          'en_retard': bool(due and due < now.date() and not todo['faite'])})
        done = [t for t in todos if t['faite']][-10:]
        return {'evenements': events, 'taches': [t for t in todos if not t['faite']], 'faites': done}

    def agenda_phrase(self, text: str) -> dict:
        """Ajout rapide : « Dentiste demain à 14h », « acheter du pain »… compris par des règles."""
        self._agenda()
        if not text.strip():
            raise Refused(400, 'Rien à ajouter')
        return {'message': quick_add(self.agenda, text, datetime.now()).replace('**', '')}

    def agenda_change(self, kind: str, item_id: int, action: str, data: dict | None = None) -> dict:
        agenda = self._agenda()
        if kind == 'evenements' and action == 'supprimer':
            return {'ok': agenda.delete_event(item_id)}
        if kind == 'taches' and action == 'supprimer':
            return {'ok': agenda.delete_todo(item_id)}
        if kind == 'taches' and action == 'cocher':
            return {'ok': agenda.set_done(item_id, (data or {}).get('faite') is not False)}
        raise Refused(404, 'Adresse inconnue')

    # -- santé ----------------------------------------------------------------------------
    def health_report(self) -> dict:
        findings = self.health.check()
        return {'constats': [{'niveau': f.level, 'titre': f.title, 'conseil': f.advice} for f in findings],
                'bien': self.health.good()}

    # -- alertes pour téléphone verrouillé ----------------------------------------------
    def alerts(self, host: str, secure: bool) -> dict:
        relay = self.relay
        if relay is None:
            return {'disponible': False}
        settings = relay.settings()
        return {'disponible': True, 'active': settings['sujet'] is not None, 'discret': settings['discret'],
                'serveur': ('https://' if secure else 'http://') + host, 'sujet': settings['sujet'],
                'lien': relay.link(host, secure), 'relies': self.listeners}

    def alerts_change(self, data: dict) -> None:
        relay = self.relay
        if relay is None:
            raise Refused(409, 'Les alertes ont besoin du planificateur')
        if data.get('active') is True:
            relay.enable()
        elif data.get('active') is False:
            relay.disable()
        if data.get('renouveler') is True and relay.enabled():
            relay.renew()
        if isinstance(data.get('discret'), bool):
            relay.set_discreet(data['discret'])

    def alerts_test(self) -> None:
        if self.planner is None:
            raise Refused(409, 'Les alertes ont besoin du planificateur')
        self.planner.notify('info', 'Essai d\'alerte', 'Si tu lis ceci téléphone verrouillé, les alertes marchent.')

    # -- notifications, mémoire ---------------------------------------------------------
    def notifications(self) -> list[dict]:
        return self.planner.notifications(limit=40) if self.planner is not None else []

    def decide_memory(self, souvenir_id: int, keep: bool) -> bool:
        souvenirs = self.assistant.souvenirs
        if souvenirs is None:
            return False
        return bool(souvenirs.confirm(souvenir_id) if keep else souvenirs.reject(souvenir_id))


# -- HTTP -----------------------------------------------------------------------------------
class _Body:
    """Le corps de la requête, borné à sa longueur annoncée, et qu'on peut finir de vider."""

    def __init__(self, raw, length: int | None):
        self.raw, self.length, self.remaining = raw, length, max(0, length or 0)

    def read(self, size: int) -> bytes:
        data = self.raw.read(min(size, self.remaining)) if self.remaining else b''
        self.remaining -= len(data)
        return data

    def drain(self, limit: int) -> bool:
        """Lit et jette ce qui reste (pour que le refus arrive lisible) ; faux si c'est trop."""
        if self.remaining > limit:
            return False
        while self.remaining:
            if not self.read(65536):
                return False
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'NEXUS'
    sys_version = ''
    timeout = 30

    def setup(self) -> None:
        super().setup()
        if isinstance(self.request, ssl.SSLSocket):      # poignée de main ici, pas dans la boucle d'accueil
            self.request.do_handshake()

    def log_message(self, format: str, *args) -> None:    # ni adresses complètes, ni contenu des questions
        if self.server.verbose:
            path = parse.urlsplit(self.path).path
            if not (path in STATIC or path.startswith('/api/') or path == '/autorite.crt'):
                path = '/…'      # peut contenir le sujet des alertes, qui est une clé
            sys.stderr.write(f'[serveur] {self.command} {path} {args[1] if len(args) > 1 else ""}\n')

    # -- réponses ---------------------------------------------------------------------
    def _headers(self, status: int, content_type: str, length: int | None, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        if length is not None:
            self.send_header('Content-Length', str(length))
        for name, value in {**SECURITY_HEADERS, 'Cache-Control': 'no-store', **(extra or {})}.items():
            self.send_header(name, value)
        self.end_headers()

    def _json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self._headers(status, 'application/json; charset=utf-8', len(body), extra)
        self.wfile.write(body)

    # -- contrôles --------------------------------------------------------------------
    def _host(self) -> str:
        host = self.headers.get('Host', '')
        name = host.rsplit(':', 1)[0] if not host.endswith(']') else host
        name = name.strip('[]').lower()
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if name not in self.server.names:
                raise Refused(421, 'Nom de serveur inattendu')
        return host

    def _device(self) -> dict:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
        except cookies.CookieError:
            jar = cookies.SimpleCookie()
        device = self.server.access.check(jar[COOKIE].value) if COOKIE in jar else None
        if device is None:
            raise Refused(401, 'Appareil non appairé')
        return device

    def _from_app(self, host: str) -> None:
        """Requête qui change quelque chose : elle doit venir du script de l'appli."""
        if self.headers.get('X-Nexus') != '1':
            raise Refused(403, 'Requête refusée')
        origin = self.headers.get('Origin')
        if origin is not None and parse.urlsplit(origin).netloc.lower() != host.lower():
            raise Refused(403, 'Requête refusée')

    def _body(self) -> dict:
        length = self.body.length
        if length is None:
            raise Refused(411, 'Longueur manquante')
        if not 0 <= length <= MAX_JSON:
            raise Refused(413, 'Requête trop longue')
        try:
            data = json.loads(self.body.read(length).decode('utf-8')) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Refused(400, 'Requête illisible') from None
        if not isinstance(data, dict):
            raise Refused(400, 'Requête illisible')
        return data

    # -- aiguillage -------------------------------------------------------------------
    def do_GET(self) -> None:
        self._serve('GET')

    def do_POST(self) -> None:
        self._serve('POST')

    def do_DELETE(self) -> None:
        self._serve('DELETE')

    def _serve(self, method: str) -> None:
        declared = self.headers.get('Content-Length', '')
        self.body = _Body(self.rfile, int(declared) if declared.isdigit() and len(declared) < 12 else None)
        try:
            host = self._host()
            url = parse.urlsplit(self.path)
            query = parse.parse_qs(url.query, max_num_fields=10)
            if method == 'GET' and url.path in STATIC:
                return self._static(*STATIC[url.path])
            if method == 'GET' and url.path == '/autorite.crt' and self.server.authority is not None:
                body = self.server.authority.read_bytes()      # certificat public : aucune clé dedans
                self._headers(200, 'application/x-x509-ca-cert', len(body),
                              {'Content-Disposition': 'attachment; filename="nexus-maison.crt"'})
                return self.wfile.write(body)
            if method == 'GET' and self._relay(host, url.path, query):
                return
            if not url.path.startswith('/api/'):
                raise Refused(404, 'Page introuvable')
            if method != 'GET':
                self._from_app(host)
            self._api(method, url.path[5:].strip('/').split('/'), query)
            # Un corps envoyé mais non lu (« {} » d'une action sans paramètre) resterait dans la
            # connexion et serait pris pour le début de la requête suivante.
            if not self.body.drain(2 * MAX_UPLOAD):
                self.close_connection = True
        except Refused as refusal:
            try:
                # On finit de lire l'envoi avant de répondre : sinon le téléphone verrait une
                # coupure réseau au lieu de l'explication.
                if not self.body.drain(2 * MAX_UPLOAD):
                    self.close_connection = True
                self._json(refusal.status, {'erreur': refusal.message})
            except (OSError, ssl.SSLError):
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, socket.timeout, ssl.SSLError):
            self.close_connection = True
        except Exception as exc:
            self.close_connection = True
            try:
                self._json(500, {'erreur': f'Erreur interne ({type(exc).__name__})'})
            except OSError:
                pass

    def _static(self, name: str, content_type: str) -> None:
        body = (WEB / name).read_bytes()
        self._headers(200, content_type, len(body), {'Cache-Control': 'no-cache'})
        self.wfile.write(body)

    def _api(self, method: str, parts: list[str], query: dict) -> None:
        home: Home = self.server.home
        if (method, parts) == ('POST', ['appairage']):
            return self._pair()
        device = self._device()
        number = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() and len(parts[1]) < 12 else None
        route = (method, parts[0], len(parts))
        if route == ('GET', 'etat', 1):
            return self._json(200, home.state(device))
        if route == ('GET', 'conversations', 1):
            return self._json(200, {'conversations': home.store.conversations()[:200]})
        if route == ('GET', 'conversations', 2) and number is not None:
            return self._json(200, {'messages': home.store.messages(number)})
        if route == ('DELETE', 'conversations', 2) and number is not None:
            return self._json(200, {'efface': home.store.delete(number)})
        if route == ('POST', 'question', 1):
            return self._question(home)
        if route == ('POST', 'arret', 1):
            return self._json(200, {'arrete': home.stop_answer()})
        if route == ('GET', 'documents', 1):
            return self._json(200, home.documents((query.get('q') or [''])[0]))
        if route == ('GET', 'documents', 3) and number is not None and parts[2] == 'fichier':
            return self._download(home.document_file(number))
        if route == ('POST', 'documents', 1):
            return self._upload(home, (query.get('nom') or [''])[0])
        if route == ('POST', 'documents', 2) and number is not None:
            return self._json(200, home.revise(number, self._body()))
        if route == ('POST', 'documents', 3) and number is not None and parts[2] == 'laisser':
            return self._json(200, home.leave(number))
        if route == ('GET', 'rangement', 1):
            return self._json(200, home.tidy_state())
        if route == ('POST', 'rangement', 1):
            ids = self._body().get('ids')
            if not isinstance(ids, list) or not 0 < len(ids) <= 200 or not all(type(i) is int for i in ids):
                raise Refused(400, 'Préciser les documents à ranger')
            return self._json(200, home.tidy(ids))
        if route == ('POST', 'rangement', 2) and parts[1] == 'annuler':
            return self._json(200, home.tidy_undo())
        if route == ('POST', 'voix', 1):
            return self._listen(home)
        if route == ('GET', 'courrier', 1):
            return self._json(200, home.mail_state())
        if route == ('GET', 'journal', 1):
            return self._json(200, home.journal_state())
        if route == ('POST', 'journal', 1):
            return self._json(200, home.journal_compose())
        if route == ('POST', 'journal', 2) and parts[1] == 'reglages':
            return self._json(200, home.journal_set(self._body()))
        if route == ('GET', 'journal', 2):
            return self._page(home.journal_file(parts[1], '.html'))
        if route == ('GET', 'journal', 3) and parts[2] == 'pdf':
            return self._download(home.journal_file(parts[1], '.pdf'))
        if route == ('GET', 'activite', 1):
            return self._json(200, home.activity())
        if route == ('POST', 'activite', 3) and number is not None:
            return self._json(200, home.activity_change(number, parts[2]))
        if route == ('POST', 'autonomie', 1):
            data = self._body()
            return self._json(200, home.set_autonomy(data.get('geste'), data.get('niveau')))
        if route == ('GET', 'agenda', 1):
            days = (query.get('jours') or ['30'])[0]
            return self._json(200, home.agenda_state(int(days) if days.isdigit() and len(days) < 4 else 30))
        if route == ('POST', 'agenda', 1):
            phrase = self._body().get('phrase')
            if not isinstance(phrase, str):
                raise Refused(400, 'Requête illisible')
            return self._json(200, home.agenda_phrase(phrase))
        if parts[0] == 'agenda' and len(parts) == 3 and parts[2].isdigit() and len(parts[2]) < 12:
            if method == 'DELETE':
                return self._json(200, home.agenda_change(parts[1], int(parts[2]), 'supprimer'))
            if method == 'POST':
                return self._json(200, home.agenda_change(parts[1], int(parts[2]), 'cocher', self._body()))
        if route == ('GET', 'sante', 1):
            return self._json(200, home.health_report())
        if route == ('GET', 'alertes', 1):
            return self._json(200, home.alerts(self.headers.get('Host', ''), self.server.tls))
        if route == ('POST', 'alertes', 1):
            home.alerts_change(self._body())
            return self._json(200, home.alerts(self.headers.get('Host', ''), self.server.tls))
        if route == ('POST', 'alertes', 2) and parts[1] == 'essai':
            home.alerts_test()
            return self._json(200, {'ok': True})
        if route == ('GET', 'notifications', 1):
            return self._json(200, {'notifications': home.notifications()})
        if route == ('POST', 'notifications', 2) and parts[1] == 'lues':
            if home.planner is not None:
                home.planner.mark_read()
            return self._json(200, {'ok': True})
        if route == ('POST', 'souvenirs', 2) and number is not None:
            return self._json(200, {'ok': home.decide_memory(number, self._body().get('garder') is True)})
        raise Refused(404, 'Adresse inconnue')

    # -- actions ----------------------------------------------------------------------
    def _pair(self) -> None:
        data = self._body()
        token = self.server.access.pair(data.get('code'), data.get('nom'))
        if token is None:
            time.sleep(1.0)      # freine les essais à la chaîne
            raise Refused(403, 'Code refusé ou expiré : en demander un nouveau sur le PC (nexus serveur appairer)')
        cookie = f'{COOKIE}={token}; Path=/; Max-Age={COOKIE_SECONDS}; HttpOnly; SameSite=Strict'
        if self.server.tls:
            cookie += '; Secure'
        self._json(200, {'ok': True}, {'Set-Cookie': cookie})

    def _question(self, home: Home) -> None:
        data = self._body()
        conversation = data.get('conversation')
        if not isinstance(data.get('question'), str) or not (conversation is None or isinstance(conversation, int)):
            raise Refused(400, 'Requête illisible')
        events = home.ask(data['question'], conversation, data.get('outils') is not False,
                          data.get('approfondi') is True)
        self._headers(200, 'application/x-ndjson; charset=utf-8', None,
                      {'Transfer-Encoding': 'chunked', 'X-Accel-Buffering': 'no'})
        try:
            while True:
                try:
                    event = events.get(timeout=15)
                except Empty:
                    event = {'t': 'attente'}      # garde la connexion en vie pendant une longue lecture
                if event is None:
                    break
                line = json.dumps(event, ensure_ascii=False).encode('utf-8') + b'\n'
                self.wfile.write(f'{len(line):X}\r\n'.encode() + line + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
        except OSError:
            # Téléphone parti (écran éteint, réseau perdu) : la réponse continue sur le PC et
            # reste enregistrée dans la conversation.
            self.close_connection = True

    # -- relais d'alertes (protocole d'abonnement de ntfy, lecture seule) ----------------
    def _relay(self, host: str, path: str, query: dict) -> bool:
        """Sert /<sujet>/auth, /<sujet>/json et /<sujet>/ws. Faux si ce n'est pas pour le relais :
        sans le bon sujet, la réponse est la même que pour n'importe quelle page inconnue."""
        home: Home = self.server.home
        parts = path.strip('/').split('/')
        relay = home.relay
        if relay is None or len(parts) != 2 or parts[1] not in ('auth', 'json', 'ws') or not relay.matches(parts[0]):
            return False
        if parts[1] == 'auth':
            self._json(200, {'success': True})
            return True

        def asked(name: str, header: str) -> str:
            return (query.get(name) or [self.headers.get(header, '')])[0][:40]

        cursor = relay.cursor(asked('since', 'X-Since'))
        click = ('https://' if self.server.tls else 'http://') + host + '/'
        if parts[1] == 'ws':
            self._relay_socket(home, parts[0], cursor, click)
        elif asked('poll', 'X-Poll').lower() in ('1', 'true', 'yes'):
            body = b''.join(json.dumps(e, ensure_ascii=False).encode('utf-8') + b'\n' for e in relay.events(cursor, click))
            self._headers(200, 'application/x-ndjson; charset=utf-8', len(body))
            self.wfile.write(body)
        else:
            self._relay_stream(home, parts[0], cursor, click)
        return True

    def _relay_loop(self, home: Home, topic: str, cursor, click: str, send, wait) -> None:
        """Envoie les alertes à mesure qu'elles arrivent, et un signe de vie quand il n'y en a pas."""
        relay = home.relay
        stop = self.server.listener_open(self.connection)
        try:
            send(relay.signal('open'))
            last = time.monotonic()
            while not stop.is_set() and relay.matches(topic):      # sujet renouvelé ou relais éteint : on raccroche
                events = relay.events(cursor, click)
                for event in events:
                    send(event)
                if events:
                    last = time.monotonic()
                elif time.monotonic() - last >= KEEPALIVE:
                    send(relay.signal('keepalive'))
                    last = time.monotonic()
                if not wait(stop):
                    break
        finally:
            self.server.listener_close(stop)

    def _relay_stream(self, home: Home, topic: str, cursor, click: str) -> None:
        self.close_connection = True
        self._headers(200, 'application/x-ndjson; charset=utf-8', None,
                      {'Transfer-Encoding': 'chunked', 'X-Accel-Buffering': 'no'})

        def send(event: dict) -> None:
            line = json.dumps(event, ensure_ascii=False).encode('utf-8') + b'\n'
            self.wfile.write(f'{len(line):X}\r\n'.encode() + line + b'\r\n')

        def wait(stop: threading.Event) -> bool:
            """Attend la suite ; faux dès que le téléphone raccroche (il n'a rien d'autre à dire)."""
            sock = self.connection
            if not getattr(sock, 'pending', lambda: 0)() and not select.select([sock], [], [], POLL)[0]:
                return True
            return bool(sock.recv(4096))

        try:
            self._relay_loop(home, topic, cursor, click, send, wait)
            self.wfile.write(b'0\r\n\r\n')
        except OSError:
            pass          # téléphone parti : il se reconnectera et demandera la suite

    def _relay_socket(self, home: Home, topic: str, cursor, click: str) -> None:
        key = self.headers.get('Sec-WebSocket-Key', '')
        if 'websocket' not in self.headers.get('Upgrade', '').lower() or not 16 <= len(key) <= 64:
            raise Refused(400, 'Connexion WebSocket attendue')
        self.close_connection = True
        self.send_response(101, 'Switching Protocols')
        for name, value in (('Upgrade', 'websocket'), ('Connection', 'Upgrade'), ('Sec-WebSocket-Accept', ws_accept(key))):
            self.send_header(name, value)
        self.end_headers()
        sock, buffer, hung_up = self.connection, b'', False

        def send(event: dict) -> None:
            self.wfile.write(ws_frame(0x1, json.dumps(event, ensure_ascii=False).encode('utf-8')))

        def wait(stop: threading.Event) -> bool:
            """Attend la suite en écoutant le téléphone ; faux quand il raccroche."""
            nonlocal buffer, hung_up
            pending = getattr(sock, 'pending', lambda: 0)()
            if not pending and not select.select([sock], [], [], POLL)[0]:
                return True
            data = sock.recv(4096)
            if not data:
                hung_up = True
                return False
            frames, buffer = ws_parse(buffer + data)
            for opcode, payload in frames:
                if opcode == 0x9:                           # ping : on répond pong
                    self.wfile.write(ws_frame(0xA, payload))
                elif opcode == 0x8:                         # le téléphone ferme : on lui répond, et c'est tout
                    self.wfile.write(ws_frame(0x8, payload[:2]))
                    hung_up = True
                    return False
            return True

        try:
            self._relay_loop(home, topic, cursor, click, send, wait)
            if not hung_up:                                 # c'est NEXUS qui raccroche (clé changée, arrêt)
                self.wfile.write(ws_frame(0x8, b'\x03\xe8'))
        except (OSError, ValueError):
            pass

    def _listen(self, home: Home) -> None:
        """Une question dictée : l'enregistrement est transcrit sur le PC, puis effacé."""
        length = self.body.length
        if length is None:
            raise Refused(411, 'Longueur manquante')
        if not 0 < length <= MAX_AUDIO:
            raise Refused(413, 'Enregistrement vide ou trop long (une minute et demie au plus)')
        audio = self.body.read(length)
        try:
            text = home.voice.transcribe(audio)
        except OllamaError as exc:
            raise Refused(422, str(exc)) from None
        self._json(200, {'texte': text})

    def _page(self, path: Path) -> None:
        """Une page du journal : composée par NEXUS, sans script ; le navigateur n'a le droit de rien charger d'autre."""
        body = path.read_bytes()
        self._headers(200, 'text/html; charset=utf-8', len(body),
                      {'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})
        self.wfile.write(body)

    def _download(self, path: Path) -> None:
        size = path.stat().st_size
        name = parse.quote(path.name, safe='')
        self._headers(200, MIME[path.suffix.lower()], size,
                      {'Content-Disposition': f"attachment; filename*=UTF-8''{name}",
                       'Content-Security-Policy': "default-src 'none'; sandbox"})
        with path.open('rb') as source:
            remaining = size
            while remaining:
                block = source.read(min(65536, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)
        if remaining:
            self.close_connection = True

    def _upload(self, home: Home, name: str) -> None:
        if self.body.length is None:
            raise Refused(411, 'Longueur manquante')
        self._json(200, home.receive(name, self.body, self.body.length))


class NexusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], home: Home, access: Access, *,
                 context: ssl.SSLContext | None = None, names: set[str] | None = None, verbose: bool = False):
        super().__init__(address, Handler)
        self.home, self.access, self.context, self.verbose = home, access, context, verbose
        self.authority: Path | None = None       # certificat de l'autorité de la maison, s'il y en a une
        self._listeners: list[tuple[threading.Event, Any]] = []      # abonnés aux alertes, du plus ancien au plus récent
        self._listeners_lock = threading.Lock()
        self.tls = context is not None
        host = socket.gethostname().lower()
        self.names = {'localhost', host, host + '.local', *(names or set())}

    def get_request(self):
        sock, address = super().get_request()
        if self.context is not None:
            sock.settimeout(15)
            sock = self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def listener_open(self, connection) -> threading.Event:
        """Inscrit un abonné aux alertes. S'il y en a trop, le plus ancien est coupé : une
        connexion morte ne doit pas empêcher le téléphone de revenir."""
        stop = threading.Event()
        with self._listeners_lock:
            while len(self._listeners) >= MAX_LISTENERS:
                old_stop, old_connection = self._listeners.pop(0)
                old_stop.set()
                try:
                    old_connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._listeners.append((stop, connection))
            self.home.listeners = len(self._listeners)
        return stop

    def listener_close(self, stop: threading.Event) -> None:
        with self._listeners_lock:
            self._listeners = [entry for entry in self._listeners if entry[0] is not stop]
            self.home.listeners = len(self._listeners)

    def server_close(self) -> None:
        with self._listeners_lock:
            for stop, _connection in self._listeners:
                stop.set()
        super().server_close()

    def handle_error(self, request, client_address) -> None:
        pass      # connexion coupée ou poignée de main ratée : rien à dire, rien à afficher


# -- certificat et adresses -----------------------------------------------------------------
def local_addresses() -> list[str]:
    """Adresses de ce PC sur le réseau de la maison (sans rien envoyer)."""
    found: list[str] = []
    for family, probe in ((socket.AF_INET, '192.0.2.1'), (socket.AF_INET6, '2001:db8::1')):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))
                address = sock.getsockname()[0]
        except OSError:
            continue
        if not ipaddress.ip_address(address).is_loopback and family == socket.AF_INET:
            found.append(address)
    return found


# L'autorité de la maison ne peut se porter garante que d'adresses privées et de noms locaux :
# même si sa clé était volée, elle ne pourrait pas se faire passer pour un site d'Internet.
AUTHORITY_LIMITS = ('critical,permitted;IP:10.0.0.0/255.0.0.0,permitted;IP:172.16.0.0/255.240.0.0,'
                    'permitted;IP:192.168.0.0/255.255.0.0,permitted;IP:127.0.0.0/255.0.0.0,'
                    'permitted;DNS:local,permitted;DNS:home.arpa,permitted;DNS:localhost')
HOME_NETWORKS = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8')]
CERTIFICATE_DAYS = 397
RENEW_AFTER_DAYS = 300


def _openssl(*argv: str) -> None:
    old_mask = os.umask(0o077)       # les clés privées ne sont lisibles que par leur propriétaire
    try:
        done = subprocess.run(['openssl', *argv], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    except FileNotFoundError:
        raise OllamaError('Il manque « openssl » pour chiffrer les échanges : sudo apt install openssl') from None
    finally:
        os.umask(old_mask)
    if done.returncode != 0:
        raise OllamaError('Création du certificat impossible (openssl a échoué)')


def ensure_authority(folder: Path) -> tuple[Path, Path]:
    """Crée (une seule fois) l'autorité de certification de la maison : (clé, certificat)."""
    folder.mkdir(parents=True, exist_ok=True)
    key, cert = folder / 'autorite.cle', folder / 'autorite.pem'
    if not (key.is_file() and cert.is_file()):
        _openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                 '-keyout', str(key), '-out', str(cert), '-days', '3650', '-subj', '/O=NEXUS/CN=NEXUS - autorite de la maison',
                 '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0', '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                 '-addext', f'nameConstraints={AUTHORITY_LIMITS}')
    return key, cert


def ensure_certificate(folder: Path, addresses: list[str]) -> tuple[Path, Path]:
    """Le certificat du serveur, signé par l'autorité de la maison. Refait si une adresse change
    ou s'il approche de son terme. Seules les adresses privées y figurent."""
    ca_key, ca_cert = ensure_authority(folder)
    key, cert, names_file = folder / 'cle.pem', folder / 'certificat.pem', folder / 'noms.txt'
    private = sorted({a for a in addresses if any(ipaddress.ip_address(a) in net for net in HOME_NETWORKS)})
    host = re.sub(r'[^a-z0-9-]', '', socket.gethostname().lower()) or 'nexus'
    names = ','.join([f'DNS:{host}.local', 'DNS:nexus.home.arpa', 'DNS:localhost', 'IP:127.0.0.1']
                     + [f'IP:{address}' for address in private if address != '127.0.0.1'])
    try:
        fresh = time.time() - cert.stat().st_mtime < RENEW_AFTER_DAYS * 86400
        if key.is_file() and fresh and names_file.read_text(encoding='utf-8') == names:
            return key, cert
    except OSError:
        pass
    request_file, extensions = folder / 'demande.csr', folder / 'extensions.cnf'
    extensions.write_text(f'subjectAltName={names}\nbasicConstraints=critical,CA:FALSE\n'
                          'keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n', encoding='ascii')
    try:
        _openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                 '-keyout', str(key), '-out', str(request_file), '-subj', '/O=NEXUS/CN=NEXUS maison')
        _openssl('x509', '-req', '-in', str(request_file), '-CA', str(ca_cert), '-CAkey', str(ca_key), '-CAcreateserial',
                 '-out', str(cert), '-days', str(CERTIFICATE_DAYS), '-extfile', str(extensions))
    finally:
        request_file.unlink(missing_ok=True)
        extensions.unlink(missing_ok=True)
    names_file.write_text(names, encoding='utf-8')
    return key, cert


def fingerprint(cert: Path) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='ascii'))).hexdigest().upper()
    return ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2))


def make_server(settings: Settings, *, network: bool = False, port: int = DEFAULT_PORT, tls: bool | None = None,
                home: Home | None = None, verbose: bool = False, vpn: bool = False) -> NexusServer:
    """Le serveur prêt à tourner. Sans `network` ni `vpn`, il n'écoute que ce PC (essais).
    Avec `vpn`, il n'écoute que dans le tunnel privé : même le Wi-Fi de la maison ne le voit pas."""
    from .vpn import SERVER_ADDRESS
    tls = (network or vpn) if tls is None else tls
    data = settings.db_path.parent
    context = None
    if tls:
        key, cert = ensure_certificate(data / 'serveur', local_addresses() + [SERVER_ADDRESS])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
    access = Access(data / 'appareils.sqlite3')
    try:
        address = SERVER_ADDRESS if vpn else '0.0.0.0' if network else '127.0.0.1'
        httpd = NexusServer((address, port), home or Home(settings), access,
                            context=context, verbose=verbose,
                            names={'nexus.home.arpa', *(n.strip().lower() for n in
                                                        os.environ.get('NEXUS_SERVEUR_NOMS', '').split(',') if n.strip())})
        if tls:
            httpd.authority = data / 'serveur' / 'autorite.pem'
        return httpd
    except OSError as exc:
        access.close()
        if vpn:
            raise OllamaError(f'Impossible d\'écouter dans le tunnel ({SERVER_ADDRESS}) : est-il démarré ? '
                              '(sudo systemctl start wg-quick@nexus)') from exc
        raise OllamaError(f'Impossible d\'écouter sur le port {port} ({exc.strerror or exc})') from exc
