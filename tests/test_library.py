import os
from pathlib import Path
import tempfile
import unittest

from nexus.library import (Library, chunk_pages, find_amount, find_date, money, parse_amount,
                           parse_date, safe_name)
from nexus.ollama import OllamaClient
from nexus.router import expense_filters, normalize, plan
from nexus.tools import Env, Toolbox
from tests.corpus import build
from tests.fake_ollama import FakeOllama
from tests.pdfgen import write_pdf

CLASSIFIED = {'categorie': 'energie', 'titre': 'Facture électricité', 'emetteur': 'Voltéa Énergie',
              'date': '2026-03-05', 'montant': 268.92, 'resume': 'Facture.'}


class Extraction(unittest.TestCase):
    def test_dates_and_amounts(self):
        self.assertEqual(find_date('Réf 12\nDate : 05/03/2026\nÉchéance 20/03/2026'), '2026-03-05')
        self.assertEqual(find_date('Émis le 1er juillet 2024'), '2024-07-01')
        self.assertIsNone(find_date('rien ici 45/99/2026'))
        self.assertEqual(find_amount('Montant total TTC : 1 099,00 €'), 109900)
        self.assertEqual(find_amount('Net à payer avant impôt : 2 812,40 €\nNet payé : 2 640,18 €'), 264018)
        self.assertEqual(find_amount('Cotisation annuelle 2026 : 612,00 €'), 61200)
        self.assertIsNone(find_amount('Solde au 31/03/2026 : 3 104,71 €'))
        self.assertEqual(parse_amount('2 914,00'), 291400)
        self.assertIsNone(parse_amount(True))
        self.assertEqual(parse_date('2026-02-30'), None)
        self.assertEqual(money(268892), '2 688,92 €')

    def test_safe_names_cannot_escape(self):
        self.assertNotIn('/', safe_name('../../etc/passwd'))
        self.assertNotIn('\\\\', safe_name('a\\\\b'))
        self.assertEqual(safe_name('   '), 'Document')

    def test_chunks_keep_page_numbers_and_overlap(self):
        chunks = chunk_pages(['court', 'mot ' * 600])
        self.assertEqual(chunks[0], (1, 'court'))
        self.assertTrue(all(page == 2 for page, _ in chunks[1:]))
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(text) <= 700 for _, text in chunks))


class LibraryOnCorpus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'Documents'
        build(self.root)
        (self.root / 'manifest.json').unlink()
        (self.root / '.cache').mkdir()
        write_pdf(self.root / '.cache' / 'secret.pdf', [['SECRET']])
        os.symlink('/etc/os-release', self.root / 'lien.txt')
        self.fake = FakeOllama().__enter__()
        self.lib = Library(Path(self.tmp.name) / 'lib.sqlite3', self.root, OllamaClient(self.fake.url),
                           embed_model='fake-embed:1', llm_model=None)

    def tearDown(self):
        self.lib.close()
        self.fake.__exit__()
        self.tmp.cleanup()

    def test_scan_and_rescan(self):
        stats = self.lib.scan(classify=False)
        self.assertEqual(stats['nouveaux'], 26)
        self.assertEqual(stats['sans_texte'], 1)
        paths = {d['path'] for d in self.lib.documents()}
        self.assertNotIn('.cache/secret.pdf', paths)  # dossiers cachés ignorés
        self.assertNotIn('lien.txt', paths)           # liens symboliques ignorés
        self.assertEqual(self.lib.scan(classify=False)['inchangés'], 26)
        write_pdf(self.root / 'devis.pdf', [['Devis modifié : 999,00 €']])
        (self.root / 'mobile.pdf').unlink()
        stats = self.lib.scan(classify=False)
        self.assertEqual((stats['modifiés'], stats['retirés']), (1, 1))

    def test_search_cites_file_and_page(self):
        self.lib.scan(classify=False)
        results = self.lib.search('résiliation sans frais demande écrite', k=3)
        self.assertEqual((results[0]['path'], results[0]['page']), ('conditions_tempo.pdf', 2))
        exact = self.lib.search('facture IP-88271', k=1)
        self.assertEqual(exact[0]['path'], 'commande_88271.pdf')

    def test_classify_propose_apply_undo(self):
        # Le modèle se trompe de date et de montant : les règles, fiables, l'emportent.
        self.fake.json_reply = dict(CLASSIFIED, date='2020-01-01', montant=1.0)
        self.lib.llm_model = 'qwen3:4b'
        self.lib.scan()
        doc = next(d for d in self.lib.documents() if d['path'] == 'facture_2026_02_88214.pdf')
        self.assertEqual((doc['category'], doc['doc_date'], doc['amount_cents']), ('energie', '2026-03-05', 26892))
        self.assertTrue(doc['proposal'].startswith('Énergie/2026/2026-03-05 Voltéa Énergie'))
        # Un fichier existe déjà à la destination : pas d'écrasement.
        target = self.root / doc['proposal']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'deja la')
        moved = dict(self.lib.apply())
        self.assertTrue(moved['facture_2026_02_88214.pdf'].endswith(' (2).pdf'))
        self.assertEqual(target.read_bytes(), b'deja la')
        self.assertFalse((self.root / 'facture_2026_02_88214.pdf').exists())
        self.assertEqual(self.lib.scan(classify=False)['inchangés'], 26)
        target.unlink()
        self.assertEqual(len(self.lib.undo()), len(moved))
        self.assertTrue((self.root / 'facture_2026_02_88214.pdf').exists())
        self.assertFalse((self.root / 'Énergie' / '2026').exists())  # dossiers vides retirés

    def test_tools_and_expenses(self):
        self.fake.json_reply = CLASSIFIED
        self.lib.llm_model = 'qwen3:4b'
        self.lib.scan()
        box = Toolbox(Env(home=self.root, library=self.lib))
        found = box.call('rechercher_documents', {'question': 'combien de jours rouges par an'})
        self.assertIn('[conditions_tempo.pdf, page 1]', found)
        spent = box.call('depenses', {'categorie': 'energie', 'annee': 2026})
        self.assertIn('total', spent)
        self.assertIn('268,92 €', spent)
        empty = Toolbox(Env(home=self.root)).call('rechercher_documents', {'question': 'loyer'})
        self.assertIn('non indexée', empty)


class Routing(unittest.TestCase):
    def test_expense_questions(self):
        self.assertEqual(expense_filters(normalize("Combien j'ai payé d'électricité en 2025 ?")),
                         {'categorie': 'energie', 'annee': 2025})
        names = [n for n, _ in plan("Combien j'ai payé de loyer l'an dernier ?")]
        self.assertEqual(names[0], 'depenses')
        searched = dict(plan('Quand se termine mon assurance habitation ?'))
        self.assertEqual(searched['rechercher_documents']['question'], 'Quand se termine mon assurance habitation ?')


if __name__ == '__main__':
    unittest.main()
