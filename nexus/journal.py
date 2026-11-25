"""Le journal du matin : une vraie page, composée chaque jour pour la maison.

Le contenu vient de ce que NEXUS sait déjà (agenda, choses à faire, échéances, ce qu'il a
fait de lui-même, courrier) et des actualités qu'il choisit d'après les souvenirs. La mise
en page est faite par le code, toujours la même : le modèle n'écrit, au plus, que le
« mot du jour », à partir de faits qu'on lui donne.

Sortie : une page HTML autonome (aucun script, rien de chargé depuis Internet), un PDF si un
navigateur est installé, et l'impression si elle est demandée.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as clock, timedelta
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from .agenda import DAYS, MONTHS, french_date, french_time

BROWSERS = ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable', 'chrome')
TOOLS_PATH = '/usr/bin:/bin:/usr/local/bin:/snap/bin'
KEEP_EDITIONS = 60
TEMPO_WORDS = {'bleu': 'jour bleu (le moins cher)', 'blanc': 'jour blanc', 'rouge': 'jour ROUGE (le plus cher : décaler ce qui peut l\'être)'}


@dataclass
class Edition:
    day: date
    number: int
    made: datetime
    word: str = ''                                                   # le mot du jour (facultatif, écrit par le modèle)
    today: list[dict[str, Any]] = field(default_factory=list)        # rendez-vous du jour
    week: list[dict[str, Any]] = field(default_factory=list)         # les sept jours suivants
    todos: list[dict[str, Any]] = field(default_factory=list)
    deadlines: list[dict[str, Any]] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)                # ce que NEXUS a fait seul depuis hier
    proposals: list[str] = field(default_factory=list)               # ce qui attend une réponse
    mail: list[dict[str, Any]] = field(default_factory=list)         # courrier important
    tempo: dict[str, str] = field(default_factory=dict)
    headlines: list[Any] = field(default_factory=list)
    personal: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)                   # ce qui n'a pas pu être fait
    health: list[dict[str, str]] = field(default_factory=list)       # ce que NEXUS constate sur lui-même
    healthy: list[str] = field(default_factory=list)                 # ce qui va bien (sauvegarde prouvée…)


def long_date(day: date) -> str:
    return f'{DAYS[day.weekday()].capitalize()} {day.day}{"er" if day.day == 1 else ""} {MONTHS[day.month - 1]} {day.year}'


# -- collecte ----------------------------------------------------------------------------------
def gather(now: datetime, *, number: int = 1, agenda: Any = None, library: Any = None, autonomy: Any = None,
           newsroom: Any = None, mailbox: Any = None, tempo: Callable[[str], str | None] | None = None,
           writer: Callable[[str], str] | None = None, health: Any = None) -> Edition:
    """Rassemble le contenu du jour. Chaque rubrique est indépendante : une panne n'empêche pas les autres."""
    edition = Edition(now.date(), number, now)
    midnight = datetime.combine(now.date(), clock(0, 0))
    if agenda is not None:
        try:
            for event in agenda.events(midnight, midnight + timedelta(days=8)):
                moment = datetime.fromisoformat(event['quand'])
                item = {'heure': '' if event['journee'] else french_time(moment), 'titre': event['titre'],
                        'lieu': event['lieu'], 'jour': french_date(moment.date(), now.date())}
                (edition.today if moment.date() == now.date() else edition.week).append(item)
            for todo in agenda.todos():
                due = date.fromisoformat(todo['echeance']) if todo['echeance'] else None
                edition.todos.append({'texte': todo['texte'], 'pour': french_date(due, now.date()) if due else '',
                                      'retard': bool(due and due < now.date())})
        except Exception as exc:
            edition.notes.append(f'Agenda illisible ({type(exc).__name__}).')
    if library is not None:
        try:
            from .planner import find_deadlines
            for item in find_deadlines(library):
                if now.date() <= item['date'] <= now.date() + timedelta(days=30):
                    edition.deadlines.append({'jour': french_date(item['date'], now.date()), 'type': item['type'],
                                              'titre': item['title'], 'jours': (item['date'] - now.date()).days})
        except Exception as exc:
            edition.notes.append(f'Échéances non relevées ({type(exc).__name__}).')
    if autonomy is not None:
        try:
            since = now - timedelta(hours=24)
            edition.activity = [a['resume'] for a in reversed(autonomy.acts(200, since)) if a['etat'] == 'fait']
            edition.proposals = [a['resume'] for a in autonomy.acts(200) if a['etat'] == 'propose']
        except Exception as exc:
            edition.notes.append(f'Activité non relevée ({type(exc).__name__}).')
    if mailbox is not None:
        try:
            edition.mail = mailbox.important(now)
        except Exception as exc:
            edition.notes.append(f'Courrier non relevé ({type(exc).__name__}).')
    if health is not None:
        try:
            edition.health = [{'niveau': f.level, 'titre': f.title, 'conseil': f.advice} for f in health.check(now)]
            edition.healthy = health.good(now)
        except Exception as exc:
            edition.notes.append(f'Santé de NEXUS non relevée ({type(exc).__name__}).')
    if tempo is not None:
        for key in ('today', 'tomorrow'):
            try:
                color = tempo(key)
            except Exception:
                color = None
            if color:
                edition.tempo['aujourdhui' if key == 'today' else 'demain'] = color
    if newsroom is not None:
        try:
            picked = newsroom.select(now)
            edition.headlines, edition.personal = picked['une'], picked['pour_toi']
            if newsroom.errors:
                edition.notes.append(f'{len(newsroom.errors)} flux d\'actualités injoignable(s).')
            if not edition.headlines:
                edition.notes.append('Actualités indisponibles (pas d\'Internet ?).')
        except Exception as exc:
            edition.notes.append(f'Actualités indisponibles ({type(exc).__name__}).')
    if writer is not None:
        try:
            edition.word = clean_word(writer(facts(edition)))
        except Exception:
            edition.word = ''          # pas de modèle disponible : le journal sort sans mot du jour
    return edition


def essentials(edition: Edition) -> str:
    """Deux ou trois phrases justes, calculées : ce qu'il faut savoir avant de commencer la journée."""
    parts = []
    if edition.today:
        first = edition.today[0]
        count = len(edition.today)
        parts.append(f"{count} rendez-vous aujourd'hui" + (f", le premier à {first['heure']} ({first['titre']})" if first['heure']
                                                            else f" ({first['titre']})") + '.')
    else:
        parts.append('Rien à l\'agenda aujourd\'hui.')
    late = sum(1 for t in edition.todos if t['retard'])
    if edition.todos:
        parts.append(f"{len(edition.todos)} chose{'s' if len(edition.todos) > 1 else ''} à faire"
                     + (f", dont {late} en retard" if late else '') + '.')
    if edition.tempo.get('aujourdhui') == 'rouge':
        parts.append('Jour rouge Tempo : l\'électricité est au plus cher jusqu\'à 22h.')
    if edition.tempo.get('demain') == 'rouge':
        parts.append('Demain sera un jour rouge Tempo : avance ce soir ce qui consomme (chauffage, recharge de la voiture).')
    if edition.mail:
        parts.append(f"{len(edition.mail)} courriel{'s' if len(edition.mail) > 1 else ''} important{'s' if len(edition.mail) > 1 else ''}.")
    if edition.proposals:
        parts.append(f"NEXUS attend ta réponse sur {len(edition.proposals)} proposition{'s' if len(edition.proposals) > 1 else ''}.")
    serious = [h for h in edition.health if h['niveau'] != 'info']
    if serious:
        parts.append(f"À régler sur NEXUS : {serious[0]['titre']}"
                     + (f" (et {len(serious) - 1} autre{'s' if len(serious) > 2 else ''})" if len(serious) > 1 else '') + '.')
    return ' '.join(parts)


def facts(edition: Edition) -> str:
    """Les faits du jour, en clair : tout ce que le modèle a le droit d'utiliser pour son mot."""
    lines = [f'Date : {long_date(edition.day)}.']
    lines.append('Rendez-vous aujourd\'hui : ' + ('; '.join(f"{e['heure'] or 'journée'} {e['titre']}" for e in edition.today)
                                                  or 'aucun') + '.')
    late = [t['texte'] for t in edition.todos if t['retard']]
    lines.append(f"Choses à faire : {len(edition.todos)}" + (f", dont en retard : {'; '.join(late)}" if late else '') + '.')
    if edition.tempo.get('aujourdhui'):
        lines.append(f"Électricité Tempo aujourd'hui : jour {edition.tempo['aujourdhui']}.")
    if edition.activity:
        lines.append(f'Depuis hier, NEXUS a fait {len(edition.activity)} geste(s) de rangement ou d\'agenda.')
    if edition.mail:
        lines.append(f'{len(edition.mail)} courriel(s) important(s) reçu(s).')
    return '\n'.join(lines)


WORD_PROMPT = ('Tu écris le mot du jour d\'un journal de famille. À partir des faits ci-dessous, et d\'eux seuls, écris deux '
               'phrases en français, chaleureuses et utiles, à la deuxième personne du singulier. N\'invente aucun fait, '
               'aucun chiffre, aucune météo. Pas de titre, pas de liste.')


def clean_word(text: str) -> str:
    text = re.sub(r'<think>.*?</think>', ' ', text or '', flags=re.S)
    text = ' '.join(text.replace('**', '').replace('#', '').split())
    return text[:420]


# -- mise en page ------------------------------------------------------------------------------
CSS = '''
@page { size: A4; margin: 12mm 13mm; }
* { box-sizing: border-box; }
body { margin: 0; color: #141414; background: #fff; font: 10.2pt/1.36 Georgia, 'Times New Roman', serif; }
.page { max-width: 190mm; margin: 0 auto; padding: 6mm 4mm; }
header { text-align: center; border-bottom: 3px double #141414; padding-bottom: 5px; margin-bottom: 4px; }
header h1 { font: 700 31pt/1.05 'Times New Roman', Georgia, serif; letter-spacing: .04em; margin: 0; text-transform: uppercase; }
.bandeau { display: flex; justify-content: space-between; font: 8.4pt Arial, Helvetica, sans-serif; text-transform: uppercase;
  letter-spacing: .06em; border-bottom: 1px solid #141414; padding: 3px 0 4px; margin-bottom: 9px; }
.essentiel { font-size: 11.4pt; font-weight: 700; text-align: center; margin: 3px 6mm 4px; }
.mot { font-style: italic; font-size: 10.6pt; text-align: center; margin: 0 9mm 6px; }
.colonnes { column-count: 3; column-gap: 6mm; column-rule: 1px solid #c8c8c8; margin-top: 8px; }
.titre-actus { font: 700 15pt 'Times New Roman', Georgia, serif; text-align: center; text-transform: uppercase; letter-spacing: .12em;
  border-top: 3px double #141414; border-bottom: 1px solid #141414; padding: 3px 0; margin: 8px 0 0; }
/* Les actualités par rangées de trois : des blocs simples, que tous les navigateurs savent paginer. */
.actus h2 { margin: 8px 0 5px; break-after: avoid; }
.rangee { display: flex; gap: 6mm; break-inside: avoid; margin: 0 0 8px; }
.rangee .article { flex: 1 1 0; min-width: 0; margin: 0; }
.rangee .article + .article { border-left: 1px solid #c8c8c8; padding-left: 6mm; margin-left: -3mm; }
a { color: inherit; text-decoration: none; }
.pratique section { break-inside: avoid; }
section { margin: 0 0 10px; }
h2 { font: 700 9pt Arial, Helvetica, sans-serif; text-transform: uppercase; letter-spacing: .09em; border-top: 2px solid #141414;
  border-bottom: 1px solid #141414; padding: 2px 0; margin: 0 0 5px; }
h3 { font: 700 11.2pt/1.2 Georgia, serif; margin: 0 0 2px; }
p { margin: 0 0 5px; text-align: justify; hyphens: auto; }
ul { list-style: none; margin: 0; padding: 0; }
li { margin: 0 0 4px; padding-left: 1.1em; text-indent: -1.1em; }
li::before { content: '▪ '; }
li.case::before { content: '☐ '; }
.heure { font-weight: 700; }
.petit { font: 7.6pt Arial, Helvetica, sans-serif; color: #555; }
.alerte { font-weight: 700; }
.article { break-inside: avoid; margin: 0 0 8px; }
.vide { font-style: italic; color: #555; }
footer { border-top: 1px solid #141414; margin-top: 6px; padding-top: 3px; text-align: center; }
@media screen and (max-width: 700px) { .colonnes { column-count: 1; } .rangee { display: block; }
  .rangee .article + .article { border-left: 0; padding-left: 0; margin: 10px 0 0; } .page { padding: 12px; } header h1 { font-size: 22pt; }
  body { font-size: 12pt; } .petit { font-size: 9.5pt; } }
'''


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ''), quote=True)


def _section(title: str, body: str) -> str:
    return f'<section><h2>{_e(title)}</h2>{body}</section>' if body else ''


def _list(items: list[str], css: str = '') -> str:
    if not items:          # rien à lister : la rubrique affiche son « rien », ou disparaît
        return ''
    return '<ul>' + ''.join(f'<li{" class=" + chr(34) + css + chr(34) if css else ""}>{item}</li>' for item in items) + '</ul>'


def _article(article: Any, why: bool = False) -> str:
    reason = f' · parce que tu as dit : « {_e(article.reason)} »' if why and getattr(article, 'reason', '') else ''
    summary = f'<p>{_e(article.summary)}</p>' if article.summary else ''
    return (f'<div class="article"><h3><a href="{_e(article.link)}">{_e(article.title)}</a></h3>{summary}'
            f'<div class="petit">{_e(article.source)}{reason}</div></div>')


def _short(line: str) -> str:
    """« Rangé : X → Santé/2026/2026-11-18 Cabinet - X.jpg » devient « Rangé : X → Santé/2026 »."""
    if ' → ' in line:
        head, target = line.rsplit(' → ', 1)
        line = f"{head} → {target.rsplit('/', 1)[0] if '/' in target else target}"
    return line if len(line) <= 150 else line[:149] + '…'


def render(edition: Edition, title: str = 'Le Journal de la maison') -> str:
    """La page du jour, en HTML autonome prêt à imprimer (A4, trois colonnes)."""
    today = _list([f'<span class="heure">{_e(e["heure"] or "Journée")}</span> {_e(e["titre"])}'
                   + (f' <span class="petit">— {_e(e["lieu"])}</span>' if e['lieu'] else '') for e in edition.today])
    week = _list([f'<span class="heure">{_e(e["jour"])}{" " + _e(e["heure"]) if e["heure"] else ""}</span> — {_e(e["titre"])}'
                  for e in edition.week[:9]])
    todos = _list([(f'<span class="alerte">{_e(t["texte"])}</span>' if t['retard'] else _e(t['texte']))
                   + (f' <span class="petit">({"en retard, " if t["retard"] else ""}pour {_e(t["pour"])})</span>' if t['pour'] else '')
                   for t in edition.todos[:12]], 'case')
    deadlines = _list([f'<span class="heure">{_e(d["jour"])}</span> — {_e(d["type"])} : {_e(d["titre"])}'
                       for d in edition.deadlines[:6]])
    energy = ''
    if edition.tempo:
        energy = _list([f'{label} : <span class="{"alerte" if color == "rouge" else ""}">{_e(TEMPO_WORDS.get(color, color))}</span>'
                        for label, color in (('Aujourd\'hui', edition.tempo.get('aujourdhui')), ('Demain', edition.tempo.get('demain')))
                        if color])
    mail = ''.join(f'<div class="article"><h3>{_e(m.get("objet"))}</h3><p>{_e(m.get("resume"))}</p>'
                   f'<div class="petit">{_e(m.get("de"))} · {_e(m.get("boite"))}</div></div>' for m in edition.mail[:6])
    activity = _list([_e(_short(line)) for line in edition.activity[:8]])
    if edition.proposals:
        activity += f'<p class="petit">{len(edition.proposals)} proposition(s) attendent ta réponse dans l\'appli.</p>'
    practical = [
        _section('Aujourd\'hui', today or '<p class="vide">Aucun rendez-vous.</p>'),
        _section('À faire', todos or '<p class="vide">Rien sur la liste.</p>'),
        _section('Électricité', energy),
        _section('Dans les jours qui viennent', week),
        _section('Échéances du mois', deadlines),
        _section('Courrier important', mail),
        _section('Ce que NEXUS a fait depuis hier', activity),
        _section('Santé de NEXUS', _list(
            [(f'<span class="alerte">{_e(h["titre"])}</span>' if h['niveau'] != 'info' else _e(h['titre']))
             + f' <span class="petit">— {_e(_short(h["conseil"]))}</span>' for h in edition.health[:6]]
            + [_e(line) for line in edition.healthy[:3]])),
    ]
    def rows(articles: list[Any], why: bool) -> str:
        cells = [_article(a, why) for a in articles]
        return ''.join(f'<div class="rangee">{"".join(cells[i:i + 3])}</div>' for i in range(0, len(cells), 3))

    news = ''
    if edition.headlines:
        news += '<h2>À la une</h2>' + rows(edition.headlines, False)
    if edition.personal:
        news += '<h2>Choisi pour toi</h2>' + rows(edition.personal, True)
    news_block = f'<div class="titre-actus">Actualités</div><div class="actus">{news}</div>' if news else ''
    notes = f'<p class="petit">{_e(" ".join(edition.notes))}</p>' if edition.notes else ''
    word = f'<p class="essentiel">{_e(essentials(edition))}</p>' + (f'<p class="mot">{_e(edition.word)}</p>' if edition.word else '')
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
        f'<title>{_e(title)} — {_e(long_date(edition.day))}</title><style>{CSS}</style></head><body><div class="page">'
        f'<header><h1>{_e(title)}</h1></header>'
        f'<div class="bandeau"><span>{_e(long_date(edition.day))}</span><span>N° {edition.number}</span>'
        f'<span>Composé à {_e(french_time(edition.made))} par NEXUS</span></div>'
        f'{word}<div class="colonnes pratique">{"".join(practical)}</div>{news_block}{notes}'
        '<footer class="petit">Composé sur le PC de la maison. Les titres d\'actualité appartiennent à leurs éditeurs ; '
        'les résumés sont les leurs.</footer></div></body></html>')


# -- fichiers, PDF, impression ---------------------------------------------------------------
class Press:
    """Le kiosque : garde les numéros, fabrique le PDF, imprime."""

    def __init__(self, folder: Path, settings_path: Path | None = None,
                 run: Callable[[list[str]], int] | None = None):
        self.folder, self.settings_path, self._run = Path(folder), settings_path, run

    def settings(self) -> dict[str, Any]:
        stored: dict[str, Any] = {}
        if self.settings_path is not None:
            try:
                data = json.loads(self.settings_path.read_text(encoding='utf-8'))
                stored = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                stored = {}
        printer = stored.get('imprimante')
        return {'imprimer': stored.get('imprimer') is True, 'actualites': stored.get('actualites') is not False,
                'mot': stored.get('mot') is True,      # mot du jour écrit par le modèle : essai, coupé d'office
                'imprimante': printer if isinstance(printer, str) and re.fullmatch(r'[\w.-]{1,80}', printer) else ''}

    def set(self, **changes: Any) -> dict[str, Any]:
        if self.settings_path is None:
            raise ValueError('réglages non modifiables ici')
        current = {**self.settings(), **{k: v for k, v in changes.items() if k in ('imprimer', 'actualites', 'imprimante', 'mot')}}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temporary, self.settings_path)
        return self.settings()

    def editions(self) -> list[dict[str, Any]]:
        found = []
        for page in sorted(self.folder.glob('????-??-??.html'), reverse=True):
            try:
                day = date.fromisoformat(page.stem)
            except ValueError:
                continue
            found.append({'jour': page.stem, 'titre': long_date(day), 'pdf': page.with_suffix('.pdf').is_file()})
        return found

    def next_number(self) -> int:
        try:
            return int((self.folder / 'numero').read_text().strip()) + 1
        except (OSError, ValueError):
            return 1

    def path(self, day: str, suffix: str = '.html') -> Path | None:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or suffix not in ('.html', '.pdf'):
            return None
        page = self.folder / f'{day}{suffix}'
        return page if page.is_file() else None

    def _exec(self, argv: list[str], timeout: int = 120) -> int:
        if self._run is not None:
            return self._run(argv)
        try:
            return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False).returncode
        except (OSError, subprocess.SubprocessError):
            return 1

    def browser(self) -> str | None:
        chosen = os.environ.get('NEXUS_NAVIGATEUR')
        if chosen and os.path.isabs(chosen) and os.access(chosen, os.X_OK):
            return chosen
        for name in BROWSERS:
            found = shutil.which(name, path=TOOLS_PATH)
            if found:
                return found
        return None

    def publish(self, edition: Edition, page: str) -> dict[str, Any]:
        """Enregistre le numéro du jour, fabrique son PDF, l'imprime si c'est demandé."""
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f'{edition.day.isoformat()}.html'
        fresh = not target.exists()
        temporary = target.with_suffix('.tmp')
        temporary.write_text(page, encoding='utf-8')
        os.replace(temporary, target)
        if fresh:
            (self.folder / 'numero').write_text(str(edition.number))
        result = {'jour': edition.day.isoformat(), 'html': target, 'pdf': None, 'imprime': False, 'notes': []}
        pdf = target.with_suffix('.pdf')
        pdf.unlink(missing_ok=True)
        browser = self.browser() if self._run is None else 'navigateur'
        if browser is None:
            result['notes'].append('Pas de PDF : aucun navigateur installé (sudo apt install chromium).')
        else:
            # La page n'a ni script ni contenu distant ; le navigateur ne fait que la mettre en PDF.
            # Un profil jetable : le navigateur ne lit ni n'écrit rien dans celui de l'utilisateur.
            with tempfile.TemporaryDirectory(prefix='nexus-journal-') as profile:
                argv = [browser, '--headless', '--disable-gpu', '--no-pdf-header-footer', '--no-first-run',
                        f'--user-data-dir={profile}', f'--print-to-pdf={pdf}', target.as_uri()]
                # Sans son propre bac à sable dans deux cas : en root (le navigateur l'exige), et dans le
                # service du mini-PC, où systemd enferme déjà tout NEXUS et interdit au navigateur de
                # fabriquer le sien.
                if os.geteuid() == 0 or os.environ.get('NEXUS_SERVICE') == '1':
                    argv.insert(1, '--no-sandbox')
                done = self._exec(argv) == 0 and pdf.is_file()
            if done:
                result['pdf'] = pdf
            else:
                result['notes'].append('Le navigateur n\'a pas réussi à fabriquer le PDF.')
        settings = self.settings()
        if settings['imprimer'] and result['pdf'] is not None:
            argv = ['lp'] + (['-d', settings['imprimante']] if settings['imprimante'] else []) + [str(pdf)]
            if shutil.which('lp', path=TOOLS_PATH) is None and self._run is None:
                result['notes'].append('Impression impossible : la commande lp est absente (sudo apt install cups-client).')
            elif self._exec(argv, timeout=60) == 0:
                result['imprime'] = True
            else:
                result['notes'].append('L\'imprimante n\'a pas accepté le journal (éteinte, hors ligne ?).')
        for old in sorted(self.folder.glob('????-??-??.html'), reverse=True)[KEEP_EDITIONS:]:
            old.unlink(missing_ok=True)
            old.with_suffix('.pdf').unlink(missing_ok=True)
        return result


# -- composition du numéro du jour -----------------------------------------------------------
def tempo_color(which: str) -> str | None:
    """Couleur Tempo d'aujourd'hui ('today') ou de demain ('tomorrow'), ou None si elle n'est pas publiée."""
    from urllib import request
    from .planner import TEMPO_URL, _NoRedirect, fetch_tempo

    def opener(_url: str) -> bytes:
        req = request.Request(TEMPO_URL.rsplit('/', 1)[0] + '/' + which,
                              headers={'User-Agent': 'NEXUS-local', 'Accept': 'application/json'})
        with request.build_opener(_NoRedirect()).open(req, timeout=15) as response:
            return response.read(10_000)
    return fetch_tempo(opener)[1]


def model_writer(client: Any, chooser: Any) -> Callable[[str], str]:
    """Le mot du jour, écrit par le modèle « de fond » à partir des seuls faits fournis."""
    def write(day_facts: str) -> str:
        info = client.show(chooser.pick('fond').name)
        messages = [{'role': 'system', 'content': WORD_PROMPT}, {'role': 'user', 'content': day_facts}]
        return ''.join(part for part in client.chat_stream(info, messages, num_ctx=4096, num_predict=140, temperature=0.5)
                       if isinstance(part, str))
    return write


def compose(now: datetime, press: Press, *, agenda: Any = None, library: Any = None, autonomy: Any = None,
            souvenirs: Any = None, client: Any = None, embed_model: str | None = None, mailbox: Any = None,
            tempo: Callable[[str], str | None] | None = None, writer: Callable[[str], str] | None = None,
            fetcher: Callable[[str], bytes] | None = None, health: Any = None) -> dict[str, Any]:
    """Compose et publie le numéro du jour. Renvoie ce que `Press.publish` renvoie, plus l'édition."""
    from .news import Newsroom, fetch
    newsroom = None
    if press.settings()['actualites']:
        interests = [row['texte'] for row in souvenirs.list()] if souvenirs is not None else []
        newsroom = Newsroom(client, embed_model, interests, fetcher or fetch)
    again = press.path(now.date().isoformat()) is not None            # numéro du jour refait : même numéro
    number = press.next_number() - (1 if again and press.next_number() > 1 else 0)
    edition = gather(now, number=number, agenda=agenda, library=library, autonomy=autonomy, newsroom=newsroom,
                     mailbox=mailbox, tempo=tempo, writer=writer if press.settings()['mot'] else None, health=health)
    return {**press.publish(edition, render(edition)), 'edition': edition}


def publisher(settings: Any, *, client: Any, chooser: Any, library: Any, agenda: Any, autonomy: Any,
              souvenirs: Any, mailbox: Any = None, health: Any = None) -> Callable[[datetime], dict[str, Any]]:
    """La fonction « compose le numéro du jour » d'un programme NEXUS (serveur, fenêtre, ligne de commande)."""
    press = settings.open_press()

    def publish(now: datetime) -> dict[str, Any]:
        return compose(now, press, agenda=agenda, library=library, autonomy=autonomy, souvenirs=souvenirs, client=client,
                       embed_model=settings.embed_model, mailbox=mailbox, tempo=tempo_color,
                       writer=model_writer(client, chooser) if chooser is not None else None, health=health)
    return publish
