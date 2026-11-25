"""Reconnaissance de texte : scans et photos deviennent des documents comme les autres.

Deux familles de tests : la mécanique, avec de faux programmes (toujours lancés) ; et la
vraie chaîne pdftoppm + tesseract, quand ils sont installés.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from nexus import library as library_module
from nexus.library import Library, extract, ocr_image
from nexus.tools import Env, Toolbox
from tests.pdfgen import write_pdf

REAL = bool(library_module.ocr_languages())
FACTURE = ['# Garage du Centre', 'Facture n 2026-0412', 'Date : 14/11/2026',
           'Remplacement des plaquettes de frein avant', 'Montant total TTC : 286,40 €']


class FakeTools:
    """Faux pdftotext / pdftoppm / tesseract : on choisit ce que chaque fichier « contient »."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.pdf_text: dict[str, list[str]] = {}       # nom du PDF -> texte de chaque page
        self.recognised: dict[str, str] = {}           # nom de l'image, ou « fichier.pdf#page » -> texte
        self.no_orientation = False                    # tesseract sans données d'orientation

    def __call__(self, argv: list[str]) -> tuple[int, bytes]:
        self.calls.append(argv)
        if argv[0] == 'pdftotext':
            return 0, '\f'.join(self.pdf_text.get(Path(argv[-2]).name, [''])).encode() + b'\f'
        if argv[0] == 'pdftoppm':
            page, source, base = argv[argv.index('-f') + 1], Path(argv[-2]).name, Path(argv[-1])
            base.with_suffix('.pgm').write_text(f'{source}#{page}')      # l'« image » dit d'où elle vient
            return 0, b''
        if argv[0] == 'tesseract':
            if self.no_orientation and argv[argv.index('--psm') + 1] == '1':
                return 1, b''
            image = Path(argv[1])
            key = image.read_text() if image.name.startswith('page-') else image.name
            return 0, self.recognised.get(key, '').encode()
        raise AssertionError(argv)

    def count(self, program: str) -> int:
        return sum(1 for call in self.calls if call[0] == program)


class MechanicsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'Documents'
        self.root.mkdir()
        self.tools = FakeTools()

    def tearDown(self):
        self.tmp.cleanup()

    def library(self, ocr: bool = True) -> Library:
        return Library(Path(self.tmp.name) / 'bib.sqlite3', self.root, None, embed_model=None,
                       run=self.tools, ocr=ocr)

    def test_only_pages_without_text_are_recognised(self):
        pdf = self.root / 'mixte.pdf'
        pdf.write_bytes(b'%PDF-1.4')
        self.tools.pdf_text['mixte.pdf'] = ['Page un, avec du vrai texte lisible.', '', '  ']
        self.tools.recognised = {'mixte.pdf#2': 'Deuxième page, reconnue sur une image.', 'mixte.pdf#3': ''}
        result = extract(pdf, self.tools, 'fra+eng')
        self.assertEqual(result.pages[0], 'Page un, avec du vrai texte lisible.')
        self.assertEqual(result.pages[1], 'Deuxième page, reconnue sur une image.')
        self.assertEqual((result.ocr_pages, result.ocr_tried), (1, True))
        self.assertEqual(self.tools.count('pdftoppm'), 2)        # la page 1 n'est pas rendue en image
        plain = extract(pdf, self.tools, '')                     # reconnaissance indisponible
        self.assertEqual((plain.pages[1], plain.ocr_tried), ('', False))

    def test_a_long_scan_is_bounded(self):
        pdf = self.root / 'long.pdf'
        pdf.write_bytes(b'%PDF-1.4')
        self.tools.pdf_text['long.pdf'] = [''] * (library_module.MAX_OCR_PAGES + 15)
        extract(pdf, self.tools, 'fra+eng')
        self.assertEqual(self.tools.count('pdftoppm'), library_module.MAX_OCR_PAGES)

    def test_orientation_data_missing_falls_back(self):
        image = self.root / 'photo.jpg'
        image.write_bytes(b'\xff\xd8\xff')
        self.tools.recognised['photo.jpg'] = 'Texte lu sans redressement.'
        self.tools.no_orientation = True
        self.assertEqual(ocr_image(image, self.tools), 'Texte lu sans redressement.')
        self.assertEqual([c[c.index('--psm') + 1] for c in self.tools.calls], ['1', '3'])

    def test_a_page_wrongly_seen_as_empty_is_read_as_a_plain_block(self):
        image = self.root / 'ticket.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\n')
        tools = self.tools

        def picky(argv):          # l'analyse de page ne voit rien, la lecture en bloc simple si
            tools.calls.append(argv)
            return 0, ('Ticket lu en bloc simple, total 12,50 €'.encode() if argv[argv.index('--psm') + 1] == '6' else b'')
        self.assertIn('12,50', ocr_image(image, picky))
        self.assertEqual([c[c.index('--psm') + 1] for c in tools.calls], ['1', '3', '6'])

    def test_a_photo_becomes_a_searchable_document_flagged_as_recognised(self):
        (self.root / 'ticket.jpg').write_bytes(b'\xff\xd8\xff')
        self.tools.recognised['ticket.jpg'] = '\n'.join(line.lstrip('# ') for line in FACTURE)
        with self.library() as lib:
            stats = lib.scan(classify=False)
            self.assertEqual((stats['nouveaux'], stats['sans_texte']), (1, 0))
            doc = lib.documents()[0]
            self.assertEqual((doc['status'], doc['ocr']), ('indexé', 1))
            self.assertEqual(lib.stats()['reconnus'], 1)
            hit = lib.search('plaquettes de frein')[0]
            self.assertTrue(hit['ocr'])
            text, recognised = lib.text_of('ticket.jpg')
            self.assertIn('286,40', text)
            self.assertTrue(recognised)
            toolbox = Toolbox(Env(home=self.root, library=lib))
            read = toolbox.call('lire_document', {'chemin': 'ticket.jpg'})
            self.assertIn('texte reconnu automatiquement', read)
            self.assertIn('Montant total TTC : 286,40', read)
            self.assertIn('vérifier les chiffres', toolbox.call('rechercher_documents', {'question': 'frein'}))
            from nexus.assistant import cited_sources
            found = toolbox.call('rechercher_documents', {'question': 'frein'})
            self.assertEqual(cited_sources(found), ['ticket.jpg, page 1 (texte reconnu sur scan)'])
            self.assertEqual(cited_sources('[bail.pdf, page 2] (Bail)\nLoyer 890 €'), ['bail.pdf, page 2'])

    def test_scans_indexed_before_are_caught_up_once(self):
        (self.root / 'ancien.pdf').write_bytes(b'%PDF-1.4 scan')
        (self.root / 'blanc.pdf').write_bytes(b'%PDF-1.4 vide')
        (self.root / 'ticket.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        self.tools.recognised = {'ancien.pdf#1': 'Attestation de domicile, établie le 03/02/2026 à Annecy.',
                                 'ticket.png': 'Ticket de caisse : total 12,50 €, merci de votre visite.'}
        with self.library(ocr=False) as lib:           # avant : pas de reconnaissance sur ce PC
            lib.scan(classify=False)
            self.assertEqual({d['status'] for d in lib.documents()}, {'sans_texte'})
            self.assertEqual(self.tools.count('tesseract'), 0)
            answer = Toolbox(Env(home=self.root, library=lib)).call('lire_document', {'chemin': 'ticket.png'})
            self.assertIn('sans texte lisible', answer)      # l'IA reçoit l'explication, pas une invention
            self.assertIn('nexus docs indexer', answer)
        with self.library() as lib:                    # après installation de tesseract
            stats = lib.scan(classify=False)
            by_name = {d['path']: (d['status'], d['ocr']) for d in lib.documents()}
            self.assertEqual(by_name, {'ancien.pdf': ('indexé', 1), 'ticket.png': ('indexé', 1),
                                       'blanc.pdf': ('sans_texte', 2)})
            self.assertEqual(stats['modifiés'], 3)
            self.assertEqual(lib.search('attestation domicile')[0]['path'], 'ancien.pdf')
            before = self.tools.count('tesseract')
            again = lib.scan(classify=False)           # la page blanche n'est pas relue à chaque passage
            self.assertEqual((again['inchangés'], self.tools.count('tesseract')), (3, before))

    def test_text_of_removes_the_overlap_between_passages(self):
        lines = [f'Ligne numéro {n} du contrat, avec assez de mots pour remplir le passage.' for n in range(40)]
        (self.root / 'long.txt').write_text('\n'.join(lines), encoding='utf-8')
        with self.library() as lib:
            lib.scan(classify=False)
            text, recognised = lib.text_of('long.txt')
            self.assertFalse(recognised)
            for n in range(40):
                self.assertEqual(text.count(f'Ligne numéro {n} du'), 1, n)
            self.assertIsNone(lib.text_of('absent.txt'))
            self.assertIsNone(lib.text_of(Path('/etc/passwd')))

    def test_expenses_from_scans_are_flagged(self):
        (self.root / 'ticket.jpg').write_bytes(b'\xff\xd8\xff')
        self.tools.recognised['ticket.jpg'] = '\n'.join(FACTURE)
        with self.library() as lib:
            lib.scan(classify=False)
            lib.db.execute("UPDATE documents SET category='vehicule', amount_cents=28640, doc_date='2026-11-14'")
            lib.db.commit()
            report = Toolbox(Env(home=self.root, library=lib)).call('depenses', {})
            self.assertIn('286,40 €', report)
            self.assertIn('scan : montant à vérifier', report)


@unittest.skipUnless(REAL, 'tesseract (avec le français) ou pdftoppm absent')
class RealRecognitionTest(unittest.TestCase):
    def setUp(self):
        from tests.scans import rasterize, write_png, write_scan_pdf
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'Documents'
        self.root.mkdir()
        source = Path(self.tmp.name) / 'source.pdf'
        write_pdf(source, [FACTURE, ['Garantie', 'Les plaquettes sont garanties jusqu\'au 14/11/2028.']])
        pages = [rasterize(source, 1), rasterize(source, 2)]
        write_scan_pdf(self.root / 'scan garage.pdf', pages)
        write_png(self.root / 'photo garage.png', pages[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_scanned_pdf_and_photo_are_read_for_real(self):
        with Library(Path(self.tmp.name) / 'bib.sqlite3', self.root, None, embed_model=None) as lib:
            self.assertTrue(lib.ocr_languages)
            stats = lib.scan(classify=False)
            self.assertEqual((stats['nouveaux'], stats['sans_texte'], stats['erreurs']), (2, 0, 0))
            self.assertEqual({d['ocr'] for d in lib.documents()}, {1})
            text, recognised = lib.text_of('scan garage.pdf')
            self.assertTrue(recognised)
            self.assertIn('286,40', text)
            self.assertIn('14/11/2028', text)                     # la page 2 est lue aussi
            self.assertEqual(library_module.find_amount(text), 28640)
            self.assertEqual(library_module.find_date(text), '2026-11-14')
            self.assertEqual(lib.search('plaquettes garanties jusqu\'à quand')[0]['page'], 2)
            self.assertIn('Garage du Centre', lib.text_of('photo garage.png')[0])

    def test_a_text_pdf_is_not_recognised_needlessly(self):
        write_pdf(self.root / 'texte.pdf', [FACTURE])
        with Library(Path(self.tmp.name) / 'bib.sqlite3', self.root, None, embed_model=None) as lib:
            lib.scan(classify=False)
            self.assertEqual(lib.document(lib.db.execute("SELECT id FROM documents WHERE path='texte.pdf'")
                                          .fetchone()[0])['ocr'], 0)


if __name__ == '__main__':
    unittest.main()
