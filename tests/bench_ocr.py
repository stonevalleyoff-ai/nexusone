"""Banc de mesure de la reconnaissance de texte.

    python3 -m tests.bench_ocr

Les faux documents du jeu d'essai sont transformés en scans de plus en plus mauvais
(ImageMagick, outil de développement seulement), relus par NEXUS, puis comparés au texte
d'origine : la date et le montant trouvés par les règles sont-ils les mêmes ? quelle part
des mots est retrouvée ?
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from nexus.library import clean, extract, extract_pages, find_amount, find_date, ocr_languages
from tests.corpus import build
from tests.scans import rasterize, write_scan_pdf

LEVELS = [
    ('scan propre (150 points par pouce)', 150, []),
    ('photo correcte (penchée, un peu floue)', 150,
     ['-rotate', '1.6', '-blur', '0x0.6', '-attenuate', '0.4', '+noise', 'Gaussian', '-quality', '60']),
    ('photo médiocre (penchée, floue, sombre, basse définition)', 110,
     ['-rotate', '-3.5', '-blur', '0x1.0', '-attenuate', '0.9', '+noise', 'Gaussian',
      '-brightness-contrast', '-18x-22', '-quality', '38']),
]


def degrade(image: tuple[int, int, bytes], operations: list[str], work: Path) -> tuple[int, int, bytes]:
    if not operations:
        return image
    width, height, pixels = image
    (work / 'in.pgm').write_bytes(b'P5\n%d %d\n255\n' % (width, height) + pixels)
    subprocess.run(['convert', str(work / 'in.pgm'), '-background', 'white', *operations, str(work / 'photo.jpg')],
                   check=True, capture_output=True)
    subprocess.run(['convert', str(work / 'photo.jpg'), '-colorspace', 'Gray', '-depth', '8', str(work / 'out.pgm')],
                   check=True, capture_output=True)
    magic, w, h, _maximum, data = (work / 'out.pgm').read_bytes().split(maxsplit=4)
    return int(w), int(h), data[-int(w) * int(h):]


def words(text: str) -> set[str]:
    return set(re.findall(r'[a-zà-ÿ0-9]{4,}', text.lower()))


def measure(docs: list[dict], truth: dict, source: Path, work: Path, dpi: int, operations: list[str],
            languages: str) -> dict:
    """Relit tous les documents sous forme de scans dégradés ; renvoie les comptes."""
    result = {'dates': 0, 'amounts': 0, 'with_amount': 0, 'recall': 0.0, 'pages': 0, 'seconds': 0.0,
              'unreadable': 0, 'misses': []}
    for item in docs:
        count, date, amount, vocabulary = truth[item['file']]
        images = [degrade(rasterize(source / item['file'], n, dpi), operations, work) for n in range(1, count + 1)]
        scan = work / 'scan.pdf'
        write_scan_pdf(scan, images)
        begun = time.monotonic()
        text = '\n'.join(extract(scan, None, languages).pages)
        result['seconds'] += time.monotonic() - begun
        result['pages'] += count
        found = words(clean(text))
        share = len(vocabulary & found) / max(1, len(vocabulary))
        result['recall'] += share
        result['unreadable'] += share < 0.5
        result['dates'] += find_date(text) == date
        if amount is not None:
            result['with_amount'] += 1
            if find_amount(text) == amount:
                result['amounts'] += 1
            else:
                result['misses'].append(f"{item['file']} : {find_amount(text)} au lieu de {amount}")
    return result


def run() -> int:
    languages = ocr_languages()
    if not languages or shutil.which('convert') is None:
        print('Il faut tesseract (avec le français), pdftoppm et ImageMagick (convert).')
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        source, work = Path(tmp) / 'source', Path(tmp) / 'travail'
        work.mkdir()
        docs = [d for d in build(source) if d['kind'] != 'scan']
        truth = {}
        for item in docs:
            pages = extract_pages(source / item['file'])
            text = '\n'.join(pages)
            truth[item['file']] = (len(pages), find_date(text), find_amount(text), words(clean(text)))
        print(f'{len(docs)} documents, {sum(t[0] for t in truth.values())} pages, langues : {languages}')
        for label, dpi, operations in LEVELS:
            print(f'\n{label}')
            r = measure(docs, truth, source, work, dpi, operations, languages)
            wrong = [m for m in r['misses'] if ' : None ' not in m]
            print(f"  mots retrouvés {100 * r['recall'] / len(docs):5.1f} % · documents illisibles {r['unreadable']}/{len(docs)}"
                  f" · même date {r['dates']}/{len(docs)} · montant : {r['amounts']} justes, "
                  f"{len(r['misses']) - len(wrong)} non trouvés, {len(wrong)} faux sur {r['with_amount']}"
                  f" · {r['seconds'] / r['pages']:.1f} s par page")
            for miss in wrong:
                print('      montant faux — ' + miss)
        # Le chemin d'une photo JPEG envoyée par le téléphone (première page de chaque document).
        for label, dpi, operations in LEVELS[1:]:
            recall, seconds = 0.0, 0.0
            for item in docs:
                degrade(rasterize(source / item['file'], 1, dpi), operations, work)
                vocabulary = words(clean(extract_pages(source / item['file'])[0]))
                begun = time.monotonic()
                text = extract(work / 'photo.jpg', None, languages).pages[0]
                seconds += time.monotonic() - begun
                recall += len(vocabulary & words(clean(text))) / max(1, len(vocabulary))
            print(f'\nfichier JPEG, {label} : mots retrouvés {100 * recall / len(docs):.1f} % · {seconds / len(docs):.1f} s par photo')
    return 0


if __name__ == '__main__':
    sys.exit(run())
