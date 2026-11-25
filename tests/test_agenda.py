"""L'agenda : dates comprises par des règles, rappels envoyés une fois, écriture sur demande seulement."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from nexus.agenda import Agenda, apply_command, parse_command, parse_when, quick_add
from nexus.assistant import Assistant, Turn
from nexus.memory import Store
from nexus.ollama import OllamaClient
from nexus.planner import Planner, Runner
from nexus.router import plan
from nexus.tools import Env, Toolbox
from tests.fake_ollama import FakeOllama

NOW = datetime(2026, 11, 25, 10, 30)          # un mercredi


def at(*parts: int) -> datetime:
    return datetime(*parts)


class WhenTest(unittest.TestCase):
    def check(self, text: str, start: datetime, all_day: bool = False, repeat: str = ''):
        when = parse_when(text, NOW)
        self.assertIsNotNone(when, text)
        self.assertEqual((when.start, when.all_day, when.repeat), (start, all_day, repeat), text)

    def test_relative_days_and_times(self):
        self.check('demain à 9h', at(2026, 11, 26, 9, 0))
        self.check('après-demain à 14h30', at(2026, 11, 27, 14, 30))
        self.check('ce soir', at(2026, 11, 25, 19, 0))
        self.check('aujourd\'hui à 18 heures', at(2026, 11, 25, 18, 0))
        self.check('demain', at(2026, 11, 26), all_day=True)
        self.check('dans 20 minutes', at(2026, 11, 25, 10, 50))
        self.check('dans 2 heures', at(2026, 11, 25, 12, 30))
        self.check('dans 3 jours', at(2026, 11, 28), all_day=True)
        self.check('dans une semaine à midi', at(2026, 12, 2, 12, 0))
        self.check('dans 2 mois', at(2027, 1, 25), all_day=True)
        self.check('à 15h', at(2026, 11, 25, 15, 0))                 # heure seule, pas encore passée : aujourd'hui
        self.check('à 8h', at(2026, 11, 26, 8, 0))                   # déjà passée : demain
        self.check('vers 14:45 demain', at(2026, 11, 26, 14, 45))

    def test_week_days(self):
        self.check('vendredi à 11h', at(2026, 11, 27, 11, 0))
        self.check('mercredi', at(2026, 12, 2), all_day=True)        # nous sommes mercredi : le prochain
        self.check('lundi prochain à 9h', at(2026, 11, 30, 9, 0))
        self.check('la semaine prochaine', at(2026, 11, 30), all_day=True)

    def test_calendar_dates(self):
        self.check('le 12 décembre à 14h30', at(2026, 12, 12, 14, 30))
        self.check('le 1er mars', at(2027, 3, 1), all_day=True)      # déjà passé cette année : l'an prochain
        self.check('le 03/09/2027', at(2027, 9, 3), all_day=True)
        self.check('le 5/12 à 9h', at(2026, 12, 5, 9, 0))
        self.check('le 5', at(2026, 12, 5), all_day=True)
        self.check('le 30', at(2026, 11, 30), all_day=True)
        self.check('samedi 12 décembre 2026 à 20h', at(2026, 12, 12, 20, 0))
        self.check('le 14 février 2028', at(2028, 2, 14), all_day=True)

    def test_repeats(self):
        self.check('tous les lundis à 19h30', at(2026, 11, 30, 19, 30), repeat='hebdomadaire')
        self.check('tous les jours à 8h', at(2026, 11, 26, 8, 0), repeat='quotidien')
        self.check('le 14 février tous les ans', at(2027, 2, 14), all_day=True, repeat='annuel')
        self.check('le 5 tous les mois', at(2026, 12, 5), all_day=True, repeat='mensuel')
        self.check('chaque mercredi à 18h', at(2026, 11, 25, 18, 0), repeat='hebdomadaire')

    def test_nothing_is_guessed(self):
        for text in ('appeler le plombier', 'le 31 février', 'bientôt', 'un de ces jours', '', 'à 25h'):
            self.assertIsNone(parse_when(text, NOW), text)


class CommandTest(unittest.TestCase):
    def test_phrases_become_commands(self):
        cases = {
            "Rappelle-moi d'appeler le plombier demain à 9h": ('event', 'Appeler le plombier', at(2026, 11, 26, 9, 0), 0),
            'Ajoute un rendez-vous chez le dentiste le 12 décembre à 14h30':
                ('event', 'Rendez-vous chez le dentiste', at(2026, 12, 12, 14, 30), 30),
            'rappelle moi dans 20 minutes de sortir le gâteau du four':
                ('event', 'Sortir le gâteau du four', at(2026, 11, 25, 10, 50), 0),
            'Note un rendez-vous avec le notaire vendredi à 11 heures dans mon agenda':
                ('event', 'Rendez-vous avec le notaire', at(2026, 11, 27, 11, 0), 30),
            'Ajoute un rdv : contrôle technique de la voiture le 03/09/2027':
                ('event', 'Contrôle technique de la voiture', at(2027, 9, 3), 30),
            'Peux-tu me rappeler de payer le loyer le 5': ('event', 'Payer le loyer', at(2026, 12, 5), 0),
        }
        for message, (kind, title, start, remind) in cases.items():
            command = parse_command(message, NOW)
            self.assertIsNotNone(command, message)
            self.assertEqual((command.kind, command.title, command.when.start, command.remind),
                             (kind, title, start, remind), message)
        todo = parse_command('Ajoute renvoyer le colis avant vendredi à ma liste de choses à faire', NOW)
        self.assertEqual((todo.kind, todo.title, todo.when.start.date()), ('todo', 'Renvoyer le colis', date(2026, 11, 27)))
        self.assertEqual(parse_command('À faire : prendre rendez-vous chez le garagiste', NOW).title,
                         'Prendre rendez-vous chez le garagiste')
        self.assertEqual(parse_command('Ajoute du lait à ma liste', NOW).title, 'Du lait')
        self.assertEqual(parse_command('Raye le lait de ma liste', NOW).kind, 'done')

    def test_ordinary_sentences_are_left_to_the_model(self):
        for message in ('Quand est mon prochain rendez-vous ?', 'Bonjour', 'Ajoute du sel à la recette',
                        'Explique-moi ce qu\'est un rappel de vaccin', 'Qu\'est-ce que j\'ai demain ?',
                        'Rappelle-moi ce que dit mon bail ?', 'Note bien que je préfère les réponses courtes', 'x' * 500):
            self.assertIsNone(parse_command(message, NOW), message)


class AgendaTest(unittest.TestCase):
    def setUp(self):
        self.agenda = Agenda(':memory:')

    def tearDown(self):
        self.agenda.close()

    def test_commands_write_and_confirm(self):
        say = lambda text: apply_command(self.agenda, text, NOW)
        self.assertEqual(say("Rappelle-moi d'appeler le plombier demain à 9h"),
                         "Noté dans ton agenda : **Appeler le plombier**, demain à 9h (rappel à l'heure dite).")
        self.assertIn('**Rendez-vous chez le dentiste**, samedi 12 décembre à 14h30 (rappel 30 min avant)',
                      say('Ajoute un rendez-vous chez le dentiste le 12 décembre à 14h30'))
        self.assertIn('toutes les semaines, à partir de lundi 30 novembre à 19h30',
                      say('Rappelle-moi de sortir les poubelles tous les lundis à 19h30'))
        self.assertIn('rappel le matin à 8h', say("Rappelle-moi l'anniversaire de Léa le 14 février tous les ans"))
        self.assertEqual(say('Ajoute du lait à ma liste'), 'Ajouté à ta liste : **Du lait**.')
        self.assertIn('(pour vendredi 27 novembre)', say('Ajoute renvoyer le colis avant vendredi à ma liste'))
        month = [e['titre'] for e in self.agenda.events(NOW, NOW + timedelta(days=28))]
        self.assertEqual(month.count('Sortir les poubelles'), 4)             # une par semaine
        self.assertEqual(sorted(set(month)), ['Appeler le plombier', 'Rendez-vous chez le dentiste', 'Sortir les poubelles'])
        self.assertEqual([t['texte'] for t in self.agenda.todos()], ['Renvoyer le colis', 'Du lait'])
        # sans date : une chose à faire, et c'est dit
        answer = say('Rappelle-moi de rendre le dossier')
        self.assertIn('Ajouté à ta liste : **Rendre le dossier**', answer)
        self.assertIn('pas vu de date', answer)
        # heure déjà passée : rien n'est noté
        before = len(self.agenda.events(NOW - timedelta(days=1), NOW + timedelta(days=1)))
        self.assertIn('déjà passé', say("Rappelle-moi d'appeler aujourd'hui à 8h"))
        self.assertEqual(len(self.agenda.events(NOW - timedelta(days=1), NOW + timedelta(days=1))), before)
        self.assertIsNone(say('Bonjour, ça va ?'))

    def test_done_only_when_one_todo_clearly_matches(self):
        self.agenda.add_todo('Acheter du pain')
        self.agenda.add_todo('Acheter des timbres')
        self.agenda.add_todo('Renvoyer le colis')
        say = lambda text: apply_command(self.agenda, text, NOW)
        self.assertEqual(say('Raye le pain de ma liste'), "C'est rayé : **Acheter du pain**. Il reste 2 choses à faire.")
        self.assertIsNone(say("J'ai fini mon rapport"))                  # rien de tel : phrase ordinaire
        self.assertEqual(say("J'ai fait le colis"), "C'est rayé : **Renvoyer le colis**. Il reste 1 chose à faire.")
        self.assertEqual(say('Raye acheter des timbres'), "C'est rayé : **Acheter des timbres**. Ta liste est vide.")
        self.assertEqual(len(self.agenda.todos(include_done=True)), 3)   # rayé, pas effacé

    def test_quick_add_without_a_formula(self):
        self.assertIn('**Dentiste**, demain à 14h30 (rappel 30 min avant)', quick_add(self.agenda, 'Dentiste demain à 14h30', NOW))
        self.assertEqual(quick_add(self.agenda, 'acheter du pain', NOW), 'Ajouté à ta liste : **Acheter du pain**.')
        self.assertIn('Du lait', quick_add(self.agenda, 'Ajoute du lait à ma liste', NOW))
        self.assertEqual(len(self.agenda.todos()), 2)
        # une échéance (« avant vendredi ») : chose à faire datée, pas rendez-vous
        self.assertEqual(quick_add(self.agenda, 'renvoyer le colis avant vendredi', NOW),
                         'Ajouté à ta liste : **Renvoyer le colis** (pour vendredi 27 novembre).')
        self.assertIn('Noté dans ton agenda : **Réunion parents**, vendredi 27 novembre',
                      quick_add(self.agenda, 'Réunion parents vendredi', NOW))

    def test_repeats_expand_and_survive_short_months(self):
        self.agenda.add_event('Loyer', at(2027, 1, 31), all_day=True, repeat='mensuel')
        days = [e['quand'][:10] for e in self.agenda.events(at(2027, 1, 1), at(2027, 5, 1))]
        self.assertEqual(days, ['2027-01-31', '2027-02-28', '2027-03-31', '2027-04-30'])
        self.agenda.add_event('Bissextile', at(2028, 2, 29), all_day=True, repeat='annuel')
        self.assertEqual([e['quand'][:10] for e in self.agenda.events(at(2029, 1, 1), at(2029, 12, 31))
                          if e['titre'] == 'Bissextile'], ['2029-02-28'])
        with self.assertRaises(ValueError):
            self.agenda.add_event('  ', NOW)
        with self.assertRaises(ValueError):
            self.agenda.add_event('x', NOW, repeat='parfois')

    def test_reminders_fire_once_at_the_right_moment(self):
        event = self.agenda.add_event('Dentiste', at(2026, 11, 26, 14, 30), place='12 rue des Lilas')
        self.agenda.add_event('Appeler le plombier', at(2026, 11, 26, 9, 0), remind=0)
        self.agenda.add_event('Anniversaire de Léa', at(2026, 11, 26), all_day=True)
        self.agenda.add_todo('Renvoyer le colis', date(2026, 11, 26))
        titles = lambda moment: [r['title'] for r in self.agenda.due_reminders(moment)]
        self.assertEqual(titles(at(2026, 11, 25, 22, 0)), [])                       # la veille : rien encore
        self.assertEqual(titles(at(2026, 11, 26, 7, 59)), [])
        self.assertEqual(titles(at(2026, 11, 26, 8, 0)),
                         ["Aujourd'hui : Anniversaire de Léa", "À faire aujourd'hui : Renvoyer le colis"])
        self.assertIn('Maintenant : Appeler le plombier (9h)', titles(at(2026, 11, 26, 9, 0)))
        self.assertNotIn('Dans 30 min : Dentiste (14h30)', titles(at(2026, 11, 26, 13, 59)))
        self.assertIn('Dans 30 min : Dentiste (14h30)', titles(at(2026, 11, 26, 14, 0)))
        reminder = [r for r in self.agenda.due_reminders(at(2026, 11, 26, 14, 0)) if 'Dentiste' in r['title']][0]
        self.assertEqual(reminder['key'], f'agenda:evenement:{event}:2026-11-26T14:30')
        self.assertIn('Lieu : 12 rue des Lilas', reminder['text'])
        # PC resté éteint : un rappel manqué de peu est encore envoyé, pas un rappel vieux de deux jours
        self.assertIn('Maintenant : Dentiste (14h30)', titles(at(2026, 11, 26, 16, 0)))
        self.assertEqual([t for t in titles(at(2026, 11, 28, 9, 0)) if 'Dentiste' in t], [])

    def test_the_planner_turns_reminders_into_notifications_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            planner = Planner(Path(tmp) / 'taches.sqlite3')
            self.agenda.add_event('Dentiste', at(2026, 11, 26, 14, 30))
            self.agenda.add_event('Notaire', at(2026, 11, 27, 11, 0))
            self.agenda.add_todo('Renvoyer le colis', date(2026, 11, 27))
            runner = Runner(planner, toolbox=Toolbox(Env(agenda=self.agenda)))
            for moment in (at(2026, 11, 26, 14, 0), at(2026, 11, 26, 14, 1), at(2026, 11, 26, 14, 20)):
                runner.tick(moment)                                      # trois passages, un seul rappel
            titles = [n['titre'] for n in planner.notifications()]
            self.assertEqual(titles, ['Dans 30 min : Dentiste (14h30)'])
            runner.tick(at(2026, 11, 26, 19, 0))                         # résumé du soir pour le lendemain
            evening = planner.notifications()[0]
            self.assertEqual(evening['titre'], 'Demain : 1 rendez-vous et 1 chose à faire')
            self.assertIn('11h00 : Notaire', evening['texte'])
            self.assertIn('à faire : Renvoyer le colis', evening['texte'])
            planner.close()

    def test_overview_is_readable(self):
        self.agenda.add_event('Dentiste', at(2026, 11, 26, 14, 30), place='Annecy')
        self.agenda.add_event('Poubelles', at(2026, 11, 30, 19, 30), repeat='hebdomadaire')
        self.agenda.add_todo('Déclarer les impôts', date(2026, 11, 20))
        text = self.agenda.overview(NOW, 7)
        self.assertIn('Nous sommes mercredi 25 novembre 2026, il est 10h30.', text)
        self.assertIn('- demain à 14h30 : Dentiste — Annecy', text)
        self.assertIn('- lundi 30 novembre à 19h30 : Poubelles (toutes les semaines)', text)
        self.assertIn('- Déclarer les impôts (pour vendredi 20 novembre, EN RETARD)', text)
        self.assertIn('Aucun rendez-vous', Agenda(':memory:').overview(NOW))


class AssistantAgendaTest(unittest.TestCase):
    def test_the_code_writes_the_model_only_reads(self):
        with FakeOllama() as fake:
            fake.capabilities['qwen3:4b'] = ['completion', 'thinking', 'tools']
            agenda, store = Agenda(':memory:'), Store(':memory:')
            toolbox = Toolbox(Env(agenda=agenda))
            assistant = Assistant(OllamaClient(fake.url), store, toolbox=toolbox, agenda=agenda)
            conversation = store.new_conversation()
            turn = Turn()
            text = ''.join(i for i in assistant.reply(conversation, 'qwen3:4b', 'Rappelle-moi de sortir le chien dans 3 jours à 18h',
                                                      '', turn=turn) if isinstance(i, str))
            self.assertIn('Noté dans ton agenda : **Sortir le chien**', text)
            self.assertEqual(fake.last('/api/chat'), None)               # aucun modèle sollicité pour écrire
            self.assertEqual(len(agenda.events(datetime.now(), datetime.now() + timedelta(days=5))), 1)
            self.assertEqual([m['role'] for m in store.messages(conversation)], ['user', 'assistant'])
            # une question : l'outil de consultation est préparé d'office, et rien n'est écrit
            self.assertEqual([n for n, _ in plan("Qu'est-ce que j'ai de prévu cette semaine ?")], ['agenda'])
            list(assistant.reply(conversation, 'qwen3:4b', "Qu'est-ce que j'ai de prévu cette semaine ?", ''))
            sent = fake.last('/api/chat')['messages']
            self.assertTrue(any(m['role'] == 'tool' and 'Sortir le chien' in m['content'] for m in sent))
            self.assertEqual(len(agenda.events(datetime.now(), datetime.now() + timedelta(days=5))), 1)
            self.assertIn('agenda', toolbox.tools)
            self.assertIn('Agenda non configuré', Toolbox(Env()).call('agenda', {}))


if __name__ == '__main__':
    unittest.main()
