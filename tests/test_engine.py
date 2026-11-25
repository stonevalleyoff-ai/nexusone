"""Le moteur colibrì tenu par NEXUS : démarrage automatique, clé privée, veille, arrêt.

Le « colibrì » de ces tests est un faux lanceur (`c/coli`) qui sert le faux serveur des
autres tests : on vérifie la mécanique de NEXUS, pas le moteur lui-même.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from nexus import engine as engine_module
from nexus.config import Settings
from nexus.engine import Engine
from nexus.ollama import ModelInfo, OllamaError, Stats
from nexus.openai_compat import ManagedColibriClient, OpenAICompatClient

FAKE_COLI = r'''
import os, signal, sys, threading, time
args = sys.argv[1:]
if args[0] == 'setup':
    import json
    home = os.environ['COLI_SETUP_HOME']; os.makedirs(home, exist_ok=True)
    models = args[args.index('--dir') + 1]
    name = args[args.index('--model') + 1] if '--model' in args else 'recommande'
    os.makedirs(os.path.join(models, name), exist_ok=True)
    json.dump({'status': 'ready', 'launcher': os.path.abspath(__file__), 'env': {'FAUX_REGLAGE': 'oui'},
               'model_dir': os.path.join(models, name), 'model': {'id': name}},
              open(os.path.join(home, 'setup.json'), 'w'))
    sys.exit(0)
assert args[0] == 'serve', args
model = args[args.index('--model') + 1]
port = int(args[args.index('--port') + 1])
print('[faux colibri] chargement de', model, flush=True)
if os.path.exists(os.path.join(model, 'casse')):
    print('modèle illisible : fichier abîmé', flush=True)
    sys.exit(3)
from http.server import ThreadingHTTPServer
from tests.fake_openai import FakeColibri
fake = FakeColibri()
fake.api_key = os.environ['COLI_API_KEY']
fake.model_id = 'gros-modele'
fake.chunks = ['Bonjour', ' du', ' moteur']
handler = fake.server.RequestHandlerClass
fake.server.server_close()
server = ThreadingHTTPServer(('127.0.0.1', port), handler)
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
server.serve_forever()
'''


def make_colibri(root: Path, name: str = 'modele-test') -> tuple[Path, Path]:
    code, model = root / 'colibri', root / 'modeles' / name
    (code / 'c').mkdir(parents=True)
    (code / 'c' / 'coli').write_text(FAKE_COLI, encoding='utf-8')
    model.mkdir(parents=True)
    return code, model


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.code, self.model = make_colibri(self.root)
        self.base = self.root / 'moteur'
        self.engines: list[Engine] = []

    def tearDown(self):
        for engine in self.engines:
            engine.stop(10)
        self.tmp.cleanup()

    def engine(self, **options) -> Engine:
        options.setdefault('colibri_dir', self.code)
        options.setdefault('model_dir', self.model)
        options.setdefault('system_base', self.root / 'nulle-part')
        engine = Engine(self.base, **options)
        self.engines.append(engine)
        return engine

    def ask(self, client) -> str:
        info = ModelInfo(name='gros-modele', thinking=True, context_length=None, tools=True)
        return ''.join(item for item in client.chat_stream(info, [{'role': 'user', 'content': 'Salut'}],
                                                           num_ctx=4096) if isinstance(item, str))


class EngineTest(EngineCase):
    def test_starts_alone_at_the_first_question_and_stops(self):
        engine = self.engine()
        self.assertFalse(engine.running())
        seen: list[str] = []
        client = ManagedColibriClient(engine, timeout=30)
        client.on_progress = seen.append
        self.assertEqual(self.ask(client), 'Bonjour du moteur')
        self.assertTrue(engine.running() and engine.started_here)
        self.assertTrue(any('Démarrage du moteur' in text for text in seen))
        self.assertEqual(seen[-1], '')          # l'interface efface son message
        state = engine.state()
        self.assertTrue(engine.stop())
        self.assertFalse(engine.running())
        self.assertFalse(engine.state_path.exists())
        for pid in (state['superviseur'], state['moteur']):
            self.assertFalse(engine_module._pid_alive(pid))

    def test_the_key_is_random_private_and_required(self):
        engine = self.engine()
        url, key = engine.ensure()
        self.assertGreaterEqual(len(key), 32)
        self.assertEqual(stat.S_IMODE(engine.state_path.stat().st_mode), 0o600)
        stranger = OpenAICompatClient(url, 10)                      # un autre programme du PC, sans la clé
        with self.assertRaises(OllamaError):
            stranger.models()
        self.assertEqual([m['name'] for m in OpenAICompatClient(url, 10, api_key=key).models()], ['gros-modele'])
        engine.stop()
        _url2, key2 = engine.ensure()
        self.assertNotEqual(key, key2)                               # nouvelle clé à chaque démarrage

    def test_two_programs_share_one_engine(self):
        first, second = self.engine(), self.engine()
        url, _key = first.ensure()
        self.assertEqual(second.ensure()[0], url)
        self.assertFalse(second.started_here)
        second.release()                         # celui qui ne l'a pas démarré ne l'arrête pas
        self.assertTrue(first.running())
        first.release()
        self.assertFalse(first.running())

    def test_sleeps_when_unused_and_wakes_up_for_the_next_question(self):
        engine = self.engine(idle_minutes=0.03)                      # ~2 s
        client = ManagedColibriClient(engine, timeout=30)
        self.assertEqual(self.ask(client), 'Bonjour du moteur')
        deadline = time.monotonic() + 15
        while engine.running() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(engine.running(), 'le moteur aurait dû se mettre en veille')
        self.assertEqual(self.ask(client), 'Bonjour du moteur')     # relancé sans rien demander

    def test_a_failed_start_is_explained_and_not_retried_in_a_loop(self):
        (self.model / 'casse').touch()
        engine = self.engine()
        with self.assertRaises(OllamaError) as caught:
            engine.ensure()
        self.assertIn('fichier abîmé', str(caught.exception))
        self.assertIn('nexus moteur journal', str(caught.exception))
        with mock.patch.object(engine, '_spawn', side_effect=AssertionError('relance interdite')):
            with self.assertRaises(OllamaError):
                engine.ensure()
        self.assertTrue(any('abîmé' in line for line in engine.log_tail()))

    def test_instructions_and_tools_are_read_ahead_once_started(self):
        from nexus.assistant import Assistant
        from nexus.memory import Store
        from nexus.ollama import OllamaClient
        from nexus.tools import Toolbox
        engine = self.engine()
        client = ManagedColibriClient(engine, timeout=30)
        assistant = Assistant(client, Store(':memory:'), toolbox=Toolbox())
        self.assertTrue(assistant.warm_up('gros-modele'))
        url, key = engine.ensure()
        with self.assertRaises(OllamaError):     # le faux moteur n'a reçu qu'une requête, protégée par la clé
            OpenAICompatClient(url, 5).models()
        self.assertFalse(Assistant(OllamaClient('http://127.0.0.1:9', 5), Store(':memory:')).warm_up('x'))

    def test_a_stale_state_file_is_not_trusted(self):
        engine = self.engine()
        self.base.mkdir(parents=True)
        engine.state_path.write_text(json.dumps({'superviseur': os.getpid(), 'moteur': 1, 'port': 9,
                                                 'cle': 'vieille', 'depuis': 0}))
        self.assertFalse(engine.running())       # ce numéro est celui d'un autre programme (ce test)
        _url, key = engine.ensure()
        self.assertNotEqual(key, 'vieille')

    def test_simultaneous_starts_give_one_engine(self):
        import threading
        engines = [self.engine() for _ in range(4)]
        urls: list[str] = []
        threads = [threading.Thread(target=lambda e=e: urls.append(e.ensure()[0])) for e in engines]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(urls), 4)
        self.assertEqual(len(set(urls)), 1)
        self.assertEqual(sum(e.started_here for e in engines), 1)

    def test_not_installed_says_how_to_install(self):
        engine = Engine(self.base, system_base=self.root / 'nulle-part')
        self.assertIsNone(engine.installed())
        with self.assertRaises(OllamaError) as caught:
            ManagedColibriClient(engine, timeout=5).models()
        self.assertIn('nexus moteur installer', str(caught.exception))
        self.assertIn('pas installé', engine.describe()[0])


class InstallTest(EngineCase):
    def make_source(self) -> tuple[Path, str]:
        source = self.root / 'source'
        (source / 'c').mkdir(parents=True)
        (source / 'c' / 'coli').write_text(FAKE_COLI, encoding='utf-8')

        def git(*args):
            # Un dépôt jetable : ni l'identité ni la signature des commits de la machine ne doivent compter.
            return subprocess.run(['git', '-C', str(source), '-c', 'user.name=t', '-c', 'user.email=t@t',
                                   '-c', 'commit.gpgsign=false', *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('config', 'uploadpack.allowAnySHA1InWant', 'true')
        git('add', '.')
        git('commit', '-q', '-m', 'faux colibri')
        return source, git('rev-parse', 'HEAD')

    def test_install_fetches_the_pinned_version_then_nexus_uses_it(self):
        source, commit = self.make_source()
        engine = Engine(self.base, system_base=self.root / 'nulle-part')
        self.engines.append(engine)
        said: list[str] = []
        with mock.patch.object(engine_module, 'PINNED_COMMIT', commit):
            found = engine.install('petit', source=str(source), say=said.append)
            self.assertEqual(found.model_id, 'petit')
            self.assertEqual(found.launcher, self.base / 'colibri' / 'c' / 'coli')
            self.assertTrue(str(found.model_dir).startswith(str(self.base / 'modeles')))
            self.assertEqual(dict(found.env), {'FAUX_REGLAGE': 'oui'})
            engine.install('petit', source='/nulle/part', say=said.append)   # déjà à la bonne version : pas de réseau
        with mock.patch.dict(os.environ, {'NEXUS_MOTEUR': str(self.base), 'HOME': str(self.root)}, clear=False):
            os.environ.pop('NEXUS_BACKEND', None)
            os.environ.pop('NEXUS_COLIBRI_URL', None)
            settings = Settings.load()
            self.assertEqual(settings.backend, 'auto')               # tous les moteurs installés, sans réglage
            client = settings.open_client(timeout=30)
            self.engines.append(client.engine)
            self.assertEqual(client.big_model(), 'petit')            # le gros modèle est connu sans démarrer le moteur
            self.assertFalse(client.engine.running())
            answer = ''.join(item for item in client.chat_stream(client.show('petit'), [{'role': 'user', 'content': 'Salut'}],
                                                                 num_ctx=4096) if isinstance(item, str))
            self.assertEqual(answer, 'Bonjour du moteur')
            with mock.patch.dict(os.environ, {'NEXUS_BACKEND': 'ollama'}):
                self.assertEqual(Settings.load().backend, 'ollama')  # on peut toujours revenir à Ollama

    def test_a_wrong_version_is_refused(self):
        source, _commit = self.make_source()
        engine = Engine(self.base, system_base=self.root / 'nulle-part')
        with mock.patch.object(engine_module, 'PINNED_COMMIT', 'a' * 40):
            with self.assertRaises(OllamaError):
                engine.install(source=str(source), say=lambda _text: None)
        self.assertIsNone(engine.installed())

    def test_without_installation_nexus_stays_on_ollama(self):
        with mock.patch.dict(os.environ, {'NEXUS_MOTEUR': str(self.root / 'vide'), 'HOME': str(self.root)}):
            for name in ('NEXUS_BACKEND', 'NEXUS_COLIBRI_URL', 'NEXUS_COLIBRI_DIR'):
                os.environ.pop(name, None)
            settings = Settings.load()
            self.assertEqual(settings.backend, 'auto')
            self.assertIsNone(settings.open_client(timeout=5).big_model())      # pas de gros modèle : Ollama seul


if __name__ == '__main__':
    unittest.main()
