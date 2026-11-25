"""Savoir-faire (skills) : des recettes en plusieurs étapes, lisibles et validées.

Un savoir-faire n'est PAS du code : c'est une liste d'étapes qui n'utilisent que les
outils de consultation existants (lecture seule), plus une consigne de synthèse.
Le code enchaîne les étapes, toujours dans le même ordre ; le modèle ne fait que
rédiger le résultat. C'est ce qui rend fiable un enchaînement en plusieurs étapes
avec un petit modèle local.

Les savoir-faire intégrés sont validés d'office. Ceux que NEXUS crée à ta demande
(« enregistre ça comme savoir-faire ») sont PROPOSÉS : rien ne s'exécute avant que
tu aies relu et validé la recette.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from .router import normalize

MAX_STEPS = 6
MAX_STEP_CHARS = 1600   # par étape : la synthèse doit tenir dans le contexte d'un petit modèle
MAX_TOTAL_CHARS = 7000
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
PLACEHOLDERS = ('annee', 'annee_precedente', 'mois', 'question')

# « enregistre ça comme savoir-faire », « fais-en un savoir-faire ». Volontairement étroit :
# « crée une recette de crêpes » est une question pour le modèle, pas une commande.
SAVE_RE = re.compile(r"\b(?:enregistre|garde|sauvegarde|m[ée]morise)\b.{0,20}\b(?:comme|en)\b.{0,10}"
                     r"\b(?:savoir[- ]faire|skill)\b|\bfais[- ]en\b.{0,10}\b(?:savoir[- ]faire|skill)\b", re.I)
RUN_RE = re.compile(r"^\s*(?:lance|ex[ée]cute|fais|d[ée]marre)\s+(?:le\s+|mon\s+|un\s+)?"
                    r"(?:savoir[- ]faire\s+)?[«\"']?(.+?)[»\"']?\s*$", re.I)


@dataclass
class Step:
    outil: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    id: str
    nom: str
    description: str
    declencheurs: list[str]
    etapes: list[Step]
    consigne: str
    statut: str = 'valide'        # 'valide' | 'propose'
    origine: str = 'integre'      # 'integre' | 'utilisateur'
    rapport: str | None = None    # intégrés seulement : rapport calculé par le code (nexus/reports.py)

    def to_json(self) -> str:
        data = asdict(self)
        data.pop('rapport', None)  # réservé aux savoir-faire intégrés
        return json.dumps(data, ensure_ascii=False, indent=1)

    def recipe(self, labels: dict[str, str] | None = None) -> str:
        """Recette lisible : ce que le savoir-faire va consulter, dans l'ordre."""
        friendly = {'{annee}': 'cette année', '{annee_precedente}': "l'an dernier", '{mois}': 'ce mois-ci',
                    '{question}': 'ta question', 0: 'toutes'}
        lines = [f'{self.nom} — {self.description}']
        for number, step in enumerate(self.etapes, 1):
            label = (labels or {}).get(step.outil, step.outil)
            args = ', '.join(f'{k} : {friendly.get(v, v)}' for k, v in step.arguments.items() if v not in ('', None))
            lines.append(f'  {number}. {label}' + (f' ({args})' if args else ''))
        if self.rapport:
            lines.append('  Puis : rapport calculé par NEXUS — chiffres tirés des mesures et des documents, '
                         'conseils tirés de règles fixes, sans rédaction par l\'IA.')
        else:
            lines.append(f'  Puis : l\'IA rédige la synthèse ({self.consigne})')
        return '\n'.join(lines)


BUILTIN = [
    Skill('check-up-pc', 'Check-up du PC',
          "État complet de l'ordinateur : système, programmes gourmands, disques, erreurs, firmwares.",
          ['check-up', 'checkup', 'check up', 'bilan du pc', 'bilan de mon pc', 'bilan de l ordinateur',
           'etat de mon pc', 'etat de mon ordinateur', 'diagnostic complet', 'verifie mon pc',
           'verifie mon ordinateur'],
          [Step('infos_systeme'), Step('processus_gourmands', {'critere': 'memoire', 'nombre': 6}),
           Step('stockage'), Step('erreurs_recentes', {'minutes': 1440}), Step('firmwares_manquants')],
          "Fais un bilan de santé de l'ordinateur en trois parties : ce qui va bien, les points "
          "d'attention (avec les chiffres), et au plus trois conseils concrets que l'utilisateur "
          "appliquera lui-même.", rapport='check_up'),
    Skill('bilan-energie', 'Bilan énergie',
          "Dépenses d'électricité de l'année, consommation par couleur Tempo et conseils.",
          ['bilan energie', 'bilan electricite', 'bilan d energie', 'bilan d electricite', 'point energie',
           'bilan tempo', 'ma consommation electrique', 'ma consommation d electricite', 'ma conso electrique'],
          [Step('depenses', {'categorie': 'energie', 'annee': '{annee}'}),
           Step('depenses', {'categorie': 'energie', 'annee': '{annee_precedente}'})],
          "Résume les dépenses d'électricité de cette année et de l'an dernier (totaux, factures), "
          "la consommation par couleur Tempo si elle apparaît (bleu, blanc, rouge), puis donne deux "
          "conseils adaptés à Tempo (éviter les heures pleines des jours rouges).", rapport='bilan_energie'),
    Skill('dossier-impots', 'Dossier impôts',
          "Rassemble ce qu'il faut pour la déclaration de revenus et liste ce qui manque.",
          ['dossier impots', 'declaration d impots', 'declaration de revenus', 'preparer mes impots',
           'preparer ma declaration', 'papiers pour les impots'],
          [Step('depenses', {'categorie': 'impots', 'annee': 0}),
           Step('depenses', {'categorie': 'travail', 'annee': '{annee_precedente}'})],
          "Liste les documents trouvés utiles pour la déclaration (avec leur fichier), les chiffres "
          "clés (revenu fiscal de référence, impôt payé, salaires), puis ce qui semble manquer.",
          rapport='dossier_impots'),
]


def fill(value: Any, question: str, today: date | None = None) -> Any:
    """Remplace {annee}, {annee_precedente}, {mois}, {question} ; le reste est inchangé."""
    if not isinstance(value, str):
        return value
    today = today or date.today()
    values = {'annee': today.year, 'annee_precedente': today.year - 1, 'mois': today.month,
              'question': question[:300]}
    whole = re.fullmatch(r'\{(\w+)\}', value)
    if whole and whole.group(1) in values:
        return values[whole.group(1)]  # garde le type (entier pour l'année)
    return re.sub(r'\{(\w+)\}', lambda m: str(values.get(m.group(1), m.group(0))), value)


def match_key(text: str) -> str:
    """Forme de comparaison : minuscules, sans accents ni ponctuation, espaces simples."""
    return ' '.join(re.findall(r'\w+', normalize(text)))


def _slug(text: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', normalize(text)).strip('-')[:40].strip('-')
    return slug if len(slug) >= 2 else 'savoir-faire'


def validate(raw: Any, tools: set[str]) -> Skill:
    """Recette venant d'un fichier : structure stricte, outils connus uniquement."""
    if not isinstance(raw, dict):
        raise ValueError('un savoir-faire doit être un objet JSON')
    unknown = set(raw) - {'id', 'nom', 'description', 'declencheurs', 'etapes', 'consigne', 'statut', 'origine'}
    if unknown:
        raise ValueError(f'champs inconnus : {", ".join(sorted(unknown))}')
    skill_id = raw.get('id')
    if not isinstance(skill_id, str) or not ID_RE.fullmatch(skill_id):
        raise ValueError('id : minuscules, chiffres et tirets')
    texts = {}
    for key, limit in (('nom', 60), ('description', 300), ('consigne', 800)):
        value = raw.get(key)
        if not isinstance(value, str) or not 2 <= len(value.strip()) <= limit:
            raise ValueError(f'{key} : texte de 2 à {limit} caractères')
        texts[key] = value.strip()
    triggers = raw.get('declencheurs', [])
    if (not isinstance(triggers, list) or len(triggers) > 20
            or not all(isinstance(t, str) and 3 <= len(t) <= 80 for t in triggers)):
        raise ValueError('declencheurs : liste de phrases courtes')
    steps_raw = raw.get('etapes')
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= MAX_STEPS:
        raise ValueError(f'etapes : de 1 à {MAX_STEPS}')
    steps = []
    for step in steps_raw:
        if not isinstance(step, dict) or set(step) - {'outil', 'arguments'}:
            raise ValueError('étape : {"outil": …, "arguments": {…}}')
        if step.get('outil') not in tools:
            raise ValueError(f'outil inconnu : {step.get("outil")!r}')
        args = step.get('arguments', {})
        if not isinstance(args, dict) or not all(isinstance(k, str) and isinstance(v, (str, int))
                                                  for k, v in args.items()):
            raise ValueError('arguments : texte ou nombres seulement')
        steps.append(Step(step['outil'], dict(args)))
    status = raw.get('statut', 'propose')
    if status not in ('valide', 'propose'):
        raise ValueError('statut : valide ou propose')
    return Skill(skill_id, texts['nom'], texts['description'], [match_key(t) for t in triggers], steps,
                 texts['consigne'], status, 'utilisateur')


class SkillBook:
    """Savoir-faire intégrés + ceux de l'utilisateur (un fichier JSON chacun)."""

    def __init__(self, folder: Path | None, tools: set[str]):
        self.folder, self.tools = folder, tools
        self.errors: list[str] = []

    def all(self) -> list[Skill]:
        skills = {s.id: s for s in BUILTIN}
        self.errors = []
        if self.folder is not None and self.folder.is_dir():
            for path in sorted(self.folder.glob('*.json')):
                try:
                    skill = validate(json.loads(path.read_text(encoding='utf-8')), self.tools)
                except (ValueError, json.JSONDecodeError, OSError) as exc:
                    self.errors.append(f'{path.name} : {exc}')
                    continue
                if skill.id in skills and skills[skill.id].origine == 'integre':
                    self.errors.append(f'{path.name} : identifiant réservé')
                    continue
                skills[skill.id] = skill
        return list(skills.values())

    def get(self, skill_id: str) -> Skill | None:
        return next((s for s in self.all() if s.id == skill_id), None)

    def match(self, question: str) -> Skill | None:
        """Savoir-faire VALIDÉ demandé par la question (« lance le bilan énergie », ou un déclencheur)."""
        text = match_key(question)
        skills = [s for s in self.all() if s.statut == 'valide']
        run = RUN_RE.match(question)
        if run:
            wanted = match_key(run.group(1))
            for skill in skills:
                if wanted in (match_key(skill.id), match_key(skill.nom)):
                    return skill
        best, best_len = None, 0
        for skill in skills:
            for trigger in skill.declencheurs:
                trigger = match_key(trigger)
                if trigger and re.search(r'\b' + re.escape(trigger) + r'\b', text) and len(trigger) > best_len:
                    best, best_len = skill, len(trigger)
        return best

    def _path(self, skill_id: str) -> Path:
        if self.folder is None or not ID_RE.fullmatch(skill_id):
            raise ValueError('savoir-faire non enregistrable')
        return self.folder / f'{skill_id}.json'

    def save(self, skill: Skill) -> Path:
        validate(json.loads(skill.to_json()) | {'statut': skill.statut}, self.tools)
        path = self._path(skill.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill.to_json(), encoding='utf-8')
        return path

    def propose_from(self, question: str, calls: list[tuple[str, dict]]) -> Skill:
        """Recette tirée d'une réponse : les mêmes consultations, pour la même demande."""
        steps = []
        for name, args in calls[:MAX_STEPS]:
            args = {k: ('{question}' if v == question else v) for k, v in args.items()
                    if isinstance(v, (str, int)) and not isinstance(v, bool)}
            if name in self.tools and Step(name, args) not in steps:
                steps.append(Step(name, args))
        if not steps:
            raise ValueError('la dernière réponse n\'a consulté aucun outil')
        base, existing = _slug(question), {s.id for s in self.all()}
        skill_id, number = base, 2
        while skill_id in existing:
            skill_id, number = f'{base[:36]}-{number}', number + 1
        title = question.strip().rstrip('?!. ')[:60]
        skill = Skill(skill_id, title[:1].upper() + title[1:], f'Créé à partir de : « {question.strip()[:200]} »',
                      [match_key(question)[:80]], steps, f'Réponds à la demande : « {question.strip()[:300]} ».',
                      'propose', 'utilisateur')
        self.save(skill)
        return skill

    def confirm(self, skill_id: str) -> bool:
        skill = self.get(skill_id)
        if skill is None or skill.origine != 'utilisateur' or skill.statut == 'valide':
            return False
        skill.statut = 'valide'
        self.save(skill)
        return True

    def delete(self, skill_id: str) -> bool:
        skill = self.get(skill_id)
        if skill is None or skill.origine != 'utilisateur':
            return False
        self._path(skill_id).unlink(missing_ok=True)
        return True
