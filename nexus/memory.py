"""Historique des conversations, dans un fichier SQLite local.

`secure_delete` est activé : une conversation effacée est réellement
écrasée dans le fichier, pas seulement marquée comme supprimée.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import threading

ROLES = ('user', 'assistant')
MAX_MESSAGE = 60_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation, id);
        ''')
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> 'Store':
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def new_conversation(self, title: str = 'Nouvelle conversation') -> int:
        with self._lock, self.db:
            cur = self.db.execute('INSERT INTO conversations(title, created_at, updated_at) VALUES (?,?,?)',
                                  (title[:80], now(), now()))
            return int(cur.lastrowid)

    def add(self, conversation: int, role: str, content: str, model: str) -> None:
        if role not in ROLES:
            raise ValueError('rôle invalide')
        content = content[:MAX_MESSAGE]
        with self._lock, self.db:
            exists = self.db.execute('SELECT title FROM conversations WHERE id=?', (conversation,)).fetchone()
            if exists is None:
                raise ValueError('conversation inconnue')
            self.db.execute('INSERT INTO messages(conversation, role, content, model, created_at) VALUES (?,?,?,?,?)',
                            (conversation, role, content, model, now()))
            title = exists[0]
            if role == 'user' and title == 'Nouvelle conversation':
                title = ' '.join(content.split())[:60] or title
            self.db.execute('UPDATE conversations SET title=?, updated_at=? WHERE id=?',
                            (title, now(), conversation))

    def messages(self, conversation: int, limit: int = 200) -> list[dict[str, str]]:
        with self._lock:
            rows = self.db.execute('''SELECT role, content FROM messages WHERE conversation=?
                                      ORDER BY id DESC LIMIT ?''', (conversation, limit)).fetchall()
        return [{'role': r, 'content': c} for r, c in reversed(rows)]

    def conversations(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute('''SELECT c.id, c.title, c.updated_at, COUNT(m.id)
                FROM conversations c LEFT JOIN messages m ON m.conversation = c.id
                GROUP BY c.id ORDER BY c.updated_at DESC, c.id DESC''').fetchall()
        return [{'id': i, 'title': t, 'updated_at': u, 'messages': n} for i, t, u, n in rows]

    def delete(self, conversation: int) -> bool:
        with self._lock, self.db:
            return self.db.execute('DELETE FROM conversations WHERE id=?', (conversation,)).rowcount > 0

    def delete_all(self) -> int:
        with self._lock:
            with self.db:
                count = self.db.execute('DELETE FROM conversations').rowcount
            self.db.execute('VACUUM')
        return count
