"""Rapports des savoir-faire intégrés : les faits et les calculs sont faits par le CODE.

Mesuré : un petit modèle local (qwen3:1.7b comme qwen3.5:2b) se trompe dans les synthèses
à plusieurs sources (additions fausses, heures creuses et pleines inversées, conseil
« sudo rm -rf »). Ici, chaque chiffre vient d'une mesure ou d'un document, chaque conseil
d'une règle explicite. Rien n'est inventé ; ce qui manque est dit.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import os
import re
from typing import Any, Callable

from .library import money
from .telemetry import Telemetry, gib

KWH_RE = re.compile(r'jours?\s+(bleus?|blancs?|rouges?)\s*:\s*([\d\s  ]+?)\s*kwh', re.I)
TEMPO_RULES = [
    "Heures creuses : de 22h à 6h, tous les jours : c'est là qu'il faut lancer chauffe-eau, "
    "lave-linge, lave-vaisselle et recharge de la voiture.",
    "Jours rouges : 22 par an, du 1er novembre au 31 mars, en semaine. Leurs heures pleines "
    "(6h-22h) ont le kWh le plus cher de l'année : baisser le chauffage d'1 à 2 °C ces jours-là "
    "et ne rien recharger en journée.",
    "La couleur du lendemain est connue la veille au soir : NEXUS pourra prévenir (tâche planifiée).",
]


def _count_items(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith('- '))


# --------------------------------------------------------------------------- check-up
def check_up(results: dict[str, str], env: Any, today: date) -> str:
    telemetry = Telemetry(env.proc, env.sys)
    sample = telemetry.sample()
    sample = telemetry.sample() if sample.cpu_percent is None else sample
    good, watch, advice = [], [], []
    free_pct = sample.mem_available_percent
    if free_pct is not None:
        line = f'Mémoire : {gib(sample.mem_available_mib)} libres sur {gib(sample.mem_total_mib)} ({free_pct:.0f} %)'
        if free_pct < 15:
            watch.append(line)
            advice.append('Mémoire presque pleine : fermer les programmes les plus gourmands '
                          '(liste ci-dessous) ou choisir un modèle d\'IA plus petit.')
        else:
            good.append(line)
    try:
        st = os.statvfs('/')
        disk_free = 100 * st.f_bavail / st.f_blocks if st.f_blocks else None
        line = f'Disque système : {gib(st.f_bavail * st.f_frsize // 2**20)} libres ({disk_free:.0f} %)'
        if disk_free is not None and disk_free < 10:
            watch.append(line)
            advice.append('Disque presque plein : vider la corbeille et le dossier Téléchargements, '
                          'déplacer les vidéos et photos vers un autre support.')
        else:
            good.append(line)
    except OSError:
        pass
    if sample.temp_c is not None:
        (watch if sample.temp_c >= 85 else good).append(f'Température maximale : {sample.temp_c:.0f} °C')
        if sample.temp_c >= 85:
            advice.append('Température élevée : vérifier que les aérations ne sont pas bouchées.')
    errors = results.get('erreurs_recentes', '')
    count = _count_items(errors)
    if errors.startswith('ERREUR'):
        watch.append('Journal système illisible : erreurs non vérifiées')
    elif count:
        watch.append(f'{count} type(s) d\'erreur dans le journal des dernières 24 h (détail : nexus outil erreurs_recentes)')
        advice.append('Erreurs système : si un problème est visible (périphérique, plantage), '
                      'demande « pourquoi cette erreur ? » en citant la ligne concernée.')
    else:
        good.append('Aucune erreur système dans les dernières 24 h')
    firmware = results.get('firmwares_manquants', '')
    missing = _count_items(firmware)
    if missing:
        watch.append(f'{missing} firmware(s) manquant(s) : un périphérique (Wi-Fi, son, Bluetooth) peut mal fonctionner')
        advice.append('Firmwares manquants : sur Debian, installer le paquet « firmware-* » correspondant '
                      '(nom visible dans « nexus outil firmwares_manquants »).')
    elif not firmware.startswith('ERREUR'):
        good.append('Aucun firmware manquant')
    lines = ['# Check-up du PC', '', '## Ce qui va bien'] + [f'- {g}' for g in good]
    lines += ['', '## Points d\'attention'] + ([f'- {w}' for w in watch] or ['- Rien à signaler'])
    lines += ['', '## Programmes qui prennent le plus de mémoire']
    lines += [l for l in results.get('processus_gourmands', '').splitlines() if l.startswith('- ')][:5]
    lines += ['', '## Conseils'] + ([f'{i}. {a}' for i, a in enumerate(advice, 1)]
                                     or ['Aucune action nécessaire.'])
    return '\n'.join(lines)


# --------------------------------------------------------------------------- énergie
def _tempo_kwh(library: Any) -> dict[str, dict[str, int]]:
    """kWh par couleur et par année, additionnés sur toutes les factures d'énergie."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = library.db.execute('''SELECT d.id, d.doc_date, c.text FROM documents d JOIN chunks c ON c.doc_id=d.id
                                 WHERE d.category='energie' AND d.doc_date IS NOT NULL''').fetchall()
    seen = set()   # les passages se chevauchent : une même ligne d'un document ne compte qu'une fois
    for doc_id, doc_date, text in rows:
        for match in KWH_RE.finditer(text):
            key = (doc_id, re.sub(r'\s+', ' ', match.group(0).lower()))
            if key in seen:
                continue
            seen.add(key)
            totals[doc_date[:4]][match.group(1).lower().rstrip('s')] += int(re.sub(r'\D', '', match.group(2)) or 0)
    return totals


def bilan_energie(results: dict[str, str], env: Any, today: date) -> str:
    library = env.library
    lines = ['# Bilan énergie', '']
    if library is None:
        return '\n'.join(lines + ['Bibliothèque de documents non indexée : rien à analyser '
                                  '(commande : nexus docs indexer).'])
    for year in (today.year, today.year - 1):
        rows = library.expenses('energie', year)
        if rows:
            total = sum(r['amount_cents'] for r in rows)
            lines.append(f'## {year} : {money(total)} sur {len(rows)} facture(s)')
            lines += [f"- {r['date']} · {r['issuer'] or '?'} : {money(r['amount_cents'])} [{r['path']}]" for r in rows]
        else:
            lines.append(f'## {year} : aucune facture d\'énergie trouvée')
        lines.append('')
    kwh = _tempo_kwh(library)
    if kwh:
        lines.append('## Consommation par couleur Tempo (additionnée sur les factures)')
        for year in sorted(kwh, reverse=True)[:2]:
            colors = kwh[year]
            total = sum(colors.values())
            red_share = 100 * colors.get('rouge', 0) / total if total else 0
            lines.append(f"- {year} : bleu {colors.get('bleu', 0)} kWh · blanc {colors.get('blanc', 0)} kWh · "
                         f"rouge {colors.get('rouge', 0)} kWh — total {total} kWh, dont {red_share:.0f} % en jours rouges")
        lines.append('')
    lines.append('## Règles Tempo à garder en tête')
    lines += [f'- {rule}' for rule in TEMPO_RULES]
    if kwh:
        latest = kwh[max(kwh)]
        total = sum(latest.values())
        if total and latest.get('rouge', 0) / total > 0.10:
            lines.append('- Ta part de consommation en jours rouges dépasse 10 % : c\'est là que se trouvent '
                         'les économies (chauffage et appareils décalés).')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- impôts
def dossier_impots(results: dict[str, str], env: Any, today: date) -> str:
    library = env.library
    income_year = today.year - 1
    lines = [f'# Dossier impôts : déclaration {today.year} (revenus {income_year})', '']
    if library is None:
        return '\n'.join(lines + ['Bibliothèque de documents non indexée (commande : nexus docs indexer).'])
    docs = library.documents()

    def find(predicate: Callable[[dict], bool]) -> list[dict]:
        return [d for d in docs if predicate(d)]

    avis = find(lambda d: d['category'] == 'impots' and 'avis' in ((d['title'] or '') + d['path']).lower())
    payslips = find(lambda d: d['category'] == 'travail' and (d['doc_date'] or '').startswith(str(income_year)))
    december = [d for d in payslips if (d['doc_date'] or '')[5:7] == '12']
    checks = [
        ('Dernier avis d\'impôt (revenu fiscal de référence, numéro fiscal)', avis),
        (f'Bulletins de salaire {income_year}', payslips),
        (f'Bulletin de décembre {income_year} (cumul du net imposable de l\'année)', december),
    ]
    lines.append('## Pièces')
    for label, found in checks:
        if found:
            lines.append(f'- ✅ {label} : ' + ', '.join(d['path'] for d in found[:4])
                         + (f' (+{len(found) - 4})' if len(found) > 4 else ''))
        else:
            lines.append(f'- ❌ {label} : non trouvé dans les documents')
    lines += ['', '## Chiffres relevés']
    for d in avis[:1]:
        text = '\n'.join(t for (t,) in library.db.execute('SELECT text FROM chunks c JOIN documents d '
                                                          'ON d.id=c.doc_id WHERE d.path=?', (d['path'],)))
        for label in ('Revenu fiscal de référence', 'Impôt sur le revenu net', 'Reste à payer'):
            match = re.search(re.escape(label) + r'\s*:\s*([^\n]+)', text)
            if match:
                lines.append(f"- {label} ({d['path']}) : {match.group(1).strip()}")
    if len(lines) and lines[-1] == '## Chiffres relevés':
        lines.append('- Aucun avis d\'impôt trouvé.')
    lines += ['', '## À vérifier selon ta situation (non détectable automatiquement)',
              '- Attestations de dons, de frais de garde, d\'emploi à domicile, de pension.',
              '- Frais réels, si tu ne prends pas l\'abattement de 10 %.',
              '- Les montants sont préremplis par l\'administration : l\'essentiel est de les vérifier.']
    return '\n'.join(lines)


REPORTS: dict[str, Callable[[dict[str, str], Any, date], str]] = {
    'check_up': check_up, 'bilan_energie': bilan_energie, 'dossier_impots': dossier_impots,
}
