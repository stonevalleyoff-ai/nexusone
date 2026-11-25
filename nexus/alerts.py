"""Les alertes sur téléphone verrouillé, sans aucun service tiers.

Un navigateur s'endort quand l'écran s'éteint : l'appli web de NEXUS ne peut pas prévenir un
téléphone verrouillé. Les notifications « normales » d'Android passent, elles, par les
serveurs de Google. Ici, rien de tout ça : NEXUS parle le protocole d'abonnement de ntfy, une
appli libre qui garde une connexion ouverte avec le PC de la maison (à travers le tunnel
privé) et affiche ce qu'elle reçoit. Rien ne sort de la maison.

Ce relais est volontairement minuscule :

- lecture seule : rien ne peut être publié par le réseau, seul NEXUS dépose des alertes
  (ce sont les notifications du planificateur : rappels, courrier important, gestes faits) ;
- un seul « sujet », au nom tiré au hasard, qui sert de clé : sans lui, le relais répond
  comme s'il n'existait pas. Il se renouvelle d'une commande ;
- éteint par défaut ;
- un mode discret, pour que le contenu n'apparaisse pas sur l'écran verrouillé.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import struct
import time
from typing import Any

CACHE_HOURS = 48          # un téléphone éteint deux jours retrouve ses alertes en se reconnectant
FIRST_HOURS = 12          # un téléphone qui s'abonne pour la première fois ne reçoit pas tout l'historique d'un coup
KEEPALIVE = 45            # secondes entre deux signes de vie, comme un serveur ntfy
POLL = 2.0                # secondes entre deux regards sur les notifications
MAX_TEXT = 1500
MAX_REPLAY = 50
MAX_FRAME = 4096
ID_RE = re.compile(r'nx(\d{10})')
DURATION_RE = re.compile(r'(\d{1,6})([smhd])')
SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
DISCREET_TEXT = 'Nouveau message : ouvre NEXUS pour le lire.'


@dataclass
class Cursor:
    """Où en est un abonné : dernière alerte reçue, et plus ancienne date acceptée."""
    after: int
    since: str            # date ISO locale, comparable aux dates des notifications
    latest: bool = False  # seulement la toute dernière (puis on reprend normalement)


class Relay:
    def __init__(self, path: Path, planner: Any):
        self.path, self.planner = Path(path), planner

    # -- réglages -----------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        topic = data.get('sujet') if isinstance(data, dict) else None
        if not (isinstance(topic, str) and re.fullmatch(r'[A-Za-z0-9_-]{20,64}', topic)):
            topic = None
        return {'sujet': topic, 'discret': isinstance(data, dict) and data.get('discret') is True}

    def _save(self, settings: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # le sujet est une clé
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(settings, handle)
        os.replace(temporary, self.path)

    @property
    def topic(self) -> str | None:
        return self.settings()['sujet']

    def enabled(self) -> bool:
        return self.topic is not None

    def enable(self) -> str:
        settings = self.settings()
        if settings['sujet'] is None:
            settings['sujet'] = 'nexus-' + secrets.token_urlsafe(18)      # 144 bits tirés au hasard
            self._save(settings)
        return settings['sujet']

    def renew(self) -> str:
        """Change le sujet : tout téléphone relié à l'ancien ne reçoit plus rien."""
        settings = self.settings()
        settings['sujet'] = 'nexus-' + secrets.token_urlsafe(18)
        self._save(settings)
        return settings['sujet']

    def disable(self) -> None:
        settings = self.settings()
        settings['sujet'] = None
        self._save(settings)

    def set_discreet(self, discreet: bool) -> None:
        settings = self.settings()
        settings['discret'] = bool(discreet)
        self._save(settings)

    def matches(self, topics: str) -> bool:
        """Vrai si le sujet demandé (ou l'un d'eux, séparés par des virgules) est le nôtre."""
        topic = self.topic
        if topic is None:
            return False
        asked = topics.split(',')
        return len(asked) <= 5 and any(hmac.compare_digest(a.encode('utf-8', 'replace'), topic.encode()) for a in asked)

    def link(self, host: str, secure: bool = True) -> str | None:
        """Le lien qui ouvre l'appli ntfy du téléphone et l'abonne."""
        topic = self.topic
        return None if topic is None else f'ntfy://{host}/{topic}?display=NEXUS' + ('' if secure else '&secure=false')

    # -- alertes ------------------------------------------------------------------------
    def cursor(self, since: str = '', now: datetime | None = None) -> Cursor:
        """Traduit le « since » d'un abonné : all, none, latest, un identifiant, une durée ou une date."""
        now = now or datetime.now()
        floor = datetime.fromtimestamp(now.timestamp() - CACHE_HOURS * 3600)
        last = self.planner.last_notification()
        since = (since or 'all').strip().lower()
        if since == 'none':
            return Cursor(last, floor.isoformat())
        if since == 'latest':
            return Cursor(0, floor.isoformat(), latest=True)
        if (found := ID_RE.fullmatch(since)) is not None:
            return Cursor(min(int(found.group(1)), last), floor.isoformat())
        if (found := DURATION_RE.fullmatch(since)) is not None:
            start = now.timestamp() - int(found.group(1)) * SECONDS[found.group(2)]
            return Cursor(0, max(floor, datetime.fromtimestamp(start)).isoformat())
        if since.isdigit() and 9 <= len(since) <= 11:
            return Cursor(0, max(floor, datetime.fromtimestamp(int(since))).isoformat())
        # « all » (ou valeur inconnue) : un téléphone qui n'a encore rien reçu.
        return Cursor(0, datetime.fromtimestamp(now.timestamp() - FIRST_HOURS * 3600).isoformat())

    def events(self, cursor: Cursor, click: str | None = None) -> list[dict[str, Any]]:
        """Les alertes que cet abonné n'a pas encore reçues ; fait avancer son curseur."""
        topic = self.topic
        if topic is None:
            return []
        rows = self.planner.notifications_after(cursor.after, cursor.since[:16], 1 if cursor.latest else MAX_REPLAY)
        if cursor.latest:
            cursor.latest, cursor.after = False, self.planner.last_notification()
        elif rows:
            cursor.after = rows[-1]['id']
        discreet = self.settings()['discret']
        return [self._event(row, topic, discreet, click) for row in rows]

    @staticmethod
    def _event(row: dict[str, Any], topic: str, discreet: bool, click: str | None) -> dict[str, Any]:
        try:
            when = int(datetime.fromisoformat(row['creee']).timestamp())
        except (TypeError, ValueError):
            when = int(time.time())
        urgent = row['niveau'] in ('warning', 'critical')
        event = {'id': f"nx{row['id']:010d}", 'time': when, 'expires': when + CACHE_HOURS * 3600, 'event': 'message',
                 'topic': topic, 'title': 'NEXUS' if discreet else (row['titre'] or 'NEXUS'),
                 'message': DISCREET_TEXT if discreet else (row['texte'] or row['titre'] or 'NEXUS')[:MAX_TEXT],
                 'priority': 5 if row['niveau'] == 'critical' else 4 if urgent else 3}
        if urgent and not discreet:
            event['tags'] = ['warning']
        if click:
            event['click'] = click
        return event

    def signal(self, kind: str) -> dict[str, Any]:
        """« open » à l'ouverture d'un abonnement, « keepalive » pour dire que la ligne vit."""
        return {'id': secrets.token_urlsafe(9)[:12], 'time': int(time.time()), 'event': kind, 'topic': self.topic or ''}

    def describe(self) -> list[str]:
        settings = self.settings()
        if settings['sujet'] is None:
            return ['Alertes téléphone verrouillé : éteintes (nexus alertes activer).']
        return ['Alertes téléphone verrouillé : allumées' + (', en mode discret.' if settings['discret'] else '.'),
                f"  sujet à donner à l'appli ntfy : {settings['sujet']}"]


# -- WebSocket (RFC 6455), juste ce qu'il faut pour un abonné ---------------------------------
def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key.strip() + WS_GUID).encode('ascii', 'replace')).digest()).decode('ascii')


def ws_frame(opcode: int, payload: bytes = b'') -> bytes:
    """Une trame du serveur (jamais masquée)."""
    head = bytes([0x80 | opcode])
    if len(payload) < 126:
        return head + bytes([len(payload)]) + payload
    if len(payload) < 65536:
        return head + bytes([126]) + struct.pack('!H', len(payload)) + payload
    return head + bytes([127]) + struct.pack('!Q', len(payload)) + payload


def ws_parse(buffer: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Les trames complètes reçues du téléphone, et ce qui reste à compléter.
    ValueError si le téléphone ne respecte pas le protocole (trame non masquée ou énorme)."""
    frames: list[tuple[int, bytes]] = []
    while len(buffer) >= 2:
        opcode, second = buffer[0] & 0x0F, buffer[1]
        if not second & 0x80:
            raise ValueError('trame non masquée')
        length, offset = second & 0x7F, 2
        if length == 126:
            if len(buffer) < 4:
                break
            length, offset = struct.unpack('!H', buffer[2:4])[0], 4
        elif length == 127:
            raise ValueError('trame trop longue')
        if length > MAX_FRAME:
            raise ValueError('trame trop longue')
        if len(buffer) < offset + 4 + length:
            break
        mask = buffer[offset:offset + 4]
        data = buffer[offset + 4:offset + 4 + length]
        frames.append((opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data))))
        buffer = buffer[offset + 4 + length:]
    return frames, buffer
