"""NEXUS choisit son modèle : mesures, règle de choix, réunion des deux moteurs."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from nexus.assistant import Assistant, Turn
from nexus.evaluation import QUESTIONS, evaluate
from nexus.fleet import Fleet
from nexus.memory import Store
from nexus.models import AUTO, Chooser, Measures, machine_signature
from nexus.ollama import ModelInfo, OllamaClient, OllamaError
from nexus.openai_compat import ManagedColibriClient
from nexus.tools import Toolbox
from tests.fake_ollama import FakeOllama
from tests.test_engine import EngineCase

GIB = 2**30
HERE = machine_signature(8, 32 * 1024)


class FakeClient:
    """Un client réduit à ce que le choix regarde : la liste des modèles et leur nature."""

    def __init__(self, models: list[tuple[str, float, str]], embedders: tuple[str, ...] = ('bge-m3:latest',)):
        self._models = [{'name': n, 'size': int(g * GIB), 'engine': e} for n, g, e in models]
        self.embedders = embedders

    def models(self):
        return list(self._models)

    def show(self, name: str) -> ModelInfo:
        return ModelInfo(name=name, thinking=False, context_length=None, tools=True, chat=name not in self.embedders)


class ChooserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'modeles.json'

    def tearDown(self):
        self.tmp.cleanup()

    def chooser(self, models, ram_gib: float = 32, forced=None, signature: str = HERE) -> Chooser:
        return Chooser(FakeClient(models), Measures(self.path, signature), ram_mib=int(ram_gib * 1024), forced=forced)

    MODELS = [('bge-m3:latest', 1.1, 'ollama'), ('qwen3:1.7b', 1.4, 'ollama'), ('qwen3.5:2b', 2.7, 'ollama'),
              ('inkling2-mini-iq2', 9.1, 'colibri')]

    def test_a_meaning_model_is_never_chosen_to_talk(self):
        chooser = self.chooser([('bge-m3:latest', 1.1, 'ollama'), ('qwen3:1.7b', 1.4, 'ollama')])
        self.assertEqual([c.name for c in chooser.candidates()], ['qwen3:1.7b'])     # avant : le 1er par ordre alphabétique
        self.assertEqual(chooser.pick('dialogue').name, 'qwen3:1.7b')
        self.assertIn('seul modèle', chooser.pick('dialogue').reason)
        with self.assertRaises(OllamaError) as caught:
            self.chooser([('bge-m3:latest', 1.1, 'ollama')]).pick()
        self.assertIn('ollama pull', str(caught.exception))

    def test_without_measures_fast_small_model_talks_and_the_big_one_works_in_the_background(self):
        chooser = self.chooser(self.MODELS)
        talk, deep = chooser.pick('dialogue'), chooser.pick('fond')
        self.assertEqual((talk.name, deep.name), ('qwen3.5:2b', 'inkling2-mini-iq2'))
        self.assertIn('inkling2-mini-iq2 est meilleur mais trop lent', talk.reason)
        self.assertIn('nexus modeles evaluer', talk.reason)          # c'est une estimation, et c'est dit

    def test_measures_on_this_machine_decide(self):
        measures = Measures(self.path, HERE)
        measures.save('qwen3:1.7b', {'outils': 0.62, 'essais': 8, 'secondes': 9.0, 'jetons_s': 12.0})
        measures.save('qwen3.5:2b', {'outils': 0.88, 'essais': 8, 'secondes': 11.0, 'jetons_s': 14.0})
        measures.save('inkling2-mini-iq2', {'outils': 1.0, 'essais': 8, 'secondes': 95.0, 'jetons_s': 7.4})
        chooser = self.chooser(self.MODELS)
        self.assertEqual(chooser.pick('dialogue').name, 'qwen3.5:2b')
        self.assertEqual(chooser.pick('fond').name, 'inkling2-mini-iq2')
        self.assertNotIn('estimation', chooser.pick('dialogue').reason)
        # Mesures égales sur le choix des outils : le petit pour dialoguer, le gros pour le fond.
        measures.save('qwen3.5:2b', {'outils': 1.0, 'essais': 8, 'secondes': 10.0, 'jetons_s': 17.0})
        tied = self.chooser(self.MODELS)
        self.assertEqual((tied.pick('dialogue').name, tied.pick('fond').name), ('qwen3.5:2b', 'inkling2-mini-iq2'))
        self.assertIn('le plus gros parmi les meilleurs', tied.pick('fond').reason)
        # Sur une machine plus forte, le gros modèle répond vite : il prend aussi le dialogue.
        measures.save('qwen3.5:2b', {'outils': 0.88, 'essais': 8, 'secondes': 11.0, 'jetons_s': 14.0})
        measures.save('inkling2-mini-iq2', {'outils': 1.0, 'essais': 8, 'secondes': 21.0, 'jetons_s': 30.0})
        self.assertEqual(self.chooser(self.MODELS).pick('dialogue').name, 'inkling2-mini-iq2')
        described = {c.name: c.describe() for c in self.chooser(self.MODELS).candidates()}
        self.assertIn('réponse type en 21 s', described['inkling2-mini-iq2'])

    def test_measures_from_another_computer_or_a_damaged_file_are_ignored(self):
        Measures(self.path, machine_signature(4, 8 * 1024)).save(
            'inkling2-mini-iq2', {'outils': 1.0, 'essais': 8, 'secondes': 5.0})       # clé USB venue d'un autre PC
        self.assertEqual(self.chooser(self.MODELS).pick('dialogue').name, 'qwen3.5:2b')
        self.path.write_text('{ceci n\'est pas du json')
        self.assertEqual(self.chooser(self.MODELS).pick('dialogue').name, 'qwen3.5:2b')
        self.path.write_text(json.dumps({'qwen3:1.7b': {'outils': 'beaucoup', 'machine': HERE}, 'x': 3}))
        self.assertEqual(self.chooser(self.MODELS).pick('dialogue').name, 'qwen3.5:2b')

    def test_a_model_too_big_for_the_memory_is_left_aside(self):
        models = [('petit:1b', 1.0, 'ollama'), ('enorme:70b', 40.0, 'ollama')]
        self.assertEqual(self.chooser(models, ram_gib=16).pick('fond').name, 'petit:1b')
        self.assertEqual(self.chooser(models, ram_gib=64).pick('fond').name, 'enorme:70b')
        alone = self.chooser([('enorme:70b', 40.0, 'ollama')], ram_gib=16)
        self.assertEqual(alone.pick('dialogue').name, 'enorme:70b')                  # rien d'autre : on essaie quand même
        self.assertIn('ne tient pas en mémoire', alone.candidates()[0].describe())
        # colibrì adapte sa mémoire : son gros modèle tient sur 16 Go, pas sur 4.
        big = [('qwen3:1.7b', 1.4, 'ollama'), ('qwen36-35b', 23.0, 'colibri')]
        self.assertEqual(self.chooser(big, ram_gib=16).pick('fond').name, 'qwen36-35b')
        self.assertEqual(self.chooser(big, ram_gib=4).pick('fond').name, 'qwen3:1.7b')
        # Rien ne tient (2 Go de mémoire) : on tente le moins gourmand, pas le meilleur.
        self.assertEqual(self.chooser(big, ram_gib=2).pick('fond').name, 'qwen3:1.7b')

    def test_a_forced_model_wins(self):
        chooser = self.chooser(self.MODELS, forced='qwen3:1.7b')
        self.assertEqual(chooser.pick('fond').name, 'qwen3:1.7b')
        self.assertIn('imposé', chooser.pick('dialogue').reason)

    def test_the_person_can_ask_to_take_the_time(self):
        chooser = self.chooser(self.MODELS)
        self.assertEqual(chooser.for_question('Bonjour, ça va ?').name, 'qwen3.5:2b')
        for question in ('Prends ton temps : compare mes deux contrats.', 'Fais une analyse détaillée de ma facture',
                         'Réfléchis bien avant de répondre'):
            self.assertEqual(chooser.for_question(question).name, 'inkling2-mini-iq2', question)
        self.assertEqual(chooser.for_question('Bonjour', deep=True).name, 'inkling2-mini-iq2')
        self.assertIn('approfondie', chooser.for_question('Bonjour', deep=True).reason)


class AssistantAutoTest(unittest.TestCase):
    def test_auto_picks_records_and_reports_the_model(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            fake.capabilities['bge-m3:latest'] = ['embedding']
            fake.tags.append({'name': 'bge-m3:latest', 'size': 1_100_000_000})
            client = OllamaClient(fake.url)
            chooser = Chooser(client, Measures(Path(tmp) / 'm.json', HERE), ram_mib=32 * 1024)
            store = Store(':memory:')
            assistant = Assistant(client, store, chooser=chooser)
            turn = Turn()
            conversation = store.new_conversation()
            text = ''.join(i for i in assistant.reply(conversation, AUTO, 'Bonjour ?', '', turn=turn) if isinstance(i, str))
            self.assertEqual(text, 'Bonjour !')
            self.assertEqual(turn.model, 'qwen3:4b')                # le plus gros des deux petits : estimé meilleur
            self.assertIn('estimation', turn.model_reason)
            self.assertEqual(fake.last('/api/chat')['model'], 'qwen3:4b')
            models = [row[0] for row in store.db.execute('SELECT model FROM messages')]
            self.assertEqual(models, ['qwen3:4b', 'qwen3:4b'])       # jamais « auto » dans l'historique
            self.assertEqual(assistant.resolve('llama3.2:1b', 'x'), ('llama3.2:1b', ''))
            with self.assertRaises(OllamaError):
                list(Assistant(client, Store(':memory:')).reply(1, AUTO, 'Bonjour ?', ''))

    def test_evaluation_measures_and_is_kept(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            client = OllamaClient(fake.url)
            steps: list[str] = []
            result = evaluate(client, 'qwen3:4b', Toolbox().schemas(), progress=steps.append)
            # Le faux modèle ne demande jamais d'outil : juste pour les 3 questions qui n'en veulent pas.
            self.assertEqual((result['outils'], result['essais']), (round(3 / len(QUESTIONS), 3), len(QUESTIONS)))
            self.assertGreaterEqual(result['secondes'], 0)
            self.assertEqual(result['jetons_s'], 2.0)
            self.assertIn(f'qwen3:4b : question {len(QUESTIONS)}/{len(QUESTIONS)}', steps)
            measures = Measures(Path(tmp) / 'modeles.json', HERE)
            measures.save('qwen3:4b', result)
            self.assertEqual(measures.get('qwen3:4b')['outils'], result['outils'])
            self.assertIsNone(measures.get('llama3.2:1b'))
            fake.capabilities['bge-m3:latest'] = ['embedding']
            with self.assertRaises(OllamaError):
                evaluate(client, 'bge-m3:latest', [])


class FleetTest(EngineCase):
    """Ollama et colibrì derrière une seule porte (faux Ollama, faux colibrì)."""

    def test_both_engines_are_listed_and_each_question_goes_to_its_engine(self):
        with FakeOllama() as fake:
            ollama = OllamaClient(fake.url)
            engine = self.engine()
            fleet = Fleet(ollama, ManagedColibriClient(engine, timeout=30, embedder=ollama))
            listed = {m['name']: m['engine'] for m in fleet.models()}
            self.assertEqual(listed, {'llama3.2:1b': 'ollama', 'qwen3:4b': 'ollama', 'modele-test': 'colibri'})
            self.assertFalse(engine.running())                       # lister ne démarre pas le gros moteur
            self.assertEqual(fleet.name, 'Ollama + colibrì')
            self.assertTrue(fleet.show('modele-test').tools)
            self.assertFalse(engine.running())

            def ask(model: str) -> str:
                return ''.join(i for i in fleet.chat_stream(fleet.show(model), [{'role': 'user', 'content': 'Salut'}],
                                                            num_ctx=4096) if isinstance(i, str))
            self.assertEqual(ask('qwen3:4b'), 'Bonjour !')
            self.assertFalse(engine.running())                       # un petit modèle ne réveille pas le gros
            self.assertEqual(ask('modele-test'), 'Bonjour du moteur')
            self.assertTrue(engine.running())
            self.assertTrue(fleet.reads_ahead_for('modele-test'))
            self.assertFalse(fleet.reads_ahead_for('qwen3:4b'))
            self.assertEqual(len(fleet.embed('fake-embed:1', ['un', 'deux'])), 2)
            engine.stop()
            self.assertEqual(ask('modele-test'), 'Bonjour du moteur')    # rendormi : relancé à la demande

    def test_without_ollama_the_big_model_is_enough(self):
        ollama = OllamaClient('http://127.0.0.1:9', 2)
        fleet = Fleet(ollama, ManagedColibriClient(self.engine(), timeout=30, embedder=ollama))
        self.assertEqual([m['name'] for m in fleet.models()], ['modele-test'])
        chooser = Chooser(fleet, Measures(None, HERE), ram_mib=32 * 1024)
        self.assertEqual(chooser.pick('dialogue').name, 'modele-test')

    def test_without_a_big_model_it_is_plain_ollama(self):
        from nexus.engine import Engine
        with FakeOllama() as fake:
            ollama = OllamaClient(fake.url)
            nothing = Engine(self.root / 'vide', system_base=self.root / 'nulle-part')
            fleet = Fleet(ollama, ManagedColibriClient(nothing, timeout=5, embedder=ollama))
            self.assertEqual((fleet.name, fleet.big_model()), ('Ollama', None))
            self.assertEqual([m['name'] for m in fleet.models()], ['llama3.2:1b', 'qwen3:4b'])
        with self.assertRaises(OllamaError):
            fleet.models()                                           # Ollama arrêté et rien d'autre : c'est dit


if __name__ == '__main__':
    unittest.main()
