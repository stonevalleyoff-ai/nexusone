from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from nexus.assistant import Assistant, ToolUse, Turn
from nexus.library import Library
from nexus.memory import Store
from nexus.ollama import OllamaClient
from nexus.reports import bilan_energie, check_up, dossier_impots
from nexus.skills import BUILTIN, SkillBook, fill, match_key, validate
from nexus.tools import Env, Toolbox
from tests.corpus import build
from tests.fake_ollama import FakeOllama

TOOLS = set(Toolbox().tools)
GOOD = {'id': 'mon-wifi', 'nom': 'Mon Wi-Fi', 'description': 'Diagnostic Wi-Fi',
        'declencheurs': ['mon wifi plante'], 'etapes': [{'outil': 'etat_reseau', 'arguments': {}}],
        'consigne': 'Explique le problème.', 'statut': 'valide'}


class Recipes(unittest.TestCase):
    def test_builtin_recipes_are_valid(self):
        for skill in BUILTIN:
            with self.subTest(skill=skill.id):
                for step in skill.etapes:
                    self.assertIn(step.outil, TOOLS)
                self.assertIsNotNone(skill.rapport)

    def test_strict_validation(self):
        self.assertEqual(validate(GOOD, TOOLS).origine, 'utilisateur')
        for patch in ({'etapes': [{'outil': 'rm', 'arguments': {}}]}, {'code': 'import os'},
                      {'rapport': 'check_up'}, {'id': '../../etc'}, {'etapes': []},
                      {'etapes': [{'outil': 'etat_reseau', 'arguments': {'x': ['liste']}}]},
                      {'etapes': [{'outil': 'etat_reseau'}] * 7}, {'statut': 'root'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate({**GOOD, **patch}, TOOLS)

    def test_fill_placeholders(self):
        today = date(2026, 11, 21)
        self.assertEqual(fill('{annee}', 'q', today), 2026)
        self.assertEqual(fill('{annee_precedente}', 'q', today), 2025)
        self.assertEqual(fill('à propos de {question}', 'loyer', today), 'à propos de loyer')
        self.assertEqual(fill('{inconnu}', 'q', today), '{inconnu}')

    def test_matching(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = SkillBook(Path(tmp), TOOLS)
            self.assertEqual(book.match('Fais un check-up de mon PC').id, 'check-up-pc')
            self.assertEqual(book.match('lance le savoir-faire « Bilan énergie »').id, 'bilan-energie')
            self.assertIsNone(book.match('Bonjour'))
            self.assertEqual(match_key('Mon Wi-Fi, pourquoi ?'), 'mon wi fi pourquoi')

    def test_user_files_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'casse.json').write_text('{pas du json')
            (folder / 'check-up-pc.json').write_text(json.dumps({**GOOD, 'id': 'check-up-pc'}))
            book = SkillBook(folder, TOOLS)
            book.all()
            self.assertEqual(len(book.errors), 2)  # fichier cassé + identifiant réservé
            skill = book.propose_from('Mon wifi plante ?', [('etat_reseau', {}), ('firmwares_manquants', {})])
            self.assertEqual(skill.statut, 'propose')
            self.assertIsNone(book.match('Mon wifi plante ?'))  # pas avant validation
            self.assertTrue(book.confirm(skill.id))
            self.assertEqual(book.match('mon wifi plante').id, skill.id)
            self.assertFalse(book.delete('check-up-pc'))  # intégré : non supprimable
            self.assertTrue(book.delete(skill.id))
            with self.assertRaises(ValueError):
                book.propose_from('rien', [])


class Reports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / 'docs'
        build(root)
        (root / 'manifest.json').unlink()
        cls.fake = FakeOllama().__enter__()
        cls.fake.json_reply = None
        cls.lib = Library(Path(cls.tmp.name) / 'lib.sqlite3', root, OllamaClient(cls.fake.url),
                          embed_model='fake-embed:1', llm_model='qwen3:4b')
        # Classement réaliste par fichier, sans modèle : catégories du manifeste.
        manifest = {d['file']: d for d in __import__('tests.corpus', fromlist=['DOCS']).DOCS}
        cls.lib.scan(classify=False)
        for doc in cls.lib.documents():
            truth = manifest[doc['path']]
            title = 'Avis d\'impôt' if truth['kind'] == 'avis' and truth['category'] == 'impots' else truth['kind']
            from nexus.library import find_amount, find_date
            text = '\n'.join(t for (t,) in cls.lib.db.execute('SELECT text FROM chunks WHERE doc_id=?', (doc['id'],)))
            cls.lib.db.execute('UPDATE documents SET category=?, title=?, issuer=?, doc_date=?, amount_cents=? WHERE id=?',
                               (truth['category'], title, truth['issuer'], find_date(text), find_amount(text), doc['id']))
        cls.lib.db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()
        cls.fake.__exit__()
        cls.tmp.cleanup()

    def test_energy_totals_and_tempo_rules(self):
        text = bilan_energie({}, Env(library=self.lib), date(2026, 11, 21))
        self.assertIn('2026 : 614,34 € sur 3 facture(s)', text)
        self.assertIn('bleu 1740 kWh · blanc 505 kWh · rouge 255 kWh', text)
        self.assertIn('de 22h à 6h', text)  # heures creuses, jamais inversées

    def test_tax_checklist(self):
        text = dossier_impots({}, Env(library=self.lib), date(2026, 11, 21))
        self.assertIn('✅ Dernier avis d\'impôt', text)
        self.assertIn('❌ Bulletins de salaire 2025', text)
        self.assertIn('Revenu fiscal de référence (Avis_2025.pdf) : 38 420 €', text)
        text2027 = dossier_impots({}, Env(library=self.lib), date(2027, 4, 1))
        self.assertIn('✅ Bulletins de salaire 2026', text2027)

    def test_check_up_advice_from_rules(self):
        text = check_up({'erreurs_recentes': 'Erreurs :\n- a\n- b', 'firmwares_manquants': 'manque :\n- iwlwifi'},
                        Env(), date(2026, 11, 21))
        self.assertIn('2 type(s) d\'erreur', text)
        self.assertIn('1 firmware(s) manquant(s)', text)
        self.assertIn('## Conseils', text)


class InConversation(unittest.TestCase):
    def test_builtin_report_runs_without_model_and_save_flow(self):
        with FakeOllama() as fake, Store(':memory:') as store, tempfile.TemporaryDirectory() as tmp:
            toolbox = Toolbox(Env())
            assistant = Assistant(OllamaClient(fake.url), store, toolbox=toolbox,
                                  skills=SkillBook(Path(tmp), set(toolbox.tools)))
            conv = store.new_conversation()
            turn = Turn()
            items = list(assistant.reply(conv, 'qwen3:4b', 'Fais un check-up de mon PC', 'R', turn=turn))
            self.assertEqual(turn.skill, 'Check-up du PC')
            self.assertGreaterEqual(sum(isinstance(i, ToolUse) for i in items), 5)
            self.assertIn('# Check-up du PC', turn.text)
            self.assertIsNone(fake.last('/api/chat'))  # rapport calculé par le code, sans le modèle
            turn = Turn()
            reply = ''.join(i for i in assistant.reply(conv, 'qwen3:4b', 'Enregistre ça comme savoir-faire', 'R',
                                                       turn=turn) if isinstance(i, str))
            self.assertIn('recette', reply)
            self.assertEqual(assistant.skills.get(turn.skill_proposal).statut, 'propose')


if __name__ == '__main__':
    unittest.main()
