import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from nexus.assistant import Assistant, ToolUse, Turn
from nexus.config import Settings
from nexus.library import Library, extract_json
from nexus.memory import Store
from nexus.ollama import Cancelled, ModelInfo, OllamaClient, OllamaError, Stats, ToolCalls
from nexus.openai_compat import OpenAICompatClient, to_openai
from nexus.tools import Env, Toolbox
from tests.fake_ollama import FakeOllama
from tests.fake_openai import FakeColibri
from tests.test_tools import fake_system

INFO = ModelInfo('qwen36-35b', True, 32768, True)


class Conversion(unittest.TestCase):
    def test_tool_round_trip_and_system_placement(self):
        messages = [
            {'role': 'system', 'content': 'A'}, {'role': 'system', 'content': 'B'},
            {'role': 'user', 'content': 'Q'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': 'stockage', 'arguments': {}}},
                {'function': {'name': 'materiel', 'arguments': {'categorie': 'usb'}}}]},
            {'role': 'tool', 'tool_name': 'stockage', 'content': 'R1'},
            {'role': 'tool', 'tool_name': 'materiel', 'content': 'R2'},
            {'role': 'system', 'content': 'CONSIGNE'}]
        out = to_openai(messages)
        self.assertEqual([m['role'] for m in out], ['system', 'user', 'assistant', 'tool', 'tool', 'user'])
        self.assertEqual(out[0]['content'], 'A\n\nB')
        calls = out[2]['tool_calls']
        self.assertEqual(calls[1]['function']['arguments'], '{"categorie": "usb"}')
        self.assertEqual([m['tool_call_id'] for m in out[3:5]], [c['id'] for c in calls])
        self.assertEqual(out[5]['content'], 'CONSIGNE')

    def test_flattened_for_engines_without_tools(self):
        out = to_openai([{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'Q'},
                         {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'stockage'}}]},
                         {'role': 'tool', 'tool_name': 'stockage', 'content': 'R1'},
                         {'role': 'system', 'content': 'CONSIGNE'}], native_tools=False)
        self.assertEqual([m['role'] for m in out], ['system', 'user'])
        self.assertIn('[stockage]\nR1', out[1]['content'])
        self.assertTrue(out[1]['content'].startswith('Q'))
        self.assertTrue(out[1]['content'].endswith('CONSIGNE'))


class AgainstFakeColibri(unittest.TestCase):
    def test_local_only(self):
        for bad in ('https://api.openai.com', 'http://192.168.1.10:8000', 'http://colibri.example:8000'):
            with self.subTest(bad=bad), self.assertRaises(OllamaError):
                OpenAICompatClient(bad)

    def test_models_show_stream(self):
        with FakeColibri() as fake:
            client = OpenAICompatClient(fake.url)
            self.assertEqual(client.models(), [{'name': 'qwen36-35b', 'size': 0}])  # sans le modèle d'images
            info = client.show('qwen36-35b')
            self.assertEqual((info.context_length, info.tools), (32768, True))
            with self.assertRaises(OllamaError):
                client.show('absent')
            self.assertEqual(client.version(), 'moteur qwen36')
            items = list(client.chat_stream(info, [{'role': 'user', 'content': 'salut'}], num_ctx=4096))
            self.assertEqual(items[:-1], ['Bon', 'jour', ' !'])
            self.assertIsInstance(items[-1], Stats)
            self.assertEqual(items[-1].tokens, 3)
            body = fake.chats()[-1]
            self.assertIs(body['enable_thinking'], False)
            self.assertEqual(body['max_tokens'], 1024)

    def test_tool_calls_parsed(self):
        with FakeColibri() as fake:
            fake.tool_calls = [('stockage', {}), ('materiel', {'categorie': 'usb'})]
            tools = Toolbox().schemas()
            items = list(OpenAICompatClient(fake.url).chat_stream(INFO, [{'role': 'user', 'content': 'x'}],
                                                                  num_ctx=4096, tools=tools))
            calls = next(i for i in items if isinstance(i, ToolCalls)).calls
            self.assertEqual(calls[1], {'function': {'name': 'materiel', 'arguments': {'categorie': 'usb'}}})

    def test_api_key_and_explained_errors(self):
        with FakeColibri() as fake:
            fake.api_key = 'secret-local'
            with self.assertRaisesRegex(OllamaError, 'NEXUS_COLIBRI_KEY'):
                OpenAICompatClient(fake.url).models()
            self.assertTrue(OpenAICompatClient(fake.url, api_key='secret-local').models())
            fake.api_key = None
            for status, text in ((429, 'occupé'), (503, 'redémarre')):
                fake.status = status
                with self.subTest(status=status), self.assertRaisesRegex(OllamaError, text):
                    list(OpenAICompatClient(fake.url).chat_stream(INFO, [{'role': 'user', 'content': 'x'}], num_ctx=1))

    def test_abort_while_model_reads_the_prompt(self):
        with FakeColibri() as fake:
            fake.delay = 5
            client, stop, outcome = OpenAICompatClient(fake.url), threading.Event(), []

            def run():
                try:
                    list(client.chat_stream(INFO, [{'role': 'user', 'content': 'x'}], num_ctx=1, cancelled=stop.is_set))
                    outcome.append('fini')
                except Cancelled:
                    outcome.append('annulé')

            worker = threading.Thread(target=run)
            worker.start()
            time.sleep(0.5)
            stop.set()
            client.abort()
            worker.join(3)
            self.assertEqual(outcome, ['annulé'])

    def test_embeddings_go_through_ollama(self):
        with FakeColibri() as fake, FakeOllama() as ollama:
            with self.assertRaisesRegex(OllamaError, 'Ollama'):
                OpenAICompatClient(fake.url).embed('bge-m3', ['a'])
            client = OpenAICompatClient(fake.url, embedder=OllamaClient(ollama.url))
            self.assertEqual(len(client.embed('fake-embed:1', ['un texte', 'deux'])), 2)


class WithAssistant(unittest.TestCase):
    def test_planned_tools_then_answer_respect_server_rules(self):
        """Le faux serveur refuse un message système mal placé, comme colibrì : toute la chaîne doit passer."""
        with FakeColibri() as fake, Store(':memory:') as store, tempfile.TemporaryDirectory() as tmp:
            fake.tool_calls = [('stockage', {})]
            assistant = Assistant(OpenAICompatClient(fake.url), store, toolbox=Toolbox(fake_system(Path(tmp))))
            turn = Turn()
            items = list(assistant.reply(store.new_conversation(), 'qwen36-35b', 'Mon Wi-Fi ne marche pas', 'RELEVÉ',
                                         turn=turn))
            used = [i.name for i in items if isinstance(i, ToolUse)]
            self.assertEqual(used, ['etat_reseau', 'firmwares_manquants', 'stockage'])  # aiguillage, puis le modèle
            self.assertEqual(turn.text, 'Bonjour !')
            last = fake.chats()[-1]['messages']
            self.assertEqual(last[0]['role'], 'system')
            self.assertNotIn('system', [m['role'] for m in last[1:]])
            self.assertIn('BLOQUÉ', next(m['content'] for m in last if m['role'] == 'tool'))

    def test_engine_without_tools_gets_results_as_text(self):
        with FakeColibri() as fake, Store(':memory:') as store, tempfile.TemporaryDirectory() as tmp:
            fake.tools_supported, fake.arch = False, 'olmoe'
            client = OpenAICompatClient(fake.url)
            assistant = Assistant(client, store, toolbox=Toolbox(fake_system(Path(tmp))))
            conv = store.new_conversation()
            turn = Turn()
            list(assistant.reply(conv, 'qwen36-35b', 'Mon Wi-Fi ne marche pas', 'R', turn=turn))
            self.assertEqual(turn.text, 'Bonjour !')
            accepted = fake.chats()[-1]
            self.assertNotIn('tools', accepted)
            self.assertIn('BLOQUÉ', accepted['messages'][-1]['content'])  # résultat donné comme texte
            before = len(fake.chats())
            list(assistant.reply(conv, 'qwen36-35b', 'Il reste de la place sur le disque ?', 'R'))
            self.assertEqual(len(fake.chats()), before + 1)  # refus mémorisé : pas de nouvel essai inutile

    def test_classification_falls_back_without_json_schema(self):
        with FakeColibri() as fake, FakeOllama() as ollama, tempfile.TemporaryDirectory() as tmp:
            fake.format_supported = False
            fake.chunks = ['Voici :\n```json\n{"categorie": "energie", "titre": "Facture", "emetteur": "Voltéa",',
                           ' "date": "2026-03-05", "montant": 12.5, "resume": "x"}\n```']
            root = Path(tmp) / 'docs'
            root.mkdir()
            (root / 'facture.txt').write_text('Facture Voltéa\nDate : 05/03/2026\nMontant total TTC : 268,92 €\n')
            client = OpenAICompatClient(fake.url, embedder=OllamaClient(ollama.url))
            with Library(Path(tmp) / 'l.sqlite3', root, client, embed_model='fake-embed:1',
                         llm_model='qwen36-35b') as lib:
                lib.scan()
                doc = lib.documents()[0]
                self.assertEqual((doc['category'], doc['amount_cents']), ('energie', 26892))
            self.assertIn('response_format', fake.chats()[0])
            self.assertNotIn('response_format', fake.chats()[1])


class Degraded(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(extract_json('texte {"a": 1} suite'), {'a': 1})
        self.assertEqual(extract_json('{cassé} puis {"b": {"c": 2}}'), {'b': {'c': 2}})
        self.assertIsNone(extract_json('rien'))
        self.assertIsNone(extract_json('[1, 2]'))

    def test_document_search_survives_missing_embedder(self):
        with FakeColibri() as fake, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'docs'
            root.mkdir()
            (root / 'bail.txt').write_text('Le dépôt de garantie est de 890 euros pour cet appartement.\n')
            with Library(Path(tmp) / 'l.sqlite3', root, OpenAICompatClient(fake.url), embed_model='bge-m3') as lib:
                self.assertEqual(lib.scan(classify=False)['erreurs'], 0)
                results = lib.search('dépôt de garantie')
                self.assertEqual(results[0]['path'], 'bail.txt')
                self.assertTrue(lib.degraded)


class Backend(unittest.TestCase):
    def test_settings_choose_the_client(self):
        with mock.patch.dict(os.environ, {'NEXUS_BACKEND': 'colibri', 'NEXUS_COLIBRI_URL': 'http://127.0.0.1:8123'}):
            client = Settings.load().open_client()
            self.assertIsInstance(client, OpenAICompatClient)
            self.assertEqual(client.base_url, 'http://127.0.0.1:8123')
            self.assertIsInstance(client.embedder, OllamaClient)
        with mock.patch.dict(os.environ, {'NEXUS_BACKEND': ''}):
            self.assertNotIsInstance(Settings.load().open_client(), OpenAICompatClient)


if __name__ == '__main__':
    unittest.main()
