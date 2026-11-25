from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from nexus.library import Library
from nexus.ollama import OllamaClient
from nexus.planner import Planner, Runner, Schedule, fetch_tempo, find_deadlines, tempo_message
from nexus.skills import SkillBook
from nexus.tools import Env, Toolbox
from tests.corpus import build
from tests.fake_ollama import FakeOllama


class Schedules(unittest.TestCase):
    def test_parse_and_describe(self):
        self.assertEqual(Schedule.parse({'type': 'quotidien', 'heure': '19:30'}).describe(), 'chaque jour à 19h30')
        self.assertEqual(Schedule.parse({'type': 'hebdomadaire', 'jour': 'Lundi', 'heure': '8h05'}).describe(),
                         'chaque lundi à 08h05')
        for bad in ({'type': 'quotidien', 'heure': '25:00'}, {'type': 'mensuel', 'jour_du_mois': 31, 'heure': '08:00'},
                    {'type': 'hebdomadaire', 'jour': 'funday', 'heure': '08:00'}, {'type': 'cron', 'heure': '08:00'}, 'x'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Schedule.parse(bad)

    def test_previous_and_next(self):
        saturday = datetime(2026, 11, 21, 15, 0)
        weekly = Schedule.parse({'type': 'hebdomadaire', 'jour': 'lundi', 'heure': '08:40'})
        self.assertEqual(weekly.previous(saturday), datetime(2026, 11, 16, 8, 40))
        self.assertEqual(weekly.next(saturday), datetime(2026, 11, 23, 8, 40))
        monthly = Schedule.parse({'type': 'mensuel', 'jour_du_mois': 1, 'heure': '08:50'})
        self.assertEqual(monthly.previous(datetime(2026, 1, 1, 7, 0)), datetime(2025, 12, 1, 8, 50))
        self.assertEqual(monthly.next(datetime(2026, 12, 31, 23, 0)), datetime(2027, 1, 1, 8, 50))
        daily = Schedule.parse({'type': 'quotidien', 'heure': '19:30'})
        self.assertEqual(daily.previous(datetime(2026, 11, 21, 19, 30)), datetime(2026, 11, 21, 19, 30))


ORIGINAL_TASKS = {'veille-tempo', 'echeances', 'bilan-energie-mensuel', 'check-up-hebdo'}


def original_tasks_only(planner: Planner) -> None:
    """Coupe les tâches ajoutées depuis (agenda, tournée, journal…) : ces tests portent sur le mécanisme."""
    for task in planner.tasks():
        if task['id'] not in ORIGINAL_TASKS:
            planner.set_active(task['id'], False)


class Claiming(unittest.TestCase):
    def test_catch_up_once_and_no_backlog_on_activation(self):
        planner = Planner(':memory:')
        original_tasks_only(planner)
        runner = Runner(planner)
        self.assertEqual(runner.tick(datetime(2026, 11, 23, 9, 0)), ['echeances'])
        self.assertEqual(runner.tick(datetime(2026, 11, 23, 9, 5)), [])
        self.assertEqual(runner.tick(datetime(2026, 12, 14, 9, 0)), ['echeances'])  # 3 lundis manqués : 1 fois
        planner.set_active('check-up-hebdo', True, now=datetime(2026, 12, 14, 9, 0))
        self.assertEqual(runner.tick(datetime(2026, 12, 14, 9, 1)), [])  # pas de rattrapage du passé
        self.assertEqual(runner.tick(datetime(2026, 12, 20, 10, 15)), ['check-up-hebdo'])

    def test_missed_tasks_are_caught_up_in_the_order_of_the_night(self):
        planner = Planner(':memory:')
        for task in planner.tasks():
            planner.set_active(task['id'], task['id'] in ('journal-matin', 'sauvegarde-nuit', 'courrier-nuit', 'tournee-nuit'),
                               now=datetime(2026, 11, 24, 23, 0))
        due = [task['id'] for task in planner.claim_due(datetime(2026, 11, 25, 11, 0))]      # PC rallumé à 11h
        self.assertEqual(due, ['tournee-nuit', 'courrier-nuit', 'sauvegarde-nuit', 'journal-matin'])
        planner.close()

    def test_two_processes_never_run_the_same_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Planner(Path(tmp) / 't.sqlite3'), Planner(Path(tmp) / 't.sqlite3')
            original_tasks_only(first)
            now = datetime(2026, 11, 23, 9, 0)
            claimed = [t['id'] for t in first.claim_due(now)] + [t['id'] for t in second.claim_due(now)]
            self.assertEqual(claimed, ['echeances'])
            first.close()
            second.close()

    def test_notifications_dedupe_and_read(self):
        planner = Planner(':memory:')
        self.assertIsNotNone(planner.notify('info', 'A', 'texte', once_key='k1'))
        self.assertIsNone(planner.notify('info', 'A', 'texte', once_key='k1'))
        self.assertEqual(planner.unread(), 1)
        planner.mark_read()
        self.assertEqual(planner.unread(), 0)

    def test_failing_task_is_reported_not_raised(self):
        planner = Planner(':memory:')
        planner.set_active('veille-tempo', True, now=datetime(2026, 11, 20, 12, 0))

        def broken():
            raise OSError('réseau indisponible')

        Runner(planner, tempo_fetch=broken).tick(datetime(2026, 11, 21, 19, 31))
        self.assertIn('échec', planner.get('veille-tempo')['resultat'])
        self.assertIn('en échec', planner.notifications()[0]['titre'])


class Tempo(unittest.TestCase):
    def test_fetch_parses_fixed_api(self):
        day, color = fetch_tempo(lambda url: b'{"dateJour":"2027-01-12","codeJour":3,"libCouleur":"Rouge"}')
        self.assertEqual((day, color), (date(2027, 1, 12), 'rouge'))
        self.assertEqual(fetch_tempo(lambda url: b'{"dateJour":"2027-01-12","codeJour":0}')[1], None)
        with self.assertRaises(ValueError):
            fetch_tempo(lambda url: b'[1, 2]')

    def test_red_day_notifies_once_with_plan(self):
        planner = Planner(':memory:')
        runner = Runner(planner, tempo_fetch=lambda: (date(2027, 1, 12), 'rouge'))
        runner.run_now('veille-tempo')
        runner.run_now('veille-tempo')
        notes = planner.notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['niveau'], 'critical')
        self.assertIn('22h à 6h', notes[0]['texte'])
        self.assertEqual(Runner(planner, tempo_fetch=lambda: (date(2027, 1, 13), 'bleu')).run_now('veille-tempo'),
                         '2027-01-13 : bleu, rien à signaler')

    def test_plan_uses_relevant_habits_only(self):
        _, _, text = tempo_message(date(2027, 1, 12), 'rouge', ['On charge la voiture la nuit', 'Ma fille a 8 ans'])
        self.assertIn('voiture', text)
        self.assertNotIn('fille', text)


class WithDocuments(unittest.TestCase):
    def test_deadlines_and_skill_report(self):
        with FakeOllama() as fake, tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'docs'
            build(root)
            lib = Library(Path(tmp) / 'lib.sqlite3', root, OllamaClient(fake.url), embed_model='fake-embed:1')
            lib.scan(classify=False)
            lib.db.execute("UPDATE documents SET doc_date='2026-03-20' WHERE path='devis.pdf'")
            lib.db.commit()
            deadlines = {(d['path'], d['date']) for d in find_deadlines(lib)}
            self.assertIn(('commande_88271.pdf', date(2027, 11, 18)), deadlines)
            self.assertIn(('attestation-habitation.pdf', date(2026, 6, 30)), deadlines)
            self.assertIn(('devis.pdf', date(2026, 6, 20)), deadlines)  # « valable 3 mois »
            toolbox = Toolbox(Env(library=lib))
            planner = Planner(':memory:')
            runner = Runner(planner, toolbox=toolbox, skills=SkillBook(None, set(toolbox.tools)))
            self.assertEqual(runner.run(planner.get('echeances'), datetime(2026, 6, 5, 9, 0)),
                             '2 échéance(s) dans les 30 jours')
            titles = [n['titre'] for n in planner.notifications()]
            self.assertTrue(any('devis' in t.lower() or 'Fin de validité' in t for t in titles))
            runner.run(planner.get('echeances'), datetime(2026, 6, 6, 9, 0))
            self.assertEqual(len(planner.notifications()), 2)  # pas de doublon le lendemain
            self.assertEqual(runner.run_now('check-up-hebdo'), 'rapport déposé dans les notifications')
            self.assertIn('# Check-up du PC', planner.notifications()[0]['texte'])
            lib.close()


if __name__ == '__main__':
    unittest.main()
