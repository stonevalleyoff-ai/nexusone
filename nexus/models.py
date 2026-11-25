"""NEXUS choisit lui-même son modèle.

Deux rôles, parce que les besoins ne sont pas les mêmes :

- **dialogue** : répondre pendant que la personne attend. Il faut le meilleur modèle
  parmi ceux qui répondent vite sur cette machine ;
- **fond** : classer un document reçu, ou répondre à une question où la personne a dit
  de prendre le temps. Personne n'attend : on prend le meilleur modèle qui tient en mémoire.

Le choix s'appuie sur des mesures faites sur cette machine (`nexus modeles evaluer`,
nexus/evaluation.py). Sans mesure, NEXUS se rabat sur ce qui a été mesuré pendant son
développement, puis sur la taille du modèle : c'est dit dans l'explication du choix.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from .ollama import OllamaError

AUTO = 'auto'                     # nom de modèle signifiant « NEXUS choisit »
INTERACTIVE_SECONDS = 45.0        # au-delà, une réponse type n'est plus du dialogue
NOISE = 0.13                      # une question sur huit : en dessous, deux mesures se valent
SYSTEM_RESERVE_GIB = 2.0          # mémoire laissée au système et à l'interface (un quart de la machine au plus)
# Part des bons choix d'outil mesurée pendant le développement (32 essais par modèle).
KNOWN_QUALITY = {'qwen3.5:2b': 0.91, 'qwen3:1.7b': 0.62, 'inkling2-mini-iq2': 0.97, 'inkling2-mini': 0.97}
# « Prends ton temps » : la personne accepte d'attendre pour une meilleure réponse.
DEEP_RE = re.compile(r'\b(prends? (ton|tout ton) temps|reflechis bien|en detail|de facon detaillee|'
                     r'analyse (complete|detaillee|approfondie)|reponse (approfondie|detaillee)|approfondi\w*)\b')


@dataclass(frozen=True)
class Candidate:
    name: str
    engine: str                   # 'ollama' ou 'colibri'
    size: int                     # octets sur le disque
    need_gib: float               # mémoire vive nécessaire, estimée
    fits: bool
    quality: float                # 0 à 1
    measured: bool                # qualité et temps mesurés sur cette machine
    seconds: float | None         # durée d'une réponse type avec consultation
    speed: float | None           # jetons par seconde
    interactive: bool

    def describe(self) -> str:
        parts = [f'{self.size / 2**30:.1f} Go']
        if self.measured:
            parts.append(f'bons choix d\'outil {round(self.quality * 100)} %')
            if self.seconds is not None:
                parts.append(f'réponse type en {self.seconds:.0f} s')
        else:
            parts.append(f'qualité estimée {round(self.quality * 100)} % (non mesuré ici)')
        if not self.fits:
            parts.append(f'ne tient pas en mémoire ({self.need_gib:.0f} Go nécessaires)')
        return ', '.join(parts)


@dataclass(frozen=True)
class Choice:
    name: str
    reason: str


def machine_signature(cores: int | None, ram_mib: int | None) -> str:
    """Des mesures faites sur un autre ordinateur (clé USB déplacée) ne valent pas ici."""
    return f'{cores or 0} coeurs, {round((ram_mib or 0) / 1024)} Go'


def estimated_quality(name: str, size: int) -> float:
    base = name.split(':latest')[0]
    if base in KNOWN_QUALITY:
        return KNOWN_QUALITY[base]
    gib = max(0.5, size / 2**30)
    return round(min(0.9, 0.45 + 0.12 * math.log2(gib + 1)), 2)   # plus gros : supposé meilleur, sans plus


class Measures:
    """Mesures des modèles sur cette machine, dans un petit fichier JSON."""

    def __init__(self, path: Path | None, signature: str):
        self.path, self.signature = path, signature

    def _load(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, name: str) -> dict[str, Any] | None:
        entry = self._load().get(name)
        if not isinstance(entry, dict) or entry.get('machine') != self.signature:
            return None
        if not isinstance(entry.get('outils'), (int, float)) or not 0 <= entry['outils'] <= 1:
            return None
        return entry

    def save(self, name: str, result: dict[str, Any]) -> None:
        if self.path is None:
            return
        data = self._load()
        data[name] = {**result, 'machine': self.signature, 'quand': datetime.now().isoformat(timespec='minutes')}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temporary, self.path)


class Chooser:
    def __init__(self, client: Any, measures: Measures, *, ram_mib: int | None, forced: str | None = None):
        self.client, self.measures, self.ram_mib, self.forced = client, measures, ram_mib, forced
        self._chat: dict[str, bool] = {}

    def _can_chat(self, name: str) -> bool:
        if name not in self._chat:
            try:
                self._chat[name] = bool(self.client.show(name).chat)
            except OllamaError:
                self._chat[name] = False
        return self._chat[name]

    def candidates(self) -> list[Candidate]:
        """Les modèles de dialogue installés, avec ce qu'on sait de chacun."""
        total_gib = (self.ram_mib or 0) / 1024
        found = []
        for model in self.client.models():
            name, size, engine = model['name'], int(model.get('size') or 0), model.get('engine', 'ollama')
            if not self._can_chat(name):
                continue       # modèle de sens (bge-m3…) : il ne dialogue pas
            gib = size / 2**30
            # colibrì adapte sa mémoire à la machine ; un modèle Ollama doit tenir tout entier.
            need = max(4.0, min(gib, 0.45 * total_gib)) if engine == 'colibri' else gib * 1.15 + 0.6
            fits = not total_gib or need + min(SYSTEM_RESERVE_GIB, total_gib / 4) <= total_gib
            measure = self.measures.get(name)
            if measure is not None:
                quality, seconds, speed = float(measure['outils']), measure.get('secondes'), measure.get('jetons_s')
                seconds = float(seconds) if isinstance(seconds, (int, float)) else None
                interactive = seconds is not None and seconds <= INTERACTIVE_SECONDS
            else:
                quality, seconds, speed = estimated_quality(name, size), None, None
                # Sans mesure : un petit modèle Ollama est supposé assez rapide, un gros modèle non.
                interactive = engine == 'ollama' and gib <= 6
            found.append(Candidate(name, engine, size, round(need, 1), fits, quality, measure is not None,
                                   seconds, float(speed) if isinstance(speed, (int, float)) else None, interactive))
        return found

    def pick(self, role: str = 'dialogue') -> Choice:
        """Le modèle pour ce rôle ('dialogue' ou 'fond'), avec l'explication du choix."""
        if self.forced:
            return Choice(self.forced, 'modèle imposé (NEXUS_MODEL)')
        candidates = self.candidates()
        if not candidates:
            raise OllamaError('Aucun modèle de dialogue installé. Avec Internet : ollama pull qwen3.5:2b')
        # Si rien ne tient en mémoire, on tente le moins gourmand plutôt que le meilleur.
        usable = [c for c in candidates if c.fits] or [min(candidates, key=lambda c: c.need_gib)]
        quick = [c for c in usable if c.interactive]
        if role == 'dialogue' and quick:
            best = max(quick, key=lambda c: (c.quality, c.speed or 0, -c.size))
            better = [c for c in usable if c.quality > best.quality and not c.interactive]
            why = 'le meilleur parmi ceux qui répondent vite sur cette machine'
            if better:
                slow = max(better, key=lambda c: c.quality)
                why += f' ({slow.name} est meilleur mais trop lent pour dialoguer : gardé pour le travail de fond)'
        elif role == 'dialogue':
            best = max(usable, key=lambda c: (c.quality, -c.size))
            why = 'le meilleur disponible (aucun ne répond vite sur cette machine)'
        else:
            # La mesure ne porte que sur le choix des outils. Quand deux modèles s'y valent, le plus
            # gros rédige et classe mieux : personne n'attend, on le prend.
            top = max(c.quality for c in usable)
            close = [c for c in usable if c.quality >= top - NOISE]
            best = max(close, key=lambda c: (c.size, c.quality))
            why = ('le meilleur modèle qui tient en mémoire' if len(close) == 1 else
                   'le plus gros parmi les meilleurs : personne n\'attend, autant prendre celui qui rédige et classe le mieux')
        if len(candidates) == 1:
            why = 'seul modèle de dialogue installé'
        elif not best.measured:
            why += ' — estimation, à confirmer avec : nexus modeles evaluer'
        return Choice(best.name, why)

    def for_question(self, question: str, deep: bool = False) -> Choice:
        """Dialogue ordinaire, ou meilleur modèle si la personne accepte d'attendre."""
        from .router import normalize
        if deep or DEEP_RE.search(normalize(question)):
            choice = self.pick('fond')
            return Choice(choice.name, choice.reason + ' (réponse approfondie demandée)')
        return self.pick('dialogue')

    def roles(self) -> dict[str, Choice]:
        return {'dialogue': self.pick('dialogue'), 'fond': self.pick('fond')}
