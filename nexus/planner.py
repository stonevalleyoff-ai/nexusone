"""Tâches planifiées et notifications.

- Horaires simples et vérifiés : chaque jour, un jour de la semaine, un jour du mois, à une heure.
- Si l'ordinateur était éteint à l'heure prévue, la tâche est rattrapée UNE fois au réveil
  (jamais une rafale), et une même échéance n'est jamais exécutée deux fois.
- Les tâches ne font que LIRE et PRÉVENIR : elles déposent des notifications ; aucune
  n'agit sur la maison ou l'ordinateur.
- Une seule tâche accède à Internet : la veille Tempo, désactivée par défaut, qui lit la
  couleur du lendemain (requête GET vers une adresse fixe, sans aucune donnée personnelle).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable
from urllib import request

DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
KINDS = ('savoir-faire', 'echeances', 'tempo')      # types qu'une tâche ajoutée par l'utilisateur peut avoir
HEALTH_MINUTES = 5
TIME_RE = re.compile(r'^([01]?\d|2[0-3])[:h]([0-5]\d)$')
TEMPO_URL = 'https://www.api-couleur-tempo.fr/api/jourTempo/tomorrow'
TEMPO_COLORS = {1: 'bleu', 2: 'blanc', 3: 'rouge'}


# --------------------------------------------------------------------------- horaires
@dataclass(frozen=True)
class Schedule:
    kind: str            # 'quotidien' | 'hebdomadaire' | 'mensuel'
    hour: int
    minute: int
    weekday: int = 0     # 0 = lundi (hebdomadaire)
    monthday: int = 1    # 1..28 (mensuel : 28 au plus, pour exister chaque mois)

    @classmethod
    def parse(cls, raw: Any) -> 'Schedule':
        if not isinstance(raw, dict):
            raise ValueError('horaire : objet attendu')
        match = TIME_RE.match(str(raw.get('heure', '')).strip())
        if not match:
            raise ValueError('heure : format HH:MM')
        hour, minute = int(match.group(1)), int(match.group(2))
        kind = raw.get('type')
        if kind == 'quotidien':
            return cls(kind, hour, minute)
        if kind == 'hebdomadaire':
            day = str(raw.get('jour', '')).lower()
            if day not in DAYS:
                raise ValueError('jour : lundi … dimanche')
            return cls(kind, hour, minute, weekday=DAYS.index(day))
        if kind == 'mensuel':
            monthday = raw.get('jour_du_mois')
            if not isinstance(monthday, int) or isinstance(monthday, bool) or not 1 <= monthday <= 28:
                raise ValueError('jour_du_mois : de 1 à 28')
            return cls(kind, hour, minute, monthday=monthday)
        raise ValueError('type : quotidien, hebdomadaire ou mensuel')

    def to_dict(self) -> dict:
        data: dict[str, Any] = {'type': self.kind, 'heure': f'{self.hour:02d}:{self.minute:02d}'}
        if self.kind == 'hebdomadaire':
            data['jour'] = DAYS[self.weekday]
        if self.kind == 'mensuel':
            data['jour_du_mois'] = self.monthday
        return data

    def describe(self) -> str:
        at = f'{self.hour:02d}h{self.minute:02d}'
        if self.kind == 'quotidien':
            return f'chaque jour à {at}'
        if self.kind == 'hebdomadaire':
            return f'chaque {DAYS[self.weekday]} à {at}'
        return f'le {self.monthday} de chaque mois à {at}'

    def previous(self, now: datetime) -> datetime:
        """Dernière échéance ≤ now."""
        candidate = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if self.kind == 'quotidien':
            return candidate if candidate <= now else candidate - timedelta(days=1)
        if self.kind == 'hebdomadaire':
            candidate -= timedelta(days=(candidate.weekday() - self.weekday) % 7)
            return candidate if candidate <= now else candidate - timedelta(days=7)
        candidate = candidate.replace(day=self.monthday)
        if candidate <= now:
            return candidate
        month, year = (candidate.month - 1, candidate.year) if candidate.month > 1 else (12, candidate.year - 1)
        return candidate.replace(year=year, month=month)

    def next(self, now: datetime) -> datetime:
        """Prochaine échéance > now."""
        probe = now + timedelta(minutes=1)
        for days in range(0, 32):
            candidate = (probe + timedelta(days=days)).replace(hour=self.hour, minute=self.minute,
                                                               second=0, microsecond=0)
            if candidate <= now:
                continue
            if (self.kind == 'quotidien' or (self.kind == 'hebdomadaire' and candidate.weekday() == self.weekday)
                    or (self.kind == 'mensuel' and candidate.day == self.monthday)):
                return candidate
        raise AssertionError('horaire sans échéance')  # impossible avec jour_du_mois ≤ 28


# --------------------------------------------------------------------------- échéances
DEADLINE_PATTERNS = [
    (re.compile(r"(garantie[^\n]{0,60}?)jusqu'au\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I), 'Fin de garantie'),
    (re.compile(r"(validit[ée][^\n]{0,60}?)au\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I), 'Fin de validité'),
    (re.compile(r"(valable[^\n]{0,30}?)jusqu'au\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.I), 'Fin de validité'),
    (re.compile(r"((?:date limite|[ée]ch[ée]ance|avant le)[^\n]{0,20}?)\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.I),
     'Échéance'),
]
VALID_FOR_RE = re.compile(r'valable\s+(\d{1,2})\s+mois', re.I)


def find_deadlines(library: Any) -> list[dict[str, Any]]:
    """Dates d'échéance relevées dans les documents (calculées par le code, avec la source)."""
    found, seen = [], set()
    rows = library.db.execute('''SELECT d.path, d.title, d.doc_date, c.text, d.sha256 FROM chunks c
                                 JOIN documents d ON d.id = c.doc_id''').fetchall()
    for path, title, doc_date, text, sha in rows:
        for regex, kind in DEADLINE_PATTERNS:
            for match in regex.finditer(text):
                try:
                    when = date(int(match.group(4)), int(match.group(3)), int(match.group(2)))
                except ValueError:
                    continue
                key = (path, when.isoformat())
                if key not in seen:
                    seen.add(key)
                    found.append({'date': when, 'type': kind, 'path': path, 'title': title or path, 'sha': sha,
                                  'extrait': re.sub(r'\s+', ' ', match.group(0))[:120]})
        months = VALID_FOR_RE.search(text)
        if months and doc_date:
            start = date.fromisoformat(doc_date)
            total = start.month - 1 + int(months.group(1))
            try:
                when = start.replace(year=start.year + total // 12, month=total % 12 + 1)
            except ValueError:
                when = start.replace(year=start.year + total // 12, month=total % 12 + 1, day=28)
            key = (path, when.isoformat())
            if key not in seen:
                seen.add(key)
                found.append({'date': when, 'type': 'Fin de validité', 'path': path, 'title': title or path,
                              'sha': sha, 'extrait': months.group(0)})
    return sorted(found, key=lambda d: d['date'])


# --------------------------------------------------------------------------- Tempo
class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OSError('redirection refusée par NEXUS')


def fetch_tempo(opener: Callable[[str], bytes] | None = None) -> tuple[date, str | None]:
    """Couleur Tempo du lendemain : (date, 'bleu'|'blanc'|'rouge'|None si pas encore publiée)."""
    if opener is None:
        def opener(url: str) -> bytes:
            req = request.Request(url, headers={'User-Agent': 'NEXUS-local', 'Accept': 'application/json'})
            # HTTPS, adresse fixe, aucune donnée envoyée ; une redirection vers une autre adresse est refusée.
            with request.build_opener(_NoRedirect()).open(req, timeout=15) as resp:
                return resp.read(10_000)
    data = json.loads(opener(TEMPO_URL).decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('réponse Tempo inattendue')
    day = date.fromisoformat(str(data.get('dateJour', ''))[:10])
    code = data.get('codeJour')
    return day, TEMPO_COLORS.get(code if isinstance(code, int) else -1)


def tempo_message(day: date, color: str, souvenirs: list[str]) -> tuple[str, str, str]:
    """(niveau, titre, texte) : plan pour un jour rouge ou blanc, d'après des règles fixes."""
    when = f"{DAYS[day.weekday()]} {day.strftime('%d/%m')}"
    lines = []
    if color == 'rouge':
        lines += ['Heures pleines (6h-22h) au prix le plus élevé de l\'année. Plan conseillé :',
                  '- Cette nuit, de 22h à 6h (heures creuses, encore au tarif d\'aujourd\'hui) : chauffe-eau, '
                  'lave-linge, lave-vaisselle, et recharge complète de la voiture.',
                  '- Demain entre 6h et 22h : chauffage baissé d\'1 à 2 °C, aucun appareil gourmand, '
                  'pas de recharge de la voiture.']
        level = 'critical'
    else:
        lines += ['Heures pleines plus chères qu\'un jour bleu : décaler les gros appareils et la recharge '
                  'en heures creuses (22h-6h).']
        level = 'warning'
    related = [s for s in souvenirs if re.search(r'voiture|recharg|chauff|radiateur|ballon|chauffe-eau', s, re.I)]
    if related:
        lines.append('Tes habitudes notées : ' + ' · '.join(f'« {s} »' for s in related[:3]))
    lines.append('Source : couleur Tempo publiée la veille (service communautaire api-couleur-tempo.fr).')
    return level, f'Demain ({when}) : jour {color.upper()}', '\n'.join(lines)


# --------------------------------------------------------------------------- stockage
class Planner:
    def __init__(self, path: Path | str):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        if str(path) != ':memory:':
            self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS taches (
                id TEXT PRIMARY KEY, nom TEXT NOT NULL, type TEXT NOT NULL, cible TEXT,
                horaire TEXT NOT NULL, active INTEGER NOT NULL, derniere TEXT, resultat TEXT);
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, creee TEXT NOT NULL, niveau TEXT NOT NULL, titre TEXT NOT NULL,
                texte TEXT NOT NULL, tache TEXT, lue INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deja_signale (cle TEXT PRIMARY KEY, le TEXT NOT NULL);
        ''')
        self.db.commit()
        self._defaults()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> 'Planner':
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _defaults(self) -> None:
        defaults = [
            ('veille-tempo', 'Veille Tempo (Internet)', 'tempo', None, {'type': 'quotidien', 'heure': '19:30'}, 0),
            ('echeances', 'Échéances des documents', 'echeances', None,
             {'type': 'hebdomadaire', 'jour': 'lundi', 'heure': '08:40'}, 1),
            ('bilan-energie-mensuel', 'Bilan énergie du mois', 'savoir-faire', 'bilan-energie',
             {'type': 'mensuel', 'jour_du_mois': 1, 'heure': '08:50'}, 0),
            ('check-up-hebdo', 'Check-up du PC', 'savoir-faire', 'check-up-pc',
             {'type': 'hebdomadaire', 'jour': 'dimanche', 'heure': '10:10'}, 0),
            ('agenda-demain', 'Agenda : ce qui est prévu demain', 'agenda', None,
             {'type': 'quotidien', 'heure': '19:00'}, 1),
            ('tournee-nuit', 'Tournée de nuit : documents reçus à traiter', 'tournee', None,
             {'type': 'quotidien', 'heure': '02:30'}, 1),
            ('courrier-nuit', 'Relève du courrier (lecture seule)', 'courrier', None,
             {'type': 'quotidien', 'heure': '03:30'}, 1),
            ('sauvegarde-nuit', 'Sauvegarde chiffrée (si un disque est réglé)', 'sauvegarde', None,
             {'type': 'quotidien', 'heure': '04:15'}, 1),
            ('veille-versions', 'Y a-t-il une nouvelle version de NEXUS ? (lecture seule)', 'versions', None,
             {'type': 'quotidien', 'heure': '05:10'}, 1),
            ('journal-matin', 'Journal du matin', 'journal', None, {'type': 'quotidien', 'heure': '06:30'}, 1),
        ]
        with self._lock, self.db:
            for task_id, name, kind, target, schedule, active in defaults:
                self.db.execute('INSERT OR IGNORE INTO taches(id, nom, type, cible, horaire, active) '
                                'VALUES (?,?,?,?,?,?)', (task_id, name, kind, target, json.dumps(schedule), active))

    # -- tâches -------------------------------------------------------------------
    def tasks(self) -> list[dict[str, Any]]:
        rows = self.db.execute('SELECT id, nom, type, cible, horaire, active, derniere, resultat FROM taches '
                               'ORDER BY id').fetchall()
        tasks = []
        for task_id, name, kind, target, schedule, active, last, result in rows:
            tasks.append({'id': task_id, 'nom': name, 'type': kind, 'cible': target,
                          'horaire': Schedule.parse(json.loads(schedule)), 'active': bool(active),
                          'derniere': last, 'resultat': result})
        return tasks

    def get(self, task_id: str) -> dict[str, Any] | None:
        return next((t for t in self.tasks() if t['id'] == task_id), None)

    def add(self, task_id: str, name: str, kind: str, schedule: dict, target: str | None = None,
            active: bool = True) -> None:
        if kind not in KINDS or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,40}', task_id):
            raise ValueError('tâche invalide')
        Schedule.parse(schedule)
        with self._lock, self.db:
            # Une tâche créée ou modifiée part de maintenant (pas de rattrapage du passé).
            self.db.execute('''INSERT INTO taches(id, nom, type, cible, horaire, active, derniere)
                               VALUES (?,?,?,?,?,?,?)
                               ON CONFLICT(id) DO UPDATE SET nom=excluded.nom, type=excluded.type,
                                   cible=excluded.cible, horaire=excluded.horaire, active=excluded.active,
                                   derniere=excluded.derniere''',
                            (task_id, name[:60], kind, target, json.dumps(schedule), int(active),
                             datetime.now().isoformat(timespec='minutes')))

    def set_active(self, task_id: str, active: bool, now: datetime | None = None) -> bool:
        with self._lock, self.db:
            # À l'activation, on part de maintenant : pas de rattrapage d'échéances passées.
            stamp = (now or datetime.now()).isoformat(timespec='minutes') if active else None
            if active:
                return self.db.execute('UPDATE taches SET active=1, derniere=? WHERE id=?',
                                       (stamp, task_id)).rowcount > 0
            return self.db.execute('UPDATE taches SET active=0 WHERE id=?', (task_id,)).rowcount > 0

    def set_schedule(self, task_id: str, schedule: dict, now: datetime | None = None) -> bool:
        Schedule.parse(schedule)
        with self._lock, self.db:
            # On repart de maintenant : sinon une tâche déjà faite aujourd'hui serait relancée
            # si le nouvel horaire tombe entre sa dernière exécution et maintenant.
            stamp = (now or datetime.now()).isoformat(timespec='minutes')
            return self.db.execute('UPDATE taches SET horaire=?, derniere=? WHERE id=?',
                                   (json.dumps(schedule), stamp, task_id)).rowcount > 0

    def claim_due(self, now: datetime) -> list[dict[str, Any]]:
        """Tâches actives dont une échéance est passée depuis la dernière exécution ; marquées aussitôt.

        Une tâche manquée plusieurs fois (ordinateur éteint) n'est exécutée qu'une fois.
        """
        due = []
        with self._lock:
            # Transaction exclusive : si l'interface et le service tournent ensemble, une seule
            # des deux prend la tâche.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                due = self._claim(now)
                self.db.commit()
                # Rattrapage après un arrêt : dans l'ordre des heures prévues (le courrier et la
                # sauvegarde de la nuit avant le journal du matin, qui en parle), pas dans celui des noms.
                due.sort(key=lambda task: task['horaire'].previous(now))
            except BaseException:
                self.db.rollback()
                raise
        return due

    def _claim(self, now: datetime) -> list[dict[str, Any]]:
        due = []
        for task in self.tasks():
            if not task['active']:
                continue
            last_due = task['horaire'].previous(now)
            last_run = datetime.fromisoformat(task['derniere']) if task['derniere'] else None
            if last_run is not None and last_run > now + timedelta(days=1):
                last_run = None   # horloge fausse lors d'une exécution passée : ne pas bloquer la tâche
            if last_run is None or last_run < last_due:
                self.db.execute('UPDATE taches SET derniere=? WHERE id=?',
                                (now.isoformat(timespec='minutes'), task['id']))
                due.append(task)
        return due

    def record(self, task_id: str, result: str) -> None:
        with self._lock, self.db:
            self.db.execute('UPDATE taches SET resultat=? WHERE id=?', (result[:300], task_id))

    # -- notifications ------------------------------------------------------------------
    def notify(self, level: str, title: str, text: str, task: str | None = None,
               once_key: str | None = None) -> int | None:
        with self._lock, self.db:
            if once_key is not None:
                if self.db.execute('SELECT 1 FROM deja_signale WHERE cle=?', (once_key,)).fetchone():
                    return None
                self.db.execute('INSERT INTO deja_signale VALUES (?,?)', (once_key, datetime.now().isoformat()))
            cur = self.db.execute('INSERT INTO notifications(creee, niveau, titre, texte, tache) VALUES (?,?,?,?,?)',
                                  (datetime.now().isoformat(timespec='minutes'), level, title[:120], text[:4000], task))
            return cur.lastrowid

    def notifications(self, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        sql = 'SELECT id, creee, niveau, titre, texte, tache, lue FROM notifications'
        if unread_only:
            sql += ' WHERE lue=0'
        keys = ['id', 'creee', 'niveau', 'titre', 'texte', 'tache', 'lue']
        return [dict(zip(keys, r)) for r in self.db.execute(sql + ' ORDER BY id DESC LIMIT ?', (limit,))]

    def notifications_after(self, after_id: int = 0, since: str = '', limit: int = 50) -> list[dict[str, Any]]:
        """Les notifications plus récentes qu'un numéro et qu'une date, dans l'ordre (les `limit` dernières)."""
        keys = ['id', 'creee', 'niveau', 'titre', 'texte', 'tache']
        with self._lock:
            rows = self.db.execute('SELECT id, creee, niveau, titre, texte, tache FROM notifications '
                                   'WHERE id>? AND creee>=? ORDER BY id DESC LIMIT ?', (after_id, since, limit)).fetchall()
        return [dict(zip(keys, r)) for r in reversed(rows)]

    def last_notification(self) -> int:
        with self._lock:
            return self.db.execute('SELECT COALESCE(MAX(id), 0) FROM notifications').fetchone()[0]

    def unread(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM notifications WHERE lue=0').fetchone()[0]

    def mark_read(self, notification_id: int | None = None) -> None:
        with self._lock, self.db:
            if notification_id is None:
                self.db.execute('UPDATE notifications SET lue=1')
            else:
                self.db.execute('UPDATE notifications SET lue=1 WHERE id=?', (notification_id,))


# --------------------------------------------------------------------------- exécution
class Runner:
    """Exécute les tâches dues. `library`, `skills`, `toolbox`, `souvenirs` peuvent manquer."""

    def __init__(self, planner: Planner, *, toolbox: Any = None, skills: Any = None, souvenirs: Any = None,
                 tempo_fetch: Callable[[], tuple[date, str | None]] = fetch_tempo,
                 deadline_days: int = 30, autonomy: Any = None,
                 journal: Callable[[datetime], dict[str, Any]] | None = None, mailbox: Any = None,
                 backup: Any = None, health: Any = None, updater: Any = None):
        self.planner, self.toolbox, self.skills, self.souvenirs = planner, toolbox, skills, souvenirs
        self.autonomy = autonomy      # nexus/autonomy.py : ce que NEXUS fait de lui-même
        self.journal = journal        # nexus/journal.py : compose le numéro du jour
        self.mailbox = mailbox        # nexus/mail.py : courrier en lecture seule
        self.backup = backup          # nexus/backup.py : sauvegarde chiffrée de la nuit
        self.health = health          # nexus/health.py : NEXUS se surveille lui-même
        self.updater = updater        # nexus/update.py : annonce des nouvelles versions (n'installe rien)
        self._health_at: datetime | None = None
        self._health_seen: set[str] = set()
        self.tempo_fetch, self.deadline_days = tempo_fetch, deadline_days

    def run_now(self, task_id: str, now: datetime | None = None) -> str:
        """« Lancer maintenant » : exécute la tâche tout de suite, même si elle est désactivée."""
        task = self.planner.get(task_id)
        if task is None:
            raise ValueError('tâche inconnue')
        result = self.run(task, now or datetime.now())
        self.planner.record(task_id, result)
        return result

    def tick(self, now: datetime | None = None) -> list[str]:
        """Une vérification : exécute ce qui est dû. Renvoie les identifiants exécutés."""
        now = now or datetime.now()
        done = []
        agenda = getattr(getattr(self.toolbox, 'env', None), 'agenda', None)
        if agenda is not None:      # rappels de l'agenda : à chaque passage, une seule fois chacun
            try:
                for reminder in agenda.due_reminders(now):
                    self.planner.notify(reminder['level'], reminder['title'], reminder['text'], 'agenda',
                                        once_key=reminder['key'])
            except Exception:       # un agenda illisible ne doit pas arrêter les autres tâches
                pass
        self._watch_health(now)
        for task in self.planner.claim_due(now):
            try:
                result = self.run(task, now)
            except Exception as exc:  # une tâche en échec ne bloque pas les autres
                result = f'échec : {type(exc).__name__}'
                self.planner.notify('warning', f'Tâche « {task["nom"]} » en échec',
                                    f'{exc}'[:300] or type(exc).__name__, task['id'])
            self.planner.record(task['id'], result)
            done.append(task['id'])
        return done

    def _watch_health(self, now: datetime) -> None:
        """Toutes les cinq minutes : un constat sérieux qui dure depuis le passage précédent devient une
        notification, une fois par jour. Un souci passager (moteur qui redémarre) ne dérange personne."""
        if self.health is None or (self._health_at is not None and (now - self._health_at).total_seconds() < HEALTH_MINUTES * 60):
            return
        self._health_at = now
        try:
            serious = [f for f in self.health.check(now) if f.level != 'info']
        except Exception:
            return
        for finding in serious:
            if finding.key in self._health_seen:
                self.planner.notify(finding.level, 'NEXUS : ' + finding.title, finding.advice, 'sante',
                                    once_key=f'sante:{finding.key}:{now.date().isoformat()}')
        self._health_seen = {f.key for f in serious}

    def run(self, task: dict[str, Any], now: datetime) -> str:
        if task['type'] == 'tempo':
            day, color = self.tempo_fetch()
            if color is None:
                return f'couleur du {day.isoformat()} pas encore publiée'
            if color == 'bleu':
                return f'{day.isoformat()} : bleu, rien à signaler'
            memories = [row['texte'] for row in self.souvenirs.list()] if self.souvenirs is not None else []
            level, title, text = tempo_message(day, color, memories)
            self.planner.notify(level, title, text, task['id'], once_key=f'tempo:{day.isoformat()}')
            return f'{day.isoformat()} : {color}, notification envoyée'
        if task['type'] == 'versions':
            if self.updater is None:
                return 'mises à jour non configurées'
            result = self.updater.check(now)
            if not result['ok']:
                return 'vérification impossible : ' + result['message'][:160]
            if result['deplacees']:
                self.planner.notify('critical', 'NEXUS : une version publiée a changé de contenu',
                                    f"Étiquettes : {', '.join(result['deplacees'])}. N'installe rien et vérifie qui a accès "
                                    'à ton dépôt.', task['id'], once_key='versions-deplacees:' + ','.join(result['deplacees']))
            if result['disponible']:      # annoncée une fois par version ; rien n'est installé sans toi
                self.planner.notify('info', f"NEXUS {result['disponible']} est disponible",
                                    f"Tu as la {result['actuelle']}. Pour l'installer, sur le PC : nexus mise-a-jour installer "
                                    '(elle passe ses tests avant, et se défait si elle ne tient pas).',
                                    task['id'], once_key=f"version-disponible:{result['disponible']}")
                return f"version {result['disponible']} disponible"
            return f"à jour ({result['actuelle']})"
        if task['type'] == 'sauvegarde':
            if self.backup is None or not self.backup.configured():
                return 'aucun disque de sauvegarde réglé'
            result = self.backup.run(now)
            if not result['ok']:      # une sauvegarde qui échoue en silence ne sert à rien : on prévient, une fois par jour
                self.planner.notify('warning', 'Sauvegarde : échec cette nuit', result['message']
                                    + '\nTes documents ne sont pas protégés tant que ce n\'est pas réglé (nexus sauvegarde etat).',
                                    task['id'], once_key=f'sauvegarde-echec:{now.date().isoformat()}')
                return 'échec : ' + result['message']
            return result['message']
        if task['type'] == 'courrier':
            if self.mailbox is None or not self.mailbox.accounts():
                return 'aucune boîte mail renseignée'
            on_message = self.autonomy.handle_mail if self.autonomy is not None else None
            counts = self.mailbox.collect(now, on_message)
            for problem in self.mailbox.errors:
                self.planner.notify('warning', 'Courrier : relève impossible', problem, task['id'],
                                    once_key=f"courrier-erreur:{now.date().isoformat()}:{problem[:60]}")
            mails = self.mailbox.important(now, hours=6) if counts['importants'] else []
            if mails:
                lines = [f"- {m['de']} : {m['objet']}" + (f" — {m['resume']}" if m['resume'] else '') for m in mails[:12]]
                self.planner.notify('info', f"Courrier : {len(mails)} courriel{'s' if len(mails) > 1 else ''} "
                                            f"important{'s' if len(mails) > 1 else ''}", '\n'.join(lines), task['id'])
            return f"{counts['lus']} courriel(s) lu(s), {counts['importants']} important(s)"
        if task['type'] == 'journal':
            if self.journal is None:
                return 'journal non configuré'
            result = self.journal(now)
            done = ('imprimé' if result['imprime'] else 'PDF prêt' if result['pdf'] else 'page prête, sans PDF')
            self.planner.notify('info', 'Le journal du jour est prêt',
                                f"Numéro du {now.strftime('%d/%m/%Y')} : {done}. Onglet Maison de l'appli, ou nexus journal."
                                + ''.join('\n' + note for note in result['notes']), task['id'],
                                once_key=f"journal:{now.date().isoformat()}")
            return done
        if task['type'] == 'tournee':
            if self.autonomy is None:
                return 'autonomie non configurée'
            results = self.autonomy.tour(now)
            done = sum(1 for r in results if r['etat'] == 'fait')
            return f'{done} geste(s) fait(s), {len(results) - done} proposition(s)'
        if task['type'] == 'agenda':
            agenda = getattr(getattr(self.toolbox, 'env', None), 'agenda', None)
            if agenda is None:
                return 'agenda non configuré'
            if now.hour < 17:      # rattrapage d'un passage manqué : le résumé de demain part le soir
                return 'trop tôt : le résumé de demain part le soir'
            tomorrow = now.date() + timedelta(days=1)
            start = datetime.combine(tomorrow, datetime.min.time())
            events = agenda.events(start, start + timedelta(days=1))
            todos = [t for t in agenda.todos() if t['echeance'] and t['echeance'] <= tomorrow.isoformat()]
            if not events and not todos:
                return 'rien de prévu demain'
            lines = []
            for event in events:
                moment = datetime.fromisoformat(event['quand'])
                lines.append('- ' + ('journée' if event['journee'] else f'{moment.hour}h{moment.minute:02d}')
                             + f" : {event['titre']}" + (f" — {event['lieu']}" if event['lieu'] else ''))
            for todo in todos:
                late = todo['echeance'] < tomorrow.isoformat() and todo['echeance'] < now.date().isoformat()
                lines.append(f"- à faire : {todo['texte']}" + (' (en retard)' if late else ''))
            parts = ([f"{len(events)} rendez-vous"] if events else []) + (
                [f"{len(todos)} chose{'s' if len(todos) > 1 else ''} à faire"] if todos else [])
            self.planner.notify('info', 'Demain : ' + ' et '.join(parts), '\n'.join(lines), task['id'],
                                once_key=f'agenda-demain:{tomorrow.isoformat()}')
            return f"{len(events)} rendez-vous et {len(todos)} chose(s) à faire signalés"
        if task['type'] == 'echeances':
            library = getattr(getattr(self.toolbox, 'env', None), 'library', None)
            if library is None:
                return 'bibliothèque de documents non configurée'
            soon = [d for d in find_deadlines(library)
                    if now.date() <= d['date'] <= now.date() + timedelta(days=self.deadline_days)]
            for item in soon:
                days = (item['date'] - now.date()).days
                # Deux rappels par échéance : à 30 jours, puis à 7 jours. La clé suit le CONTENU
                # du document (pas son chemin) : un rangement ne provoque pas de doublon.
                stage = 'j7' if days <= 7 else 'j30'
                self.planner.notify(
                    'warning' if days <= 7 else 'info',
                    f"{item['type']} dans {days} jour(s) : {item['title']}",
                    f"Le {item['date'].strftime('%d/%m/%Y')} — « {item['extrait']} »\nDocument : {item['path']}",
                    task['id'], once_key=f"echeance:{item['sha']}:{item['date'].isoformat()}:{stage}")
            return f'{len(soon)} échéance(s) dans les {self.deadline_days} jours'
        if task['type'] == 'savoir-faire':
            from .reports import REPORTS
            from .skills import fill
            skill = self.skills.get(task['cible']) if self.skills is not None else None
            if skill is None or skill.statut != 'valide':
                return f'savoir-faire « {task["cible"]} » introuvable ou non activé'
            if skill.rapport not in REPORTS:
                return 'seuls les savoir-faire à rapport calculé peuvent être planifiés pour l\'instant'
            raw = {step.outil: self.toolbox.call(step.outil, {k: fill(v, skill.nom) for k, v in step.arguments.items()})
                   for step in skill.etapes}
            report = REPORTS[skill.rapport](raw, self.toolbox.env, now.date())
            self.planner.notify('info', f'{skill.nom} — {now.strftime("%d/%m/%Y")}', report, task['id'])
            return 'rapport déposé dans les notifications'
        return 'type de tâche inconnu'
