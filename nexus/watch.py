"""Surveillance de la machine : règles déclaratives sur les mesures réelles.

Une règle se déclenche quand sa condition tient pendant `sustain` relevés
consécutifs, et ne retombe que lorsque la valeur repasse le seuil `clear`
(hystérésis : pas d'alerte qui clignote). Les règles ne font qu'informer :
aucune n'agit sur le système.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any, Callable

from .telemetry import Sample

METRICS: dict[str, Callable[[Sample], float | None]] = {
    'cpu_percent': lambda s: s.cpu_percent,
    'mem_available_percent': lambda s: s.mem_available_percent,
    'disk_free_percent': lambda s: s.disk_free_percent,
    'temp_c': lambda s: s.temp_c,
    # Seulement quand l'ordinateur est réellement sur batterie.
    'battery_percent': lambda s: s.battery_percent if s.on_battery else None,
}
CLEARED_WHEN_ABSENT = {'battery_percent'}
LEVELS = ('info', 'warning', 'critical')
MAX_RULES = 64


@dataclass(frozen=True)
class Rule:
    id: str
    metric: str
    op: str          # 'above' | 'below'
    threshold: float
    clear: float
    sustain: int
    level: str
    message: str     # peut contenir {value}
    advice: str

    def triggered(self, value: float) -> bool:
        return value > self.threshold if self.op == 'above' else value < self.threshold

    def cleared(self, value: float) -> bool:
        return value <= self.clear if self.op == 'above' else value >= self.clear


DEFAULT_RULES: list[dict[str, Any]] = [
    {'id': 'memoire-basse', 'metric': 'mem_available_percent', 'op': 'below',
     'threshold': 12, 'clear': 20, 'sustain': 2, 'level': 'critical',
     'message': 'Mémoire presque pleine : {value:.0f} % libre',
     'advice': "Fermer des onglets ou applications, ou choisir un modèle plus petit."},
    {'id': 'espace-session', 'metric': 'disk_free_percent', 'op': 'below',
     'threshold': 10, 'clear': 15, 'sustain': 1, 'level': 'warning',
     'message': "Espace de session presque plein : {value:.0f} % libre",
     'advice': "En session Live, cet espace est pris sur la RAM : supprimer des téléchargements."},
    {'id': 'processeur-sature', 'metric': 'cpu_percent', 'op': 'above',
     'threshold': 92, 'clear': 70, 'sustain': 15, 'level': 'info',
     'message': 'Processeur saturé depuis un moment ({value:.0f} %)',
     'advice': "Normal pendant une réponse de l'IA ; sinon, chercher l'application gourmande."},
    {'id': 'surchauffe', 'metric': 'temp_c', 'op': 'above',
     'threshold': 90, 'clear': 80, 'sustain': 3, 'level': 'warning',
     'message': 'Température élevée : {value:.0f} °C',
     'advice': "Vérifier la ventilation ; éviter les longues générations."},
    {'id': 'batterie-faible', 'metric': 'battery_percent', 'op': 'below',
     'threshold': 15, 'clear': 20, 'sustain': 1, 'level': 'warning',
     'message': 'Batterie faible : {value:.0f} %',
     'advice': "Brancher le secteur : sans persistance, tout est perdu à l'extinction."},
]


def _num(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f'{label} doit être un nombre')
    return float(value)


def _text(value: Any, label: str, size: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= size:
        raise ValueError(f'{label} : texte de 1 à {size} caractères attendu')
    return value.strip()


def validate_rule(raw: Any) -> Rule:
    if not isinstance(raw, dict):
        raise ValueError('une règle doit être un objet JSON')
    unknown = set(raw) - {'id', 'metric', 'op', 'threshold', 'clear', 'sustain',
                          'level', 'message', 'advice'}
    if unknown:
        raise ValueError(f'champs inconnus : {", ".join(sorted(unknown))}')
    rule_id = _text(raw.get('id'), 'id', 48)
    if not all(c.isalnum() or c in '-_.' for c in rule_id):
        raise ValueError('id : lettres, chiffres, - _ . uniquement')
    metric = raw.get('metric')
    if metric not in METRICS:
        raise ValueError(f'mesure inconnue : {metric!r} (connues : {", ".join(METRICS)})')
    op = raw.get('op')
    if op not in ('above', 'below'):
        raise ValueError("op doit valoir 'above' ou 'below'")
    threshold = _num(raw.get('threshold'), 'threshold')
    clear = _num(raw.get('clear', threshold), 'clear')
    if (op == 'above' and clear > threshold) or (op == 'below' and clear < threshold):
        raise ValueError(f'{rule_id} : le seuil clear doit être du côté « normal » du seuil')
    sustain = raw.get('sustain', 1)
    if isinstance(sustain, bool) or not isinstance(sustain, int) or not 1 <= sustain <= 3600:
        raise ValueError('sustain doit être un entier de 1 à 3600')
    level = raw.get('level', 'warning')
    if level not in LEVELS:
        raise ValueError(f'level doit valoir {", ".join(LEVELS)}')
    message = _text(raw.get('message'), 'message', 200)
    try:
        message.format(value=0.0)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f'{rule_id} : message mal formé (seul {{value}} est permis)') from exc
    return Rule(rule_id, metric, op, threshold, clear, sustain, level, message,
                _text(raw.get('advice', '—'), 'advice', 300))


def load_rules(raw: Any) -> list[Rule]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RULES:
        raise ValueError(f'liste de 1 à {MAX_RULES} règles attendue')
    rules = [validate_rule(r) for r in raw]
    if len({r.id for r in rules}) != len(rules):
        raise ValueError('identifiants de règles en double')
    return rules


def load_rules_file(path: Path | None) -> list[Rule]:
    """Règles de l'utilisateur si le fichier existe, sinon les règles par défaut."""
    if path is not None and path.is_file():
        return load_rules(json.loads(path.read_text(encoding='utf-8')))
    return load_rules(DEFAULT_RULES)


@dataclass
class Alert:
    rule: Rule
    value: float
    since: float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        return self.rule.message.format(value=self.value)


@dataclass(frozen=True)
class Change:
    kind: str  # 'raised' | 'cleared'
    alert: Alert


class Watcher:
    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self._streak: dict[str, int] = {r.id: 0 for r in rules}
        self._active: dict[str, Alert] = {}

    def update(self, sample: Sample) -> list[Change]:
        changes = []
        for rule in self.rules:
            value = METRICS[rule.metric](sample)
            active = self._active.get(rule.id)
            if value is None:
                # Mesure indisponible : on ne conclut rien… sauf pour la batterie, dont la mesure
                # disparaît exprès quand le secteur est branché : l'alerte doit alors se lever.
                if active is not None and rule.metric in CLEARED_WHEN_ABSENT:
                    del self._active[rule.id]
                    self._streak[rule.id] = 0
                    changes.append(Change('cleared', active))
                continue
            if active is not None:
                active.value = value
                if rule.cleared(value):
                    del self._active[rule.id]
                    self._streak[rule.id] = 0
                    changes.append(Change('cleared', active))
                continue
            self._streak[rule.id] = self._streak[rule.id] + 1 if rule.triggered(value) else 0
            if self._streak[rule.id] >= rule.sustain:
                alert = Alert(rule, value)
                self._active[rule.id] = alert
                changes.append(Change('raised', alert))
        return changes

    def active(self) -> list[Alert]:
        order = {level: i for i, level in enumerate(reversed(LEVELS))}
        return sorted(self._active.values(), key=lambda a: (order[a.rule.level], a.since))
