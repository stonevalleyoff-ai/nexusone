"""NEXUS agit seul, dans un périmètre fermé.

Jusqu'ici NEXUS proposait et attendait. Ici il peut faire, à trois conditions qui ne dépendent
pas de la confiance qu'on accorde au modèle :

1. **Une liste fermée de gestes.** Ranger un document reçu, noter un rendez-vous ou une
   échéance trouvés dans un document. Chaque geste est du code vérifié ; le modèle ne
   fournit que le classement du document, jamais une commande.
2. **Tout est annulable et journalisé.** Chaque geste est inscrit avec de quoi le défaire.
   Rien n'est supprimé. Un geste annulé n'est jamais refait tout seul.
3. **Un niveau par geste, réglé par la personne** : demander, faire et prévenir, faire en
   silence, jamais. En cas de doute (classement incertain) ou au-delà d'un nombre de gestes
   par jour, NEXUS repasse de lui-même à « demander ».

Les dates sont relevées par des règles strictes (date complète, sur une ligne qui parle d'un
rendez-vous ou d'une échéance). Un document est une donnée : ce qui y est écrit n'est jamais
pris pour une consigne.
"""
from __future__ import annotations

from datetime import date, datetime, time as clock, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from .agenda import MONTHS, french_date, french_time, plain, tidy
from .library import CATEGORIES, INBOX, money

LEVELS = {'demander': 'demander avant', 'prevenir': 'faire et prévenir', 'silence': 'faire en silence',
          'jamais': 'ne jamais le faire'}
GESTURES = {
    'ranger': 'Ranger un document reçu',
    'rendez-vous': 'Noter dans l\'agenda un rendez-vous trouvé dans un document',
    'tache': 'Ajouter à la liste une échéance trouvée dans un document',
    'rendez-vous-courriel': 'Noter dans l\'agenda un rendez-vous trouvé dans un courriel',
    'tache-courriel': 'Ajouter à la liste une échéance trouvée dans un courriel',
}
DEFAULT_LEVEL = 'prevenir'
# Un courriel peut venir de n'importe qui (fausse facture, faux rendez-vous) : par défaut NEXUS
# demande, là où il agit seul pour les documents que la personne a elle-même envoyés.
DEFAULT_LEVELS = {'rendez-vous-courriel': 'demander', 'tache-courriel': 'demander'}
DAILY_LIMIT = 40                   # gestes faits seuls par jour ; au-delà, NEXUS demande
HORIZON_DAYS = 730                 # une date à plus de deux ans n'est pas notée

_MONTHS = '|'.join(plain(m) for m in MONTHS)
_DATE = rf'(?:(\d{{1,2}})/(\d{{1,2}})/(\d{{4}})|(\d{{1,2}})(?:er)?\s+({_MONTHS})\s+(\d{{4}}))'
_TIME = r'(?:\s*(?:a|,|-)?\s*(\d{1,2})\s*(?:h|:)\s*(\d{2})?)?'
_MEETING_RE = re.compile(rf'\b(rendez vous|rdv|convocation|consultation|controle|visite|entretien|reunion|audience|'
                         rf'intervention|livraison|passage)\b[^\n]{{0,60}}?{_DATE}{_TIME}')
_DUE_RE = re.compile(rf'\b(a payer|a regler|date limite|echeance|avant le|au plus tard)\b[^\n]{{0,40}}?{_DATE}')


# « Votre rendez-vous de contrôle est fixé le » → « Rendez-vous de contrôle »
_TAIL_RE = re.compile(r"(?:\s+(?:le|du|au|avant|aura lieu|est|sera|a été|fixée?|prévue?|programmée?|planifiée?|confirmée?))+$", re.I)
_HEAD_RE = re.compile(r"^(?:votre|vos|notre|nos|le|la|les|un|une)\s+", re.I)


def _date_of(match: re.Match, first: int) -> date | None:
    try:
        if match.group(first):
            return date(int(match.group(first + 2)), int(match.group(first + 1)), int(match.group(first)))
        month = [plain(m) for m in MONTHS].index(match.group(first + 4)) + 1
        return date(int(match.group(first + 5)), month, int(match.group(first + 3)))
    except (ValueError, TypeError):
        return None


def find_dates(text: str, today: date) -> list[dict[str, Any]]:
    """Rendez-vous et échéances à venir écrits dans un document (règles strictes, rien de deviné)."""
    found, seen = [], set()
    for raw in text.splitlines():
        line = ' '.join(raw.split())
        low = plain(line)
        for kind, pattern in (('rendez-vous', _MEETING_RE), ('tache', _DUE_RE)):
            match = pattern.search(low)
            if not match:
                continue
            day = _date_of(match, 2)
            if day is None or not today <= day <= today + timedelta(days=HORIZON_DAYS):
                continue
            moment = None
            if kind == 'rendez-vous' and match.group(8):
                hour, minute = int(match.group(8)), int(match.group(9) or 0)
                if hour <= 23 and minute <= 59:
                    moment = clock(hour, minute)
            key = (kind, day, moment)
            if key in seen:
                continue
            seen.add(key)
            date_at = match.start(2) if match.group(2) else match.start(5)
            label = _TAIL_RE.sub('', line[:date_at].strip(' :,-–—'))
            label = tidy(_HEAD_RE.sub('', label), 80)
            label = label[:1].upper() + label[1:]
            found.append({'kind': kind, 'date': day, 'time': moment, 'label': label.strip(' :,-–—') or line[:80]})
            break
    return found


class Autonomy:
    def __init__(self, path: Path | str, levels_path: Path | None, *, library: Any = None, agenda: Any = None,
                 planner: Any = None, daily_limit: int = DAILY_LIMIT):
        self.library, self.agenda, self.planner = library, agenda, planner
        self.levels_path, self.daily_limit = levels_path, daily_limit
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        if str(path) != ':memory:':
            self.db.execute('PRAGMA journal_mode=WAL')
        # cle : un même geste sur un même document n'existe qu'une fois, pour toujours. C'est ce
        # qui empêche NEXUS de refaire ce que la personne a annulé.
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS actes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cle TEXT UNIQUE NOT NULL, quand TEXT NOT NULL,
                geste TEXT NOT NULL, resume TEXT NOT NULL, detail TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
                etat TEXT NOT NULL);
        ''')   # etat : 'fait', 'propose', 'annule', 'refuse'
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> 'Autonomy':
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- niveaux ------------------------------------------------------------------------
    def levels(self) -> dict[str, str]:
        stored: dict[str, Any] = {}
        if self.levels_path is not None:
            try:
                data = json.loads(self.levels_path.read_text(encoding='utf-8'))
                stored = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                stored = {}
        return {g: stored[g] if stored.get(g) in LEVELS else DEFAULT_LEVELS.get(g, DEFAULT_LEVEL) for g in GESTURES}

    def set_level(self, gesture: str, level: str) -> None:
        if gesture not in GESTURES or level not in LEVELS:
            raise ValueError('geste ou niveau inconnu')
        if self.levels_path is None:
            raise ValueError('réglages non modifiables ici')
        levels = {**self.levels(), gesture: level}
        self.levels_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.levels_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(levels, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temporary, self.levels_path)

    # -- journal ------------------------------------------------------------------------
    def _row(self, row: tuple) -> dict[str, Any]:
        keys = ['id', 'cle', 'quand', 'geste', 'resume', 'detail', 'source', 'etat']
        act = dict(zip(keys, row))
        act['detail'] = json.loads(act['detail'])
        return act

    def acts(self, limit: int = 60, since: datetime | None = None) -> list[dict[str, Any]]:
        sql, params = 'SELECT * FROM actes', []
        if since is not None:
            sql += ' WHERE quand >= ?'
            params.append(since.isoformat(timespec='minutes'))
        with self._lock:
            rows = self.db.execute(sql + ' ORDER BY id DESC LIMIT ?', (*params, limit)).fetchall()
        return [self._row(row) for row in rows]

    def get(self, act_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute('SELECT * FROM actes WHERE id=?', (act_id,)).fetchone()
        return self._row(row) if row else None

    def _done_today(self, now: datetime) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM actes WHERE etat='fait' AND substr(quand, 1, 10)=?",
                                   (now.date().isoformat(),)).fetchone()[0]

    def _record(self, key: str, gesture: str, summary: str, detail: dict, source: str, state: str,
                now: datetime) -> int | None:
        """Inscrit le geste ; None s'il a déjà été inscrit un jour (fait, annulé ou refusé)."""
        with self._lock, self.db:
            cursor = self.db.execute(
                'INSERT OR IGNORE INTO actes(cle, quand, geste, resume, detail, source, etat) VALUES (?,?,?,?,?,?,?)',
                (key, now.isoformat(timespec='minutes'), gesture, summary[:300], json.dumps(detail, ensure_ascii=False),
                 source[:300], state))
            return cursor.lastrowid if cursor.rowcount else None

    def _set(self, act_id: int, state: str, detail: dict | None = None, summary: str | None = None) -> None:
        with self._lock, self.db:
            self.db.execute('UPDATE actes SET etat=? WHERE id=?', (state, act_id))
            if detail is not None:
                self.db.execute('UPDATE actes SET detail=? WHERE id=?', (json.dumps(detail, ensure_ascii=False), act_id))
            if summary is not None:
                self.db.execute('UPDATE actes SET resume=? WHERE id=?', (summary[:300], act_id))

    # -- les gestes (du code, vérifié) --------------------------------------------------
    def _do(self, gesture: str, detail: dict) -> tuple[dict, str] | None:
        """Exécute un geste ; renvoie (de quoi l'annuler, phrase de compte rendu) ou None s'il échoue."""
        gesture = gesture.removesuffix('-courriel')
        origin = 'courriel' if 'courriel' in detail else 'document'
        if gesture == 'ranger':
            moved = self.library.apply([detail['doc']])
            if not moved:
                return None
            return ({**detail, 'lot': self.library.last_lot, 'de': moved[0][0], 'vers': moved[0][1]},
                    f'Rangé : {detail["titre"]} → {moved[0][1]}')
        if gesture == 'rendez-vous':
            start = datetime.fromisoformat(detail['debut'])
            event = self.agenda.add_event(detail['titre'], start, all_day=detail['journee'], origin=origin,
                                          notes=f"D'après : {detail['document']}")
            when = french_date(start.date()) + ('' if detail['journee'] else f' à {french_time(start)}')
            return {**detail, 'evenement': event}, f'Noté dans l\'agenda : {detail["titre"]}, {when}'
        if gesture == 'tache':
            due = date.fromisoformat(detail['echeance'])
            todo = self.agenda.add_todo(detail['titre'], due, origin=origin)
            return {**detail, 'tache': todo}, f'Ajouté à la liste : {detail["titre"]} (pour {french_date(due)})'
        return None

    def undo(self, act_id: int) -> str:
        """Défait un geste fait par NEXUS. Il ne le refera pas."""
        act = self.get(act_id)
        if act is None or act['etat'] != 'fait':
            return 'Rien à annuler pour cette ligne.'
        detail = act['detail']
        if act['geste'] == 'ranger':
            undone = self.library.undo(detail.get('lot'))
            if not undone:
                self._set(act_id, 'annule')
                return 'Impossible de remettre ce fichier à sa place (il a été déplacé ou supprimé depuis).'
            message = f'Remis à sa place : {undone[0][1]}'
        elif act['geste'].startswith('rendez-vous'):
            self.agenda.delete_event(detail.get('evenement', -1))
            message = f'Retiré de l\'agenda : {detail["titre"]}'
        else:
            self.agenda.delete_todo(detail.get('tache', -1))
            message = f'Retiré de la liste : {detail["titre"]}'
        self._set(act_id, 'annule')
        return message

    def confirm(self, act_id: int) -> str:
        """« Oui, fais-le » : exécute une proposition en attente."""
        act = self.get(act_id)
        if act is None or act['etat'] != 'propose':
            return 'Cette proposition n\'est plus en attente.'
        done = self._do(act['geste'], act['detail'])
        if done is None:
            self._set(act_id, 'refuse')
            return 'Ce geste n\'est plus possible (document déplacé ou déjà rangé).'
        self._set(act_id, 'fait', done[0], done[1])
        return done[1]

    def dismiss(self, act_id: int) -> bool:
        act = self.get(act_id)
        if act is None or act['etat'] != 'propose':
            return False
        self._set(act_id, 'refuse')
        return True

    def prune(self) -> None:
        """Écarte les propositions de rangement devenues sans objet (document rangé ou retiré entre-temps)."""
        for act in self.acts(200):
            if act['etat'] == 'propose' and act['geste'] == 'ranger':
                doc = self.library.document(act['detail'].get('doc', -1)) if self.library is not None else None
                if doc is None or not doc['proposal'] or doc['proposal'] == doc['path']:
                    self._set(act['id'], 'refuse')

    # -- déclenchement ------------------------------------------------------------------
    def _candidates(self, doc: dict[str, Any], today: date) -> list[tuple[str, str, dict, str, bool]]:
        """(clé, geste, détail, phrase de proposition, sûr ?) pour un document reçu."""
        sha, found = doc['sha256'], []
        title = doc['title'] or Path(doc['path']).stem
        if doc['proposal'] and doc['proposal'] != doc['path'] and doc['path'].startswith(INBOX + '/'):
            # Classement incertain : catégorie « Autres », ou ni date ni émetteur reconnus.
            sure = bool(doc['category']) and doc['category'] != 'autre' and bool(doc['doc_date'] or doc['issuer'])
            found.append((f'ranger:{sha}', 'ranger', {'doc': doc['id'], 'titre': title},
                          f'Ranger « {title} » dans {doc["proposal"]}', sure))
        text = self.library.text_of(doc['path'])
        for item in find_dates(text[0] if text else '', today):
            who = doc['issuer'] or title
            if item['kind'] == 'rendez-vous':
                start = datetime.combine(item['date'], item['time'] or clock(0, 0))
                name = tidy(f"{item['label']} ({who})" if who.lower() not in item['label'].lower() else item['label'], 120)
                detail = {'titre': name, 'debut': start.isoformat(timespec='minutes'), 'journee': item['time'] is None,
                          'document': doc['path']}
                when = french_date(item['date'], today) + (f" à {french_time(start)}" if item['time'] else '')
                found.append((f"rendez-vous:{sha}:{start.isoformat(timespec='minutes')}", 'rendez-vous', detail,
                              f'Noter dans l\'agenda : {name}, {when}', True))
            else:
                amount = f" ({money(doc['amount_cents'])})" if doc.get('amount_cents') else ''
                name = tidy(f"{item['label'].capitalize()} : {title}{amount}", 200)
                detail = {'titre': name, 'echeance': item['date'].isoformat(), 'document': doc['path']}
                found.append((f"tache:{sha}:{item['date'].isoformat()}", 'tache', detail,
                              f'Ajouter à la liste : {name}, pour {french_date(item["date"], today)}', True))
        return found

    def handle_document(self, doc_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
        """Ce que NEXUS fait (ou propose) de lui-même pour un document reçu. Une seule fois par document."""
        now = now or datetime.now()
        doc = self.library.document(doc_id) if self.library is not None else None
        if doc is None or doc['status'] != 'indexé':
            return []
        title = doc['title'] or Path(doc['path']).stem
        category = CATEGORIES.get(doc['category'] or '', '')
        return self._process(self._candidates(doc, now.date()), doc['path'], now,
                             f'Document reçu : {title}' + (f' ({category})' if category else ''))

    def _process(self, candidates: list[tuple[str, str, dict, str, bool]], source: str, now: datetime,
                 headline: str) -> list[dict[str, Any]]:
        """Fait, propose ou ignore chaque geste candidat selon son niveau ; une seule notification pour le tout."""
        levels, results, lines = self.levels(), [], []
        # Les dates d'abord : elles lisent le document à son emplacement actuel, avant le rangement.
        for key, gesture, detail, proposal, sure in sorted(candidates, key=lambda c: c[1] == 'ranger'):
            level = levels[gesture]
            if level == 'jamais' or (gesture != 'ranger' and self.agenda is None):
                continue
            acts_alone = level in ('prevenir', 'silence') and sure and self._done_today(now) < self.daily_limit
            act_id = self._record(key, gesture, proposal, detail, source, 'propose', now)
            if act_id is None:
                continue                      # déjà fait, proposé, annulé ou refusé un jour : on n'y revient pas
            if not acts_alone:
                results.append({'id': act_id, 'geste': gesture, 'etat': 'propose', 'resume': proposal})
                lines.append('• À valider : ' + proposal)
                continue
            done = self._do(gesture, detail)
            if done is None:
                self._set(act_id, 'refuse')
                continue
            self._set(act_id, 'fait', done[0], done[1])
            results.append({'id': act_id, 'geste': gesture, 'etat': 'fait', 'resume': done[1]})
            if level == 'prevenir':
                lines.append('• ' + done[1])
        if lines and self.planner is not None:
            self.planner.notify('info', headline, '\n'.join(lines) + '\nPour répondre ou défaire : onglet Maison, '
                                '« Activité de NEXUS ».', 'autonomie')
        return results

    def handle_mail(self, message: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
        """Rendez-vous et échéances trouvés dans un courriel important : proposés (par défaut), jamais imposés."""
        now = now or datetime.now()
        candidates = []
        sender = message['de'] or message['adresse']
        source = f"courriel de {sender} : {message['objet']}"
        for item in find_dates(f"{message['objet']}\n{message['texte']}", now.date()):
            name = tidy(f"{item['label']} ({sender})", 120)
            if item['kind'] == 'rendez-vous':
                start = datetime.combine(item['date'], item['time'] or clock(0, 0))
                detail = {'titre': name, 'debut': start.isoformat(timespec='minutes'), 'journee': item['time'] is None,
                          'document': source, 'courriel': message['cle']}
                when = french_date(item['date'], now.date()) + (f" à {french_time(start)}" if item['time'] else '')
                candidates.append((f"courriel:{message['cle']}:{start.isoformat(timespec='minutes')}", 'rendez-vous-courriel',
                                   detail, f'Noter dans l\'agenda : {name}, {when} — d\'après un courriel', True))
            else:
                detail = {'titre': name, 'echeance': item['date'].isoformat(), 'document': source, 'courriel': message['cle']}
                candidates.append((f"courriel:{message['cle']}:tache:{item['date'].isoformat()}", 'tache-courriel', detail,
                                   f'Ajouter à la liste : {name}, pour {french_date(item["date"], now.date())} — d\'après un courriel',
                                   True))
        return self._process(candidates, source, now, f'Courriel de {sender} : {message["objet"]}'[:120])

    def tour(self, now: datetime | None = None, classify: bool = True) -> list[dict[str, Any]]:
        """Tournée : regarde le dossier d'arrivée et s'occupe de ce qui n'a pas encore été traité."""
        if self.library is None:
            return []
        results = []
        for doc_id in self.library.scan_folder(INBOX):
            doc = self.library.document(doc_id)
            if doc is None or doc['status'] != 'indexé':
                continue
            if classify and not doc['category']:
                try:
                    self.library.classify(doc_id)
                except Exception:      # moteur absent : on réessaiera à la prochaine tournée
                    continue
            results += self.handle_document(doc_id, now)
        return results

    # -- compte rendu -------------------------------------------------------------------
    def report(self, since: datetime) -> str:
        """Ce que NEXUS a fait seul depuis `since`, et ce qui attend une réponse."""
        acts = list(reversed(self.acts(200, since)))
        done = [a for a in acts if a['etat'] == 'fait']
        waiting = [a for a in self.acts(200) if a['etat'] == 'propose']
        lines = [f'NEXUS a fait {len(done)} geste{"s" if len(done) > 1 else ""} de lui-même :' if done
                 else 'NEXUS n\'a rien fait de lui-même.']
        lines += [f"- {a['resume']}" for a in done]
        if waiting:
            lines.append(f'{len(waiting)} proposition{"s" if len(waiting) > 1 else ""} en attente de ta réponse :')
            lines += [f"- {a['resume']}" for a in waiting]
        return '\n'.join(lines)
