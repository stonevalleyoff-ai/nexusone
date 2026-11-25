"""Mémoire à long terme : ce que NEXUS sait de l'utilisateur, sous son contrôle.

Règles :
- un souvenir ne vient QUE de l'utilisateur : « retiens que… » l'enregistre ; une phrase
  où il parle de lui devient une PROPOSITION qu'il valide. Jamais d'un document, d'une
  page lue ou d'un résultat d'outil (un PDF piégé ne peut donc rien « faire croire ») ;
- les secrets (mots de passe, codes, numéros de carte, IBAN) sont refusés ;
- seuls les souvenirs pertinents pour la question sont donnés au modèle (seuil mesuré) ;
- tout se lit, se corrige et s'efface ; un effacement est réel (secure_delete).
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from .library import to_blob
from .ollama import OllamaClient, OllamaError
from .router import normalize

CATEGORIES = {
    'preference': 'Préférences', 'sante': 'Santé', 'maison': 'Maison et équipements', 'habitude': 'Habitudes',
    'personne': 'Personnes', 'decision': 'Décisions', 'autre': 'Autres',
}
MAX_TEXT = 300
MAX_SOUVENIRS = 2000
RECALL_K = 4
MAX_PREFERENCES = 3  # préférences de style : toujours appliquées, quelle que soit la question
# Seuil de similarité (cosinus) : en dessous, un souvenir n'est pas jugé lié à la question.
# Mesuré sur le banc d'essai (tests/bench_souvenirs.py) avec bge-m3 : à 0,55, 14 bons souvenirs
# rappelés sur 15 et aucun faux rappel sur 12 questions sans rapport.
RECALL_THRESHOLD = 0.55
DUPLICATE_THRESHOLD = 0.92

# Commandes reconnues, volontairement étroites : « retiens que … », « oublie que … »,
# « oublie le souvenir sur … ». Une phrase comme « oublie de me répondre en anglais » ou
# « note : le serveur plante, pourquoi ? » reste une question ordinaire pour le modèle.
REMEMBER_RE = re.compile(
    r"^\s*(?:nexus\s*[,:]?\s*)?(?:retiens|souviens[- ]toi|rappelle[- ]toi|n'oublie pas|note|m[ée]morise)"
    r"\s+(?:bien\s+)?(?:que\b|qu')\s*(.+)$", re.I | re.S)
FORGET_RE = re.compile(
    r"^\s*(?:nexus\s*[,:]?\s*)?(?:oublie|efface|supprime)\s+(?:"
    r"(?:que\b|qu')|(?:le souvenir|ce que je t'ai dit)\s*(?:que\b|qu'|sur\b|à propos de\b|au sujet de\b|:))"
    r"\s*(.+)$", re.I | re.S)
# Phrases où l'utilisateur parle de lui : candidates à une proposition de souvenir.
SELF_RE = re.compile(
    r"\b(?:je|j')\s*(?:pr[ée]f[èe]re|n'aime pas|aime bien|adore|d[ée]teste|pars|rentre|travaille|"
    r"habite|vis|suis|ai une|ai un|ai des|commence|termine|charge|prends|me l[èe]ve|me couche)\b|"
    r"\b(?:mon|ma|mes)\s+\w+(?:\s+\w+)?\s+(?:s'appellent?|est|sont|a|ont)\b|"
    r"\b(?:appelle[- ]moi|tutoie[- ]moi|vouvoie[- ]moi|r[ée]ponds[- ]moi)\b", re.I)
# Filtre « au mieux » : il attrape les formulations courantes, pas toutes. Ne jamais
# confier un secret à NEXUS en comptant sur lui.
SECRET_RE = re.compile(
    r"mots? de passe|\bmdp\b|password|passcode|passphrase|\bcodes?\b|digicodes?|\bpin\b|cryptogramme|"
    r"identifiants?|\blogin\b|\bcl[ée]s?\s+(?:wi-?fi|priv[ée]e|secr[èe]te|api|d'activation|de chiffrement|"
    r"de r[ée]cup[ée]ration|bitlocker)|"
    r"\b(?:\d[ .\-]*){13,19}\b|"                               # numéro de carte, tous séparateurs
    r"\b[A-Za-z]{2}\d{2}(?:[ ]?[A-Za-z0-9]{4}){3,}\b|\biban\b", re.I)  # IBAN de tout pays
CATEGORY_HINTS = [
    ('sante', r"allergi|intol[ée]ran|diab[èe]t|asthm|enceinte|traitement|m[ée]dicament|r[ée]gime|maladie|v[ée]g[ée]tarien"),
    ('preference', r"pr[ée]f[èe]re|aime|adore|d[ée]teste|tutoie|vouvoie|appelle-moi|r[ée]ponds|r[ée]ponses|style"),
    ('personne', r"\b(?:mon|ma|mes)\s+(?:fils|fille|femme|mari|compagne?|conjoint|enfants?|m[èe]re|p[èe]re|"
                 r"fr[èe]re|s[œo]eur|chat|chien|voisin)|s'appellent?"),
    ('decision', r"on a d[ée]cid[ée]|j'ai d[ée]cid[ée]|je garde|j'ai choisi|on garde|plus jamais"),
    ('habitude', r"tous les|chaque|d'habitude|le matin|le soir|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|semaine|week-end|\b\d{1,2}\s?h\d{0,2}\b|pars|rentre|me l[èe]ve"),
    ('maison', r"maison|appartement|chauffage|radiateur|voiture|batterie|tempo|abonnement|linky|compteur|"
               r"chauffe-eau|kva|box|wifi|wi-fi"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def guess_category(text: str) -> str:
    for category, pattern in CATEGORY_HINTS:
        if re.search(pattern, text, re.I):
            return category
    return 'autre'


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode('utf-8')).hexdigest()


def tidy(text: str) -> str:
    text = re.sub(r'\s+', ' ', text).strip().strip('«»"\' ')
    return text[:1].upper() + text[1:] if text else text


def is_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text))


@dataclass(frozen=True)
class Command:
    kind: str   # 'remember' | 'forget'
    text: str


def parse_command(message: str) -> Command | None:
    """« retiens que … » / « oublie que … » en début de message."""
    for kind, regex in (('remember', REMEMBER_RE), ('forget', FORGET_RE)):
        match = regex.match(message.strip())
        if match and match.group(1).strip():
            return Command(kind, tidy(match.group(1)))
    return None


def candidates(message: str) -> list[str]:
    """Phrases où l'utilisateur parle de lui-même (hors questions) : à proposer, jamais imposer."""
    found = []
    for sentence in re.split(r'(?<=[.!?;\n])\s+', message.strip()):
        sentence = sentence.strip()
        if (len(sentence) < 8 or sentence.endswith('?') or not SELF_RE.search(sentence)
                or is_secret(sentence) or parse_command(sentence)):
            continue
        found.append(tidy(sentence.rstrip('.!;'))[:MAX_TEXT])
    return found[:2]


def _cosine(a: bytes, b: bytes) -> float:
    va, vb = array('f'), array('f')
    va.frombytes(a)
    vb.frombytes(b)
    return sum(x * y for x, y in zip(va, vb))  # vecteurs déjà normalisés


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r'[a-z0-9]{4,}', normalize(text))}


class Souvenirs:
    def __init__(self, path: Path | str, client: OllamaClient | None = None,
                 embed_model: str | None = 'bge-m3'):
        self.client, self.embed_model = client, embed_model
        self.degraded = False  # vrai si le modèle d'embedding n'a pas répondu (recherche par mots)
        self._lock = threading.RLock()
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS souvenirs (
                id INTEGER PRIMARY KEY, texte TEXT NOT NULL, categorie TEXT NOT NULL,
                statut TEXT NOT NULL CHECK (statut IN ('valide', 'propose')),
                origine TEXT NOT NULL, cree_le TEXT NOT NULL, utilise_le TEXT,
                utilisations INTEGER NOT NULL DEFAULT 0, modele TEXT, vecteur BLOB);
            -- Propositions refusées : on ne les repropose pas (empreinte SHA-256, jamais le texte).
            CREATE TABLE IF NOT EXISTS refus (empreinte TEXT PRIMARY KEY, le TEXT NOT NULL);
        ''')
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> 'Souvenirs':
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # -- vecteurs -------------------------------------------------------------------
    def _vector(self, text: str) -> bytes | None:
        if self.client is None or not self.embed_model:
            return None
        try:
            vector = to_blob(self.client.embed(self.embed_model, [text])[0])
        except OllamaError:
            self.degraded = True
            return None
        self.degraded = False
        return vector

    def _refresh_vectors(self) -> None:
        """Souvenirs enregistrés sans vecteur, ou avec un autre modèle : on les recalcule."""
        stale = self.db.execute('SELECT id, texte FROM souvenirs WHERE vecteur IS NULL OR modele IS NOT ?',
                                (self.embed_model,)).fetchall()
        for souvenir_id, text in stale:
            vector = self._vector(text)
            if vector is None:
                return
            with self._lock, self.db:
                self.db.execute('UPDATE souvenirs SET vecteur=?, modele=? WHERE id=?',
                                (vector, self.embed_model, souvenir_id))

    def _scored(self, query: str, statut: str | None = 'valide',
                purpose: str = 'recall') -> list[tuple[float, dict]]:
        """Souvenirs classés par proximité. Sans modèle de sens, on compare des mots : pour
        remplacer (`duplicate`) ou effacer (`forget`) un souvenir, la comparaison est alors
        beaucoup plus stricte, car un seul mot commun (« voiture ») ne prouve rien."""
        vector = self._vector(query)
        self._by_words = vector is None
        if vector is not None:
            self._refresh_vectors()
        rows = self.list(statut)
        scored = []
        for row in rows:
            if vector is not None and row['_vecteur'] is not None and row['_modele'] == self.embed_model:
                score = _cosine(vector, row['_vecteur'])
            else:  # sans modèle d'embedding : recouvrement de mots
                a, b = _words(query), _words(row['texte'])
                if not a or not b:
                    score = 0.0
                elif purpose == 'duplicate':     # presque les mêmes mots, dans les deux sens
                    score = len(a & b) / len(a | b)
                elif purpose == 'forget':        # toute la description se retrouve dans le souvenir
                    score = len(a & b) / len(a) if len(a) >= 2 else 0.0
                else:
                    score = len(a & b) / min(len(a), len(b))
            scored.append((score, row))
        return sorted(scored, key=lambda item: -item[0])

    # -- écriture -------------------------------------------------------------------
    def add(self, text: str, *, category: str | None = None, origin: str = 'utilisateur',
            status: str = 'valide') -> tuple[int | None, str]:
        """Enregistre (ou met à jour un doublon). Renvoie (id, 'ajouté'|'mis à jour'|'refusé : …')."""
        text = tidy(text)[:MAX_TEXT]
        if len(text) < 3:
            return None, 'refusé : trop court'
        if is_secret(text):
            return None, 'refusé : NEXUS ne retient ni mot de passe, ni code, ni numéro de carte ou de compte'
        if status == 'propose' and self.db.execute('SELECT 1 FROM refus WHERE empreinte=?',
                                                   (fingerprint(text),)).fetchone():
            return None, 'refusé : déjà refusé par l\'utilisateur'
        with self._lock:
            best = self._scored(text, None, 'duplicate')[:1]
            if best and best[0][0] >= DUPLICATE_THRESHOLD:
                row = best[0][1]
                if status == 'valide':  # la version la plus récente, dite par l'utilisateur, l'emporte
                    with self.db:
                        self.db.execute('UPDATE souvenirs SET texte=?, statut=?, vecteur=?, modele=? WHERE id=?',
                                        (text, 'valide', self._vector(text), self.embed_model, row['id']))
                    return row['id'], 'mis à jour'
                return row['id'], 'déjà connu'
            if self.db.execute('SELECT COUNT(*) FROM souvenirs').fetchone()[0] >= MAX_SOUVENIRS:
                return None, 'refusé : mémoire pleine'
            with self.db:
                cur = self.db.execute(
                    'INSERT INTO souvenirs(texte, categorie, statut, origine, cree_le, modele, vecteur) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (text, category if category in CATEGORIES else guess_category(text), status, origin,
                     now(), self.embed_model, self._vector(text)))
            return cur.lastrowid, 'ajouté'

    def propose(self, text: str) -> int | None:
        souvenir_id, outcome = self.add(text, origin='conversation', status='propose')
        return souvenir_id if outcome == 'ajouté' else None

    def confirm(self, souvenir_id: int) -> bool:
        with self._lock, self.db:
            return self.db.execute("UPDATE souvenirs SET statut='valide' WHERE id=? AND statut='propose'",
                                   (souvenir_id,)).rowcount > 0

    def reject(self, souvenir_id: int) -> bool:
        row = self.get(souvenir_id)
        if not row or row['statut'] != 'propose':
            return False
        with self._lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO refus VALUES (?,?)', (fingerprint(row['texte']), now()))
            self.db.execute('DELETE FROM souvenirs WHERE id=?', (souvenir_id,))
        return True

    def edit(self, souvenir_id: int, text: str) -> bool:
        text = tidy(text)[:MAX_TEXT]
        if len(text) < 3 or is_secret(text):
            return False
        with self._lock, self.db:
            return self.db.execute('UPDATE souvenirs SET texte=?, categorie=?, vecteur=?, modele=? WHERE id=?',
                                   (text, guess_category(text), self._vector(text), self.embed_model,
                                    souvenir_id)).rowcount > 0

    def delete(self, souvenir_id: int) -> bool:
        with self._lock, self.db:
            return self.db.execute('DELETE FROM souvenirs WHERE id=?', (souvenir_id,)).rowcount > 0

    def forget(self, description: str) -> dict | None:
        """« oublie que … » : efface le souvenir validé le plus proche, s'il l'est clairement."""
        scored = self._scored(description, 'valide', 'forget')
        threshold = 0.99 if self._by_words else RECALL_THRESHOLD
        if not scored or scored[0][0] < threshold:
            return None
        row = scored[0][1]
        self.delete(row['id'])
        return row

    def forget_all(self) -> int:
        with self._lock:
            with self.db:
                count = self.db.execute('DELETE FROM souvenirs').rowcount
                self.db.execute('DELETE FROM refus')
            self.db.execute('VACUUM')
        return count

    # -- lecture --------------------------------------------------------------------
    def get(self, souvenir_id: int) -> dict | None:
        rows = [r for r in self.list(None) if r['id'] == souvenir_id]
        return rows[0] if rows else None

    def list(self, statut: str | None = 'valide') -> list[dict[str, Any]]:
        sql = ('SELECT id, texte, categorie, statut, origine, cree_le, utilise_le, utilisations, modele, vecteur '
               'FROM souvenirs')
        params: tuple = ()
        if statut:
            sql += ' WHERE statut=?'
            params = (statut,)
        rows = self.db.execute(sql + ' ORDER BY categorie, id', params).fetchall()
        keys = ['id', 'texte', 'categorie', 'statut', 'origine', 'cree_le', 'utilise_le', 'utilisations',
                '_modele', '_vecteur']
        return [dict(zip(keys, row)) for row in rows]

    def recall(self, question: str, k: int = RECALL_K,
               threshold: float = RECALL_THRESHOLD) -> list[dict[str, Any]]:
        """Préférences de style (toujours), puis souvenirs validés liés à la question (au plus k)."""
        scored = self._scored(question)
        preferences = [row for _, row in scored if row['categorie'] == 'preference'][:MAX_PREFERENCES]
        related = [row for score, row in scored
                   if score >= threshold and row['categorie'] != 'preference'][:k]
        chosen = preferences + related
        if chosen:
            with self._lock, self.db:
                for row in chosen:
                    self.db.execute('UPDATE souvenirs SET utilise_le=?, utilisations=utilisations+1 WHERE id=?',
                                    (now(), row['id']))
        return chosen


def context_block(recalled: list[dict]) -> str:
    """Texte donné au modèle. Les souvenirs sont des propos de l'utilisateur, à prendre en compte."""
    if not recalled:
        return ''
    return ('Ce que l\'utilisateur t\'a demandé de retenir (à prendre en compte si c\'est utile) :\n'
            + '\n'.join(f'- {row["texte"]}' for row in recalled))
