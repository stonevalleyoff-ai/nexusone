"""Non-régression des défauts trouvés par la relecture indépendante (novembre 2026)."""
from datetime import date, datetime
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from nexus.assistant import Assistant, Turn
from nexus.library import Library, find_amount
from nexus.memory import Store
from nexus.ollama import OllamaClient
from nexus.openai_compat import OpenAICompatClient
from nexus.planner import Planner, Runner
from nexus.reports import _tempo_kwh
from nexus.skills import SAVE_RE, SkillBook
from nexus.souvenirs import Souvenirs, is_secret, parse_command
from nexus.telemetry import Sample
from nexus.tools import Env, Toolbox
from nexus.watch import DEFAULT_RULES, Watcher, load_rules
from tests.fake_ollama import FakeOllama
from tests.test_planner import original_tasks_only
from tests.fake_openai import FakeColibri

CLASSIFIED = {'categorie': 'energie', 'titre': 'Facture', 'emetteur': 'Voltéa', 'date': '2026-03-05',
              'montant': 1.0, 'resume': 'x'}


def library(tmp: str, fake: FakeOllama, files: dict[str, str], llm: str | None = 'qwen3:4b') -> Library:
    root = Path(tmp) / 'docs'
    root.mkdir(exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text)
    return Library(Path(tmp) / 'lib.sqlite3', root, OllamaClient(fake.url), embed_model='fake-embed:1', llm_model=llm)


class Rangement(unittest.TestCase):
    def test_undo_is_per_batch_and_never_blocks(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            fake.json_reply = CLASSIFIED
            lib = library(tmp, fake, {'a.txt': 'Facture A\nDate : 05/03/2026\n', 'b.txt': 'Facture B\nDate : 06/03/2026\n'})
            lib.scan()
            ids = {Path(d['path']).name: d['id'] for d in lib.documents()}
            first = lib.apply([ids['a.txt']])
            second = lib.apply([ids['b.txt']])
            self.assertEqual((len(first), len(second)), (1, 1))
            (lib.root / second[0][1]).unlink()            # le fichier du dernier lot disparaît à la main
            self.assertEqual(lib.undo(), [])              # ce lot n'est plus annulable…
            self.assertEqual(lib.not_undone, [second[0][1]])
            self.assertEqual(lib.undo(), [(first[0][1], 'a.txt')])   # … mais ne bloque pas le précédent
            self.assertTrue((lib.root / 'a.txt').is_file())
            lib.close()

    def test_a_batch_is_undone_whole_even_across_seconds(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            fake.json_reply = CLASSIFIED
            lib = library(tmp, fake, {f'f{i}.txt': f'Facture {i}\nDate : 0{i}/03/2026\n' for i in range(1, 5)})
            lib.scan()
            self.assertEqual(len(lib.apply()), 4)
            lib.db.execute("UPDATE moves SET moved_at='2020-01-01T00:00:00+00:00' WHERE id IN (1, 2)")
            lib.db.commit()
            self.assertEqual(len(lib.undo()), 4)
            lib.close()

    def test_interrupted_move_is_repaired_at_startup_and_nothing_is_overwritten(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            fake.json_reply = CLASSIFIED
            lib = library(tmp, fake, {'a.txt': 'Facture A\nDate : 05/03/2026\n'})
            lib.scan()
            doc = lib.documents()[0]
            target = lib.root / doc['proposal']
            target.parent.mkdir(parents=True)
            # Panne juste après le déplacement : le journal dit « prévu », le fichier est déjà parti.
            lib.db.execute("INSERT INTO moves(doc_id, source, target, moved_at, undone, lot) VALUES (?,?,?,?,2,'x')",
                           (doc['id'], 'a.txt', doc['proposal'], 'maintenant'))
            lib.db.commit()
            os.rename(lib.root / 'a.txt', target)
            lib.close()
            lib = library(tmp, fake, {})
            self.assertEqual(lib.documents()[0]['path'], doc['proposal'])
            self.assertEqual(lib.undo(), [(doc['proposal'], 'a.txt')])
            # Le nom d'arrivée est pris entre-temps : jamais d'écrasement.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('autre fichier')
            moved = lib._move_no_replace(lib.root / 'a.txt', target)
            self.assertNotEqual(moved, target)
            self.assertEqual(target.read_text(), 'autre fichier')
            lib.close()


class Documents(unittest.TestCase):
    def test_amounts(self):
        cases = {'Total TTC : 45.90 €': 4590, 'Montant total TTC : 1.234,56 €': 123456,
                 'Montant à payer : 1 234.56 €': 123456,
                 'Total HT : 100,00 €\nTVA : 20,00 €\nNet à payer : 120,00 €': 12000,
                 'Net à payer avant impôt : 2 812,40 €\nNet payé : 2 640,18 €': 264018}
        for text, cents in cases.items():
            with self.subTest(text=text):
                self.assertEqual(find_amount(text), cents)

    def test_kwh_lines_in_overlapping_passages_count_once(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            filler = '\n'.join(f'Ligne de détail numéro {i} de la facture' for i in range(14))
            text = f'{filler}\nJours bleus : 300 kWh\nJours blancs : 100 kWh\nJours rouges : 50 kWh\n{filler}\n'
            lib = library(tmp, fake, {'facture.txt': text}, llm=None)
            lib.scan(classify=False)
            lib.db.execute("UPDATE documents SET category='energie', doc_date='2026-03-05'")
            lib.db.commit()
            # La ligne « Jours rouges » tombe dans le chevauchement : elle figure dans deux passages.
            self.assertGreater(lib.db.execute("SELECT COUNT(*) FROM chunks WHERE text LIKE '%Jours rouges%'").fetchone()[0], 1)
            self.assertEqual(dict(_tempo_kwh(lib)['2026']), {'bleu': 300, 'blanc': 100, 'rouge': 50})
            lib.close()

    def test_unclassified_documents_are_classified_on_next_scan(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            fake.json_reply = CLASSIFIED
            lib = library(tmp, fake, {'a.txt': 'Facture A\nDate : 05/03/2026\n'})
            lib.scan(classify=False)
            self.assertIsNone(lib.documents()[0]['category'])
            self.assertEqual(lib.scan()['classés'], 1)
            self.assertEqual(lib.documents()[0]['category'], 'energie')
            lib.close()

    def test_search_sees_documents_indexed_by_another_process(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            ui = library(tmp, fake, {'a.txt': 'Le bail de location prévoit un dépôt de garantie.\n'}, llm=None)
            ui.scan(classify=False)
            self.assertEqual(ui.search('bail location')[0]['path'], 'a.txt')
            cli = library(tmp, fake, {'b.txt': 'Ordonnance : amoxicilline, antibiotique pendant six jours.\n'}, llm=None)
            cli.scan(classify=False)
            self.assertEqual(ui.search('ordonnance amoxicilline antibiotique')[0]['path'], 'b.txt')
            ui.close()
            cli.close()

    def test_cited_paths_can_be_read_back(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            (home / 'Documents' / 'Factures').mkdir(parents=True)
            (home / 'Documents' / 'Factures' / 'edf.txt').write_text('Montant total TTC : 45,90 €\n')
            lib = Library(Path(tmp) / 'l.sqlite3', home / 'Documents', OllamaClient(fake.url), embed_model='fake-embed:1')
            lib.scan(classify=False)
            box = Toolbox(Env(home=home, library=lib))
            self.assertIn('[Factures/edf.txt, page 1]', box.call('rechercher_documents', {'question': 'montant total'}))
            self.assertIn('45,90', box.call('lire_document', {'chemin': 'Factures/edf.txt'}))
            self.assertTrue(box.call('lire_document', {'chemin': '../../etc/passwd'}).startswith('ERREUR'))
            lib.close()


class Memoire(unittest.TestCase):
    def test_without_embeddings_one_shared_word_changes_nothing(self):
        mem = Souvenirs(':memory:', None)
        mem.add('Ma voiture est une Renault Zoé de 2019, garée chez Paul')
        self.assertEqual(mem.add('Ma voiture a 5 ans')[1], 'ajouté')
        self.assertEqual(len(mem.list()), 2)
        self.assertIsNone(mem.forget('le mot voiture'))
        self.assertEqual(len(mem.list()), 2)

    def test_commands_are_narrow(self):
        for text in ('Retiens quelque chose de drôle sur les chats', 'Note : le serveur plante, pourquoi ?',
                     'Oublie de me répondre en anglais', 'Supprime des doublons dans cette liste',
                     'Efface de ce message le mot voiture'):
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text))
        self.assertEqual(parse_command('Oublie le souvenir sur la voiture').kind, 'forget')
        self.assertFalse(SAVE_RE.search('Crée une recette de crêpes pour 4 personnes'))
        book = SkillBook(None, set(Toolbox().tools))
        self.assertIsNone(book.match('Pourquoi ma consommation de mémoire est si élevée ?'))
        self.assertIsNone(book.match("Quel est l'état du PC de ma sœur ?"))

    def test_secret_filter_and_no_trace_of_a_refused_secret(self):
        for text in ('Le code du garage est 4821', 'La clé wifi est abc', 'Mon code est 1234',
                     "Le digicode de l'immeuble est 12A45", '4970.1234.5678.9012', '4970  1234  5678  9012',
                     'BE68 5390 0754 7034'):
            with self.subTest(text=text):
                self.assertTrue(is_secret(text))
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'n.sqlite3')
            mem = Souvenirs(Path(tmp) / 's.sqlite3', OllamaClient(fake.url), embed_model='fake-embed:1')
            assistant = Assistant(OllamaClient(fake.url), store, souvenirs=mem)
            reply = ''.join(assistant.reply(store.new_conversation(), 'llama3.2:1b',
                                            'Retiens que mon mot de passe wifi est Tr0ub4dor&3', 'R'))
            self.assertIn('ne retiens pas', reply)
            pid = mem.propose('Je déteste le fromage de chèvre')
            mem.reject(pid)
            store.close()
            mem.close()
            for name in ('n.sqlite3', 's.sqlite3'):
                data = (Path(tmp) / name).read_bytes()
                self.assertNotIn(b'Tr0ub4dor', data)
                self.assertNotIn('fromage de ch'.encode(), data)


class Taches(unittest.TestCase):
    def test_schedule_change_does_not_rerun_the_same_day(self):
        planner = Planner(':memory:')
        original_tasks_only(planner)
        planner.set_active('echeances', False)
        planner.set_active('veille-tempo', True, now=datetime(2026, 11, 20, 12, 0))
        runner = Runner(planner, tempo_fetch=lambda: (date(2026, 11, 22), 'bleu'))
        self.assertEqual(runner.tick(datetime(2026, 11, 21, 19, 30)), ['veille-tempo'])
        planner.set_schedule('veille-tempo', {'type': 'quotidien', 'heure': '19:45'}, now=datetime(2026, 11, 21, 20, 0))
        self.assertEqual(runner.tick(datetime(2026, 11, 21, 20, 1)), [])
        self.assertEqual(runner.tick(datetime(2026, 11, 22, 19, 46)), ['veille-tempo'])

    def test_clock_in_the_future_does_not_block_a_task(self):
        planner = Planner(':memory:')
        original_tasks_only(planner)
        planner.set_active('echeances', False)
        planner.set_active('veille-tempo', True, now=datetime(2030, 1, 1, 12, 0))
        runner = Runner(planner, tempo_fetch=lambda: (date(2026, 11, 22), 'bleu'))
        self.assertEqual(runner.tick(datetime(2026, 11, 21, 19, 31)), ['veille-tempo'])

    def test_deadline_reminders_escalate_and_survive_a_move(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            lib = library(tmp, fake, {'garantie.txt': "Garantie constructeur : 2 ans, jusqu'au 18/11/2027\n"}, llm=None)
            lib.scan(classify=False)
            planner = Planner(':memory:')
            runner = Runner(planner, toolbox=Toolbox(Env(library=lib)))
            task = planner.get('echeances')
            runner.run(task, datetime(2027, 10, 25, 9, 0))
            runner.run(task, datetime(2027, 10, 26, 9, 0))
            self.assertEqual([n['niveau'] for n in planner.notifications()], ['info'])
            os.rename(lib.root / 'garantie.txt', lib.root / 'rangé.txt')   # rangement : même contenu
            lib.scan(classify=False)
            runner.run(task, datetime(2027, 10, 27, 9, 0))
            self.assertEqual(len(planner.notifications()), 1)
            runner.run(task, datetime(2027, 11, 13, 9, 0))                 # à 5 jours : second rappel
            self.assertEqual([n['niveau'] for n in planner.notifications()], ['warning', 'info'])
            lib.close()

    def test_battery_alert_clears_when_plugged_in(self):
        watcher = Watcher(load_rules(DEFAULT_RULES))
        self.assertEqual(watcher.update(Sample(battery_percent=10, on_battery=True))[0].kind, 'raised')
        self.assertEqual(watcher.update(Sample(battery_percent=10, on_battery=False))[0].kind, 'cleared')
        self.assertEqual(watcher.active(), [])


class Dialogue(unittest.TestCase):
    def test_model_that_keeps_asking_for_tools_still_ends_cleanly(self):
        with FakeColibri() as fake, Store(':memory:') as store:
            fake.always_tool = ('stockage', {})
            assistant = Assistant(OpenAICompatClient(fake.url), store, toolbox=Toolbox(Env()))
            conv = store.new_conversation()
            turn = Turn()
            text = ''.join(i for i in assistant.reply(conv, 'qwen36-35b', 'Il reste de la place ?', 'R', turn=turn)
                           if isinstance(i, str))
            self.assertIn('pas réussi à conclure', text)
            self.assertEqual(store.messages(conv)[-1]['content'], turn.text)   # enregistré, pas perdu


if __name__ == '__main__':
    unittest.main()
