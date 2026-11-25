"""L'agenda de la maison : rendez-vous, rappels et liste de choses à faire.

Tout est dans un fichier SQLite local. Trois principes :

- les dates sont comprises par des règles (« demain à 9h », « le 12 décembre », « tous les
  lundis »), pas par le modèle : une date mal devinée ferait manquer un rendez-vous ;
- NEXUS n'écrit dans l'agenda que sur une demande explicite (« rappelle-moi… », « ajoute… »)
  ou après validation d'une proposition ; l'IA, elle, ne fait que le consulter ;
- un rappel n'est envoyé qu'une fois, et pas pour un rendez-vous passé depuis longtemps
  (PC resté éteint).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
import unicodedata

REPEATS = {'': 'une fois', 'quotidien': 'tous les jours', 'hebdomadaire': 'toutes les semaines',
           'mensuel': 'tous les mois', 'annuel': 'tous les ans'}
DEFAULT_REMINDER = 30                  # minutes avant un rendez-vous à heure fixe
ALL_DAY_REMINDER = clock(8, 0)         # un événement sur la journée est rappelé le matin
STALE = timedelta(hours=6)             # au-delà, un rappel manqué n'est plus envoyé
TITLE_LIMIT, TEXT_LIMIT = 120, 300
DAYS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
MONTHS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre',
          'octobre', 'novembre', 'décembre']


def tidy(text: Any, limit: int) -> str:
    text = ' '.join(str(text or '').split())
    return ''.join(ch for ch in text if ch.isprintable())[:limit].strip()


def plain(text: str) -> str:
    """Minuscules sans accents, de même longueur que le texte (les positions se correspondent)."""
    out = []
    for char in unicodedata.normalize('NFC', text):
        base = unicodedata.normalize('NFKD', char)[0].lower()
        out.append(' ' if char in "'’-" else base if len(base) == 1 else char)
    return ''.join(out)


def french_date(day: date, today: date | None = None) -> str:
    today = today or date.today()
    if day == today:
        return 'aujourd\'hui'
    if day == today + timedelta(days=1):
        return 'demain'
    text = f'{DAYS[day.weekday()]} {day.day}{"er" if day.day == 1 else ""} {MONTHS[day.month - 1]}'
    return text if day.year == today.year else f'{text} {day.year}'


def french_time(moment: datetime) -> str:
    return f'{moment.hour}h{moment.minute:02d}' if moment.minute else f'{moment.hour}h'


# -- compréhension des dates ------------------------------------------------------------------
@dataclass
class When:
    start: datetime
    all_day: bool
    repeat: str = ''
    spans: tuple[tuple[int, int], ...] = ()     # morceaux du texte qui disaient la date


_NUMBER = r'(\d{1,2})'
_MONTH_NAMES = [plain(m) for m in MONTHS]
_MONTH_RE = '|'.join(_MONTH_NAMES)
_DAY_RE = '|'.join(DAYS)
_TIME_RE = re.compile(r'\b(?:a|vers|pour|des)?\s*(\d{1,2})\s*(?:h|heures?|:)\s*(\d{2})?\b|\b(?:a|vers)\s+(midi|minuit)\b')
_REPEAT_RE = re.compile(rf'\b(?:tous les jours|chaque jour)\b|\b(?:tous les|chaque) ({_DAY_RE})s?\b|'
                        r'\b(?:toutes les semaines|chaque semaine)\b|\b(?:tous les mois|chaque mois)\b|'
                        r'\b(?:tous les ans|chaque annee)\b')
_DATE_RES = [
    ('numeric', re.compile(rf'\b(?:le\s+)?{_NUMBER}/{_NUMBER}(?:/(\d{{4}}|\d{{2}}))?\b')),
    ('named', re.compile(rf'\b(?:le\s+)?(?:(?:{_DAY_RE})\s+)?{_NUMBER}(?:er)?\s+({_MONTH_RE})(?:\s+(\d{{4}}))?\b')),
    ('after_tomorrow', re.compile(r'\bapres demain\b')),
    ('tomorrow', re.compile(r'\bdemain\b')),
    ('today', re.compile(r'\b(aujourd hui|ce matin|cet apres midi|ce soir|ce midi|tout a l heure)\b')),
    ('in', re.compile(r'\bdans\s+(\d{1,3}|une?)\s+(minutes?|heures?|jours?|semaines?|mois)\b')),
    ('next_week', re.compile(r'\bla semaine prochaine\b')),
    ('weekday', re.compile(rf'\b(?:ce\s+|le\s+)?({_DAY_RE})(\s+prochain)?\b')),
    ('day_only', re.compile(rf'\ble\s+{_NUMBER}(?:er)?\b(?!\s*(?:h|heures?|:|/))')),
]


def parse_when(text: str, now: datetime) -> When | None:
    """La date (et l'heure) dites dans le texte, ou None si rien n'est sûr."""
    low = plain(text)
    spans: list[tuple[int, int]] = []
    day: date | None = None
    moment: clock | None = None
    repeat = ''
    exact: datetime | None = None

    match = _REPEAT_RE.search(low)
    if match:
        spans.append(match.span())
        phrase = match.group(0)
        if match.group(1):
            repeat = 'hebdomadaire'
            day = now.date() + timedelta(days=(DAYS.index(match.group(1)) - now.weekday()) % 7)
        elif 'jour' in phrase:
            repeat, day = 'quotidien', now.date()
        elif 'semaine' in phrase:
            repeat = 'hebdomadaire'
        elif 'mois' in phrase:
            repeat = 'mensuel'
        else:
            repeat = 'annuel'

    for kind, pattern in _DATE_RES:
        if day is not None and not (kind == 'named' or kind == 'numeric'):
            break
        match = pattern.search(low)
        if not match or any(a < match.end() and match.start() < b for a, b in spans):
            continue
        found: date | None = None
        try:
            if kind == 'numeric':
                year = int(match.group(3)) if match.group(3) else now.year
                found = date(year + 2000 if year < 100 else year, int(match.group(2)), int(match.group(1)))
                if not match.group(3) and found < now.date():
                    found = found.replace(year=found.year + 1)
            elif kind == 'named':
                month = _MONTH_NAMES.index(match.group(2)) + 1
                found = date(int(match.group(3)) if match.group(3) else now.year, month, int(match.group(1)))
                if not match.group(3) and found < now.date():
                    found = found.replace(year=found.year + 1)
            elif kind == 'after_tomorrow':
                found = now.date() + timedelta(days=2)
            elif kind == 'tomorrow':
                found = now.date() + timedelta(days=1)
            elif kind == 'today':
                found = now.date()
                moment = {'ce soir': clock(19, 0), 'ce midi': clock(12, 0)}.get(match.group(1), moment)
            elif kind == 'in':
                count = 1 if match.group(1) in ('un', 'une') else int(match.group(1))
                unit = match.group(2)
                if unit.startswith(('minute', 'heure')):
                    exact = now + (timedelta(minutes=count) if unit.startswith('minute') else timedelta(hours=count))
                    exact = exact.replace(second=0, microsecond=0)
                    found = exact.date()
                elif unit.startswith('mois'):
                    month = now.month - 1 + count
                    year, month = now.year + month // 12, month % 12 + 1
                    found = date(year, month, min(now.day, calendar.monthrange(year, month)[1]))
                else:
                    found = now.date() + timedelta(days=count * (7 if unit.startswith('semaine') else 1))
            elif kind == 'next_week':
                found = now.date() + timedelta(days=7 - now.weekday())
            elif kind == 'weekday':
                ahead = (DAYS.index(match.group(1)) - now.weekday()) % 7
                found = now.date() + timedelta(days=ahead or 7)
            elif kind == 'day_only':
                number = int(match.group(1))
                found = now.date().replace(day=number)
                if found < now.date():
                    month = now.month % 12 + 1
                    found = date(now.year + (now.month == 12), month, number)
        except ValueError:
            return None            # « le 31 février » : on ne devine pas
        if found is not None:
            day = found if day is None or kind in ('numeric', 'named') else day
            spans.append(match.span())
            if not repeat:
                break

    for match in _TIME_RE.finditer(low):
        if any(a < match.end() and match.start() < b for a, b in spans):
            continue
        if match.group(3):
            hour, minute = (12, 0) if match.group(3) == 'midi' else (0, 0)
        else:
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if hour > 23 or minute > 59:
            continue
        moment = clock(hour, minute)
        exact = None
        spans.append(match.span())
        break

    if day is None and moment is None and not repeat:
        return None
    if day is None:      # une heure sans jour : aujourd'hui si elle n'est pas passée, sinon demain
        day = now.date() if moment is None or datetime.combine(now.date(), moment) > now else now.date() + timedelta(days=1)
    if exact is not None:
        return When(exact, False, repeat, tuple(sorted(spans)))
    start = datetime.combine(day, moment or clock(0, 0))
    if moment is not None and start <= now and repeat in ('quotidien', 'hebdomadaire'):
        start += timedelta(days=1 if repeat == 'quotidien' else 7)     # première fois : la prochaine, pas une passée
    return When(start, moment is None, repeat, tuple(sorted(spans)))


def without(text: str, spans: tuple[tuple[int, int], ...]) -> str:
    """Le texte sans les morceaux qui disaient la date."""
    text = unicodedata.normalize('NFC', text)
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + ' ' + text[end:]
    return ' '.join(text.split())


_DEADLINE_RE = re.compile(r'\b(avant|d ici|au plus tard|pour)\s+(?:le |la |ce |cet |cette )?\S')
_LEADING = re.compile(r"^(?:de |d'|d’|que |qu'|qu’|à |:)\s*", re.I)
_TRAILING = re.compile(r"\s+(?:avant|pour|d'ici|d’ici|le|la|au|à|a|,|et)$", re.I)


def as_title(text: str, limit: int, noun: str = '') -> str:
    """Un titre propre : sans « de » initial ni préposition orpheline en fin de phrase."""
    text = tidy(text, limit).strip(' .,!:;')
    previous = None
    while previous != text:
        previous = text
        text = _TRAILING.sub('', _LEADING.sub('', text)).strip(' .,!:;')
    if noun and re.match(r"(?:chez|avec|au|aux|à|pour|du|des|d'|d’)\b", text, re.I):
        text = f'{noun} {text}'       # « chez le dentiste » → « Rendez-vous chez le dentiste »
    return text[:1].upper() + text[1:]


# -- commandes en langage courant --------------------------------------------------------------
@dataclass
class Command:
    kind: str                       # 'event', 'todo', 'done'
    title: str
    when: When | None = None
    remind: int = DEFAULT_REMINDER  # minutes avant ; 0 : à l'heure dite (« rappelle-moi… »)


_REMIND_RE = re.compile(r'^\s*(?:peux tu |tu peux |merci de )?(?:me rappeler|rappelle moi|rappelle nous|fais moi penser|'
                        r'pense a me rappeler)\s+(?:de |d |que |qu |a |l |le |la |les )?')
_EVENT_RE = re.compile(r'^\s*(?:peux tu |tu peux )?(?:ajoute|ajouter|note|noter|mets|mettre|inscris|programme|planifie|cree|creer)\s+'
                       r'(?:moi\s+)?(?:(?:dans|a|sur)\s+(?:mon|l|le)\s+(?:agenda|calendrier)\s+)?'
                       r'(?:(?:un|une|le|la|mon|ma)\s+)?(?:rendez vous|rdv|evenement|reunion|rappel)\s*(?::\s*)?')
_EVENT_TAIL_RE = re.compile(r'\s+(?:dans|a|sur)\s+(?:mon|l|le)\s+(?:agenda|calendrier)\s*[.!]?\s*$')
_TODO_RES = [
    re.compile(r'^\s*(?:peux tu |tu peux )?(?:ajoute|ajouter|mets|mettre|note|noter|rajoute|inscris)\s+(?P<what>.+?)\s+'
               r'(?:a|dans|sur)\s+(?:ma|la|notre)\s+(?:liste|todo(?: list)?)(?:\s+de\s+(?:choses a faire|taches|courses))?\s*[.!]?\s*$'),
    re.compile(r'^\s*(?:ajoute|ajouter|cree|creer|note|noter)\s+(?:une\s+)?(?:tache|chose a faire)\s*:?\s+(?P<what>.+)$'),
    re.compile(r'^\s*(?:a faire|todo|to do)\s*:\s*(?P<what>.+)$'),
]
_DONE_RE = re.compile(r'^\s*(?:raye|rayer|coche|cocher|barre|barrer)\s+(?P<what>.+?)(?:\s+(?:de|dans|sur)\s+(?:ma|la)\s+liste)?\s*[.!]?\s*$|'
                      r'^\s*(?:j ai|c est)\s+(?:fait|fini|termine)\s*:?\s+(?P<what2>.+?)\s*[.!]?\s*$')


def parse_command(message: str, now: datetime) -> Command | None:
    """Une demande explicite d'écriture dans l'agenda, ou None (le modèle répondra)."""
    message = unicodedata.normalize('NFC', message.strip())
    if not message or len(message) > 400 or message.endswith('?'):
        return None
    low = plain(message)
    for pattern, kind in ((_REMIND_RE, 'event'), (_EVENT_RE, 'event')):
        match = pattern.match(low)
        if match:
            rest = message[match.end():]
            tail = _EVENT_TAIL_RE.search(plain(rest))
            if tail:
                rest = rest[:tail.start()]
            noun = 'Rendez-vous' if kind == 'event' and re.search(r'rendez vous|rdv', match.group(0)) else ''
            when = parse_when(rest, now)
            remind = 0 if pattern is _REMIND_RE else DEFAULT_REMINDER
            if when is None:      # pas de date comprise : ce sera une chose à faire, et on le dira
                return Command('event', as_title(rest, TITLE_LIMIT, noun), None, remind)
            return Command('event', as_title(without(rest, when.spans), TITLE_LIMIT, noun), when, remind)
    for pattern in _TODO_RES:
        match = pattern.match(low)
        if match:
            start, end = match.span('what')
            what = message[start:end]
            when = parse_when(what, now)
            return Command('todo', as_title(without(what, when.spans) if when else what, TEXT_LIMIT), when)
    match = _DONE_RE.match(low)
    if match:
        group = 'what' if match.group('what') else 'what2'
        start, end = match.span(group)
        return Command('done', tidy(message[start:end], TEXT_LIMIT))
    return None


# -- stockage ------------------------------------------------------------------------------
class Agenda:
    def __init__(self, path: Path | str):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        if str(path) != ':memory:':
            self.db.execute('PRAGMA journal_mode=WAL')    # partagé : fenêtre, serveur, ligne de commande
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS evenements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, titre TEXT NOT NULL, debut TEXT NOT NULL,
                journee INTEGER NOT NULL DEFAULT 0, lieu TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                rappel INTEGER, repetition TEXT NOT NULL DEFAULT '', origine TEXT NOT NULL DEFAULT 'utilisateur',
                cree_le TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS taches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, texte TEXT NOT NULL, echeance TEXT,
                faite INTEGER NOT NULL DEFAULT 0, faite_le TEXT, origine TEXT NOT NULL DEFAULT 'utilisateur',
                cree_le TEXT NOT NULL);
        ''')
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> 'Agenda':
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- rendez-vous ----------------------------------------------------------------------
    def add_event(self, title: str, start: datetime, *, all_day: bool = False, place: str = '', notes: str = '',
                  remind: int | None = DEFAULT_REMINDER, repeat: str = '', origin: str = 'utilisateur') -> int:
        title = tidy(title, TITLE_LIMIT)
        if not title:
            raise ValueError('titre vide')
        if repeat not in REPEATS:
            raise ValueError('répétition inconnue')
        if remind is not None and not 0 <= remind <= 7 * 24 * 60:
            raise ValueError('rappel hors limites')
        start = start.replace(second=0, microsecond=0, tzinfo=None)
        if all_day:
            start = start.replace(hour=0, minute=0)
        with self._lock, self.db:
            return int(self.db.execute(
                'INSERT INTO evenements(titre, debut, journee, lieu, notes, rappel, repetition, origine, cree_le) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (title, start.isoformat(timespec='minutes'), int(all_day), tidy(place, TITLE_LIMIT),
                 tidy(notes, TEXT_LIMIT), None if all_day else remind, repeat, tidy(origin, 40),
                 datetime.now().isoformat(timespec='seconds'))).lastrowid)

    def _events(self) -> list[dict[str, Any]]:
        keys = ['id', 'titre', 'debut', 'journee', 'lieu', 'notes', 'rappel', 'repetition', 'origine']
        with self._lock:
            rows = self.db.execute(f"SELECT {', '.join(keys)} FROM evenements").fetchall()
        return [dict(zip(keys, row)) for row in rows]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return next((e for e in self._events() if e['id'] == event_id), None)

    def delete_event(self, event_id: int) -> bool:
        with self._lock, self.db:
            return self.db.execute('DELETE FROM evenements WHERE id=?', (event_id,)).rowcount > 0

    @staticmethod
    def _occurrences(event: dict[str, Any], start: datetime, end: datetime) -> list[datetime]:
        """Dates de l'événement dans [start, end[ (une seule s'il ne se répète pas)."""
        first = datetime.fromisoformat(event['debut'])
        repeat = event['repetition']
        if not repeat:
            return [first] if start <= first < end else []
        found, current, index = [], first, 0
        while current < end and index < 5000:
            if current >= start:
                found.append(current)
            index += 1
            if repeat == 'quotidien':
                current = first + timedelta(days=index)
            elif repeat == 'hebdomadaire':
                current = first + timedelta(weeks=index)
            else:
                months = first.month - 1 + index * (12 if repeat == 'annuel' else 1)
                year, month = first.year + months // 12, months % 12 + 1
                current = first.replace(year=year, month=month, day=min(first.day, calendar.monthrange(year, month)[1]))
        return found

    def events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Rendez-vous entre deux instants, répétitions comprises, dans l'ordre."""
        found = []
        for event in self._events():
            lower = start if not event['journee'] else datetime.combine(start.date(), clock(0, 0))
            for moment in self._occurrences(event, lower, end):
                found.append({**event, 'quand': moment.isoformat(timespec='minutes')})
        return sorted(found, key=lambda e: (e['quand'], e['id']))

    # -- choses à faire ---------------------------------------------------------------------
    def add_todo(self, text: str, due: date | None = None, origin: str = 'utilisateur') -> int:
        text = tidy(text, TEXT_LIMIT)
        if not text:
            raise ValueError('texte vide')
        with self._lock, self.db:
            return int(self.db.execute(
                'INSERT INTO taches(texte, echeance, origine, cree_le) VALUES (?,?,?,?)',
                (text, due.isoformat() if due else None, tidy(origin, 40),
                 datetime.now().isoformat(timespec='seconds'))).lastrowid)

    def todos(self, include_done: bool = False) -> list[dict[str, Any]]:
        keys = ['id', 'texte', 'echeance', 'faite', 'faite_le', 'origine']
        sql = f"SELECT {', '.join(keys)} FROM taches" + ('' if include_done else ' WHERE faite=0')
        with self._lock:
            rows = self.db.execute(sql + ' ORDER BY faite, echeance IS NULL, echeance, id').fetchall()
        return [dict(zip(keys, row)) for row in rows]

    def set_done(self, todo_id: int, done: bool = True) -> bool:
        with self._lock, self.db:
            return self.db.execute('UPDATE taches SET faite=?, faite_le=? WHERE id=?',
                                   (int(done), datetime.now().isoformat(timespec='minutes') if done else None,
                                    todo_id)).rowcount > 0

    def delete_todo(self, todo_id: int) -> bool:
        with self._lock, self.db:
            return self.db.execute('DELETE FROM taches WHERE id=?', (todo_id,)).rowcount > 0

    def find_todo(self, description: str) -> dict[str, Any] | None:
        """La chose à faire (non faite) que ces mots désignent clairement, ou None."""
        wanted = {w for w in re.findall(r'[a-z0-9]{3,}', plain(description))}
        best, best_score, tie = None, 0.0, False
        for todo in self.todos():
            words = set(re.findall(r'[a-z0-9]{3,}', plain(todo['texte'])))
            if not words or not wanted:
                continue
            score = len(words & wanted) / len(words | wanted)
            if score > best_score:
                best, best_score, tie = todo, score, False
            elif score == best_score and score > 0:
                tie = True
        return best if best_score >= 0.5 and not tie else None

    # -- rappels --------------------------------------------------------------------------
    def due_reminders(self, now: datetime) -> list[dict[str, str]]:
        """Rappels à envoyer maintenant : {'key', 'level', 'title', 'text'} ; la clé évite les doublons."""
        due = []
        for event in self.events(now - STALE, now + timedelta(days=8)):
            moment = datetime.fromisoformat(event['quand'])
            if event['journee']:
                at = datetime.combine(moment.date(), ALL_DAY_REMINDER)
                expires = datetime.combine(moment.date(), clock(23, 59))
                title = f"Aujourd'hui : {event['titre']}"
            elif event['rappel'] is None:
                continue
            else:
                at, expires = moment - timedelta(minutes=event['rappel']), moment + STALE
                wait = int((moment - now).total_seconds() // 60)
                lead = ('Maintenant' if wait <= 0 else f'Dans {wait} min' if wait < 90
                        else f'Dans {round(wait / 60)} h')
                title = f"{lead} : {event['titre']} ({french_time(moment)})"
            if not at <= now <= expires:
                continue
            details = [f"{french_date(moment.date(), now.date()).capitalize()}"
                       + ('' if event['journee'] else f' à {french_time(moment)}')]
            details += [f"Lieu : {event['lieu']}"] if event['lieu'] else []
            details += [event['notes']] if event['notes'] else []
            due.append({'key': f"agenda:evenement:{event['id']}:{event['quand']}", 'level': 'warning',
                        'title': title, 'text': '\n'.join(details)})
        if now.time() >= ALL_DAY_REMINDER:
            today = now.date().isoformat()
            for todo in self.todos():
                if todo['echeance'] == today:
                    due.append({'key': f"agenda:tache:{todo['id']}:{today}", 'level': 'info',
                                'title': f"À faire aujourd'hui : {todo['texte']}", 'text': ''})
        return due

    # -- vue d'ensemble -------------------------------------------------------------------
    def overview(self, now: datetime, days: int = 7) -> str:
        """Texte lisible de ce qui vient : pour l'IA, la ligne de commande et les résumés."""
        end = datetime.combine(now.date() + timedelta(days=days), clock(0, 0))
        events = self.events(now - timedelta(hours=1), end)
        todos = self.todos()
        lines = [f"Nous sommes {french_date(now.date(), now.date() - timedelta(days=9))} {now.year}, il est {french_time(now)}."]
        lines.append(f'Rendez-vous des {days} prochains jours :' if events else f'Aucun rendez-vous dans les {days} prochains jours.')
        for event in events:
            moment = datetime.fromisoformat(event['quand'])
            line = f"- {french_date(moment.date(), now.date())}"
            line += ' (journée)' if event['journee'] else f' à {french_time(moment)}'
            line += f" : {event['titre']}"
            line += f" — {event['lieu']}" if event['lieu'] else ''
            line += f" ({REPEATS[event['repetition']]})" if event['repetition'] else ''
            lines.append(line)
        lines.append('Choses à faire :' if todos else 'Liste de choses à faire : vide.')
        for todo in todos:
            line = f"- {todo['texte']}"
            if todo['echeance']:
                due = date.fromisoformat(todo['echeance'])
                line += f" (pour {french_date(due, now.date())}" + (', EN RETARD)' if due < now.date() else ')')
            lines.append(line)
        return '\n'.join(lines)


# -- exécution des demandes ----------------------------------------------------------------
def apply_command(agenda: Agenda, message: str, now: datetime) -> str | None:
    """Exécute une demande explicite d'écriture (« rappelle-moi… », « ajoute… à ma liste »,
    « raye… ») et renvoie la confirmation ; None si le message n'en est pas une."""
    command = parse_command(message, now)
    if command is None:
        return None
    if command.kind == 'done':
        todo = agenda.find_todo(command.title)
        if todo is None:
            return None           # rien d'évident dans la liste : c'était une phrase ordinaire
        agenda.set_done(todo['id'])
        left = len(agenda.todos())
        return f"C'est rayé : **{todo['texte']}**. " + (
            f"Il reste {left} chose{'s' if left > 1 else ''} à faire." if left else 'Ta liste est vide.')
    if not command.title:
        return 'Je n\'ai pas compris quoi noter. Par exemple : « rappelle-moi d\'appeler le plombier demain à 9h ».'
    when = command.when
    if command.kind == 'todo' or when is None:
        due = when.start.date() if when is not None else None
        agenda.add_todo(command.title, due, origin='discussion')
        text = f'Ajouté à ta liste : **{command.title}**' + (f' (pour {french_date(due, now.date())})' if due else '') + '.'
        if command.kind == 'event':
            text += (' Je n\'ai pas vu de date : pour un rappel daté, dis par exemple '
                     '« rappelle-moi … demain à 9h ».')
        return text
    if not when.all_day and not when.repeat and when.start < now:
        return (f"{french_date(when.start.date(), now.date()).capitalize()} à {french_time(when.start)}, "
                "c'est déjà passé : je n'ai rien noté. Précise le jour, par exemple « demain à 9h ».")
    remind = command.remind
    if not when.all_day and not when.repeat and when.start - timedelta(minutes=remind) <= now:
        remind = 0          # trop proche pour prévenir d'avance : rappel à l'heure dite
    agenda.add_event(command.title, when.start, all_day=when.all_day, repeat=when.repeat, remind=remind,
                     origin='discussion')
    moment = french_date(when.start.date(), now.date())
    if when.repeat:
        moment = f'{REPEATS[when.repeat]}, à partir de {moment}'
    if when.all_day:
        return f'Noté dans ton agenda : **{command.title}**, {moment} (rappel le matin à 8h).'
    return (f'Noté dans ton agenda : **{command.title}**, {moment} à {french_time(when.start)} '
            + ('(rappel à l\'heure dite).' if remind == 0 else f'(rappel {remind} min avant).'))


def quick_add(agenda: Agenda, text: str, now: datetime) -> str:
    """Ajout rapide sans formule : « Dentiste demain à 14h » devient un rappel daté,
    « acheter du pain » une chose à faire."""
    text = ' '.join(text.split())[:400]
    message = apply_command(agenda, text, now)
    if message is None:
        when = parse_when(text, now)
        # « … avant vendredi », « … d'ici le 12 » : une échéance, donc une chose à faire, pas un rendez-vous.
        deadline = when is not None and when.all_day and not when.repeat and _DEADLINE_RE.search(plain(text))
        message = apply_command(agenda, f'Ajoute un événement : {text}' if when is not None and not deadline
                                else f'À faire : {text}', now)
    return message or 'Je n\'ai pas compris.'
