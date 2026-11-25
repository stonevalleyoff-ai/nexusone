"""Qui a le droit de parler au serveur NEXUS : les appareils appairés.

Principe : on ne crée ni compte ni mot de passe. Depuis le PC, `nexus serveur appairer`
affiche un code à usage unique, valable dix minutes. Le téléphone le saisit une fois ; il
reçoit en échange une clé longue et aléatoire, qu'il garde. NEXUS n'en conserve que
l'empreinte : lire sa base ne permet pas de se faire passer pour un appareil. Chaque
appareil se révoque séparément (`nexus serveur revoquer N`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading

CODE_MINUTES = 10
MAX_FAILURES = 5          # essais ratés avant d'annuler les codes en cours
MAX_DEVICES = 20
NAME_LIMIT = 40


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def tidy_name(name: object) -> str:
    text = ' '.join(str(name or '').split())
    text = ''.join(ch for ch in text if ch.isprintable())[:NAME_LIMIT].strip()
    return text or 'Appareil'


class Access:
    def __init__(self, path: Path | str):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        if str(path) != ':memory:':
            self.db.execute('PRAGMA journal_mode=WAL')   # le serveur et la ligne de commande la partagent
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS appareils (
                id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT NOT NULL, empreinte TEXT UNIQUE NOT NULL,
                cree_le TEXT NOT NULL, vu_le TEXT);
            CREATE TABLE IF NOT EXISTS codes (
                empreinte TEXT PRIMARY KEY, expire_le TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS echecs (quand TEXT NOT NULL);
        ''')
        self.db.commit()
        if str(path) != ':memory:':
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> 'Access':
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- appairage -------------------------------------------------------------------
    def new_code(self, now: datetime | None = None) -> str:
        """Un code à usage unique, à lire sur l'écran du PC : « 4827-1936 »."""
        now = now or _now()
        digits = f'{secrets.randbelow(10**8):08d}'
        with self._lock, self.db:
            self.db.execute('DELETE FROM codes WHERE expire_le < ?', (now.isoformat(),))
            self.db.execute('INSERT OR REPLACE INTO codes VALUES (?,?)',
                            (_hash(digits), (now + timedelta(minutes=CODE_MINUTES)).isoformat()))
        return f'{digits[:4]}-{digits[4:]}'

    def pair(self, code: object, name: object, now: datetime | None = None) -> str | None:
        """Échange un code valide contre la clé de l'appareil ; None si le code est refusé."""
        now = now or _now()
        digits = ''.join(ch for ch in str(code or '') if ch.isdigit())
        with self._lock, self.db:
            self.db.execute('DELETE FROM codes WHERE expire_le < ?', (now.isoformat(),))
            self.db.execute('DELETE FROM echecs WHERE quand < ?', ((now - timedelta(minutes=CODE_MINUTES)).isoformat(),))
            known = len(digits) == 8 and self.db.execute(
                'DELETE FROM codes WHERE empreinte=?', (_hash(digits),)).rowcount > 0
            if not known:
                self.db.execute('INSERT INTO echecs VALUES (?)', (now.isoformat(),))
                if self.db.execute('SELECT COUNT(*) FROM echecs').fetchone()[0] >= MAX_FAILURES:
                    self.db.execute('DELETE FROM codes')    # quelqu'un devine : on repart d'un nouveau code
                return None
            if self.db.execute('SELECT COUNT(*) FROM appareils').fetchone()[0] >= MAX_DEVICES:
                return None
            token = secrets.token_urlsafe(32)
            self.db.execute('INSERT INTO appareils(nom, empreinte, cree_le) VALUES (?,?,?)',
                            (tidy_name(name), _hash(token), now.isoformat(timespec='seconds')))
            self.db.execute('DELETE FROM echecs')
            return token

    # -- contrôle --------------------------------------------------------------------
    def check(self, token: object, now: datetime | None = None) -> dict | None:
        """L'appareil qui présente cette clé, ou None."""
        if not isinstance(token, str) or not 20 <= len(token) <= 100:
            return None
        now = now or _now()
        with self._lock:
            row = self.db.execute('SELECT id, nom, vu_le FROM appareils WHERE empreinte=?',
                                  (_hash(token),)).fetchone()
            if row is None:
                return None
            stamp = now.isoformat(timespec='minutes')
            if row[2] != stamp:     # au plus une écriture par minute
                with self.db:
                    self.db.execute('UPDATE appareils SET vu_le=? WHERE id=?', (stamp, row[0]))
        return {'id': row[0], 'nom': row[1]}

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute('SELECT id, nom, cree_le, vu_le FROM appareils ORDER BY id').fetchall()
        return [{'id': i, 'nom': n, 'cree_le': c, 'vu_le': v} for i, n, c, v in rows]

    def revoke(self, device_id: int) -> bool:
        with self._lock, self.db:
            return self.db.execute('DELETE FROM appareils WHERE id=?', (device_id,)).rowcount > 0
