"""Le journal du matin et ses actualités : contenu juste, flux traités comme des données, page sans script."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from nexus import news
from nexus.agenda import Agenda
from nexus.journal import Edition, Press, compose, essentials, gather, render
from nexus.news import Newsroom, parse_feed
from nexus.ollama import OllamaClient
from nexus.planner import Planner, Runner
from tests.fake_ollama import FakeOllama

NOW = datetime(2026, 11, 25, 6, 30)


def rss(items: list[tuple[str, str, str]], when: datetime | None = None) -> bytes:
    stamp = (when or NOW.replace(tzinfo=timezone.utc)).strftime('%a, %d %b %Y %H:%M:%S +0000')
    body = ''.join(f'<item><title><![CDATA[{t}]]></title><link>{link}</link><description><![CDATA[{d}]]></description>'
                   f'<pubDate>{stamp}</pubDate></item>' for t, d, link in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


class FeedTest(unittest.TestCase):
    def test_rss_and_atom_are_read_as_plain_text(self):
        raw = rss([('Prix de <b>l\'électricité</b> &amp; Tempo', '<p>Le tarif <script>alert(1)</script>change.</p><img src=x>',
                    'https://www.lemonde.fr/a'), ('Sans lien sûr', 'x', 'http://exemple.test/a'),
                   ('', 'sans titre', 'https://www.lemonde.fr/b')])
        articles = parse_feed(raw, 'Le Monde', 'energie')
        self.assertEqual(len(articles), 1)                               # lien non chiffré et titre vide : écartés
        self.assertEqual(articles[0].title, "Prix de l'électricité & Tempo")
        self.assertEqual(articles[0].summary, 'Le tarif alert(1) change.')
        self.assertNotIn('<', articles[0].title + articles[0].summary)
        atom = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Titre atom</title>'
                b'<link href="https://www.numerama.com/x"/><summary>R\xc3\xa9sum\xc3\xa9</summary>'
                b'<updated>2026-11-25T05:00:00Z</updated></entry></feed>')
        entry = parse_feed(atom, 'Numerama', 'numerique')[0]
        self.assertEqual((entry.title, entry.summary, entry.link), ('Titre atom', 'Résumé', 'https://www.numerama.com/x'))
        self.assertEqual(entry.published, datetime(2026, 11, 25, 5, 0, tzinfo=timezone.utc))

    def test_hostile_feeds_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss><channel><item><title>&lol;</title></item></channel></rss>'
        for raw in (bomb, b'<rss><channel><!ENTITY x "y"></channel></rss>', b'pas du xml', b''):
            with self.assertRaises(ValueError):
                parse_feed(raw, 'x', 'general')
        with self.assertRaises(ValueError):
            news.fetch('https://exemple.test/flux.xml')                  # hôte hors catalogue
        with self.assertRaises(ValueError):
            news.fetch('http://www.lemonde.fr/rss/une.xml')              # pas chiffré

    def test_the_catalogue_only_holds_https_feeds(self):
        for theme in news.THEMES.values():
            for _source, url in theme['flux']:
                self.assertTrue(url.startswith('https://'), url)
        self.assertIn('general', news.THEMES)


class NewsroomTest(unittest.TestCase):
    FEEDS = {
        'https://www.lemonde.fr/rss/une.xml': rss([('Budget : le vote attendu', 'Les députés votent.', 'https://www.lemonde.fr/1'),
                                                    ('Tempête sur la Bretagne', 'Vents forts.', 'https://www.lemonde.fr/2')]),
        'https://www.franceinfo.fr/titres.rss': rss([('Grève dans les transports', 'Trafic perturbé.', 'https://www.franceinfo.fr/1')]),
        'https://www.bevolt.com/feed/rss/': rss([('Recharge voiture électrique moins chère la nuit', 'voiture électrique recharge prise',
                                                  'https://www.bevolt.com/1'),
                                                 ('Salon de la moto', 'motos', 'https://www.bevolt.com/2')]),
    }

    def room(self, fake: FakeOllama, interests: list[str]) -> Newsroom:
        def fetcher(url: str) -> bytes:
            if url not in self.FEEDS:
                raise OSError('injoignable')
            return self.FEEDS[url]
        return Newsroom(OllamaClient(fake.url), 'fake-embed:1', interests, fetcher)

    def test_themes_and_articles_follow_the_memories(self):
        with FakeOllama() as fake:
            room = self.room(fake, ['voiture électrique recharge prise', 'réponses courtes'])
            self.assertEqual([k for k, _s, _w in room.themes()][:2], ['general', 'voiture'])
            picked = room.select(NOW)
            self.assertEqual(len(picked['une']), 3)
            self.assertEqual({a.source for a in picked['une']}, {'Le Monde', 'franceinfo'})
            self.assertEqual(picked['pour_toi'][0].title, 'Recharge voiture électrique moins chère la nuit')
            self.assertEqual(picked['pour_toi'][0].reason, 'voiture électrique recharge prise')
            self.assertEqual(room.errors, [])

    def test_without_memories_or_embeddings_only_headlines(self):
        with FakeOllama() as fake:
            self.assertEqual([k for k, _s, _w in self.room(fake, []).themes()], ['general'])
        offline = Newsroom(OllamaClient('http://127.0.0.1:9', 2), 'fake-embed:1', ['voiture électrique'],
                           lambda url: self.FEEDS[url])
        picked = offline.select(NOW)
        self.assertEqual((len(picked['une']), picked['pour_toi']), (3, []))

    def test_old_articles_and_dead_feeds_do_not_break_the_edition(self):
        old = rss([('Vieille nouvelle', 'x', 'https://www.lemonde.fr/vieux')], NOW.replace(tzinfo=timezone.utc) - timedelta(days=5))
        room = Newsroom(None, None, [], lambda url: old if 'lemonde' in url else (_ for _ in ()).throw(OSError('panne')))
        picked = room.select(NOW)
        self.assertEqual(picked, {'une': [], 'pour_toi': []})
        self.assertEqual(len(room.errors), 1)


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agenda = Agenda(':memory:')
        self.agenda.add_event('Dentiste', datetime(2026, 11, 25, 14, 30), place='Annecy')
        self.agenda.add_event('Notaire', datetime(2026, 11, 27, 11, 0))
        self.agenda.add_todo('Renvoyer le colis', date(2026, 11, 20))
        self.agenda.add_todo('Acheter du <pain>')

    def tearDown(self):
        self.agenda.close()
        self.tmp.cleanup()

    def test_content_and_essentials_are_computed(self):
        edition = gather(NOW, number=7, agenda=self.agenda, tempo=lambda which: {'today': 'blanc', 'tomorrow': 'rouge'}[which])
        self.assertEqual([e['titre'] for e in edition.today], ['Dentiste'])
        self.assertEqual([e['titre'] for e in edition.week], ['Notaire'])
        self.assertEqual(edition.tempo, {'aujourdhui': 'blanc', 'demain': 'rouge'})
        text = essentials(edition)
        self.assertIn("1 rendez-vous aujourd'hui, le premier à 14h30 (Dentiste).", text)
        self.assertIn('2 choses à faire, dont 1 en retard.', text)
        self.assertIn('Demain sera un jour rouge Tempo', text)
        self.assertEqual(essentials(Edition(NOW.date(), 1, NOW)), "Rien à l'agenda aujourd'hui.")

    def test_the_page_is_self_contained_and_escapes_everything(self):
        edition = gather(NOW, number=7, agenda=self.agenda)
        edition.headlines = [news.Article('Titre <script>alert(1)</script>', 'Résumé "cité" & co', 'https://www.lemonde.fr/x?a=1&b=2',
                                          'Le Monde', 'general')]
        edition.personal = [news.Article('Recharge', 'x', 'https://www.bevolt.com/1', 'Bevolt', 'voiture', reason='J\'ai une <voiture>')]
        page = render(edition)
        self.assertIn('Mercredi 25 novembre 2026', page)
        self.assertIn('N° 7', page)
        self.assertNotIn('<script', page)
        self.assertIn('Titre &lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertIn('Acheter du &lt;pain&gt;', page)
        self.assertIn('parce que tu as dit : « J&#x27;ai une &lt;voiture&gt; »', page)
        self.assertIn("default-src 'none'", page)                        # rien ne se charge depuis la page
        self.assertNotIn('src=', page)
        self.assertIn('href="https://www.lemonde.fr/x?a=1&amp;b=2"', page)

    def test_a_failing_section_does_not_stop_the_presses(self):
        class Broken:
            def events(self, *_):
                raise RuntimeError('agenda cassé')

            def select(self, _now):
                raise OSError('pas de réseau')
            errors: list[str] = []
        edition = gather(NOW, agenda=Broken(), newsroom=Broken(), tempo=lambda which: (_ for _ in ()).throw(OSError()),
                         writer=lambda facts: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(len(edition.notes), 2)
        self.assertIn('Agenda illisible', edition.notes[0])
        self.assertIn('<h1>', render(edition))

    def test_publish_numbers_pdf_and_printing(self):
        calls: list[list[str]] = []

        def run(argv: list[str]) -> int:
            calls.append(argv)
            if argv[0] == 'navigateur':
                Path(next(a for a in argv if a.startswith('--print-to-pdf=')).split('=', 1)[1]).write_bytes(b'%PDF-1.4')
            return 0
        press = Press(Path(self.tmp.name) / 'journal', Path(self.tmp.name) / 'journal.json', run)
        self.assertEqual(press.settings(), {'imprimer': False, 'actualites': True, 'mot': False, 'imprimante': ''})
        first = compose(NOW, press, agenda=self.agenda, fetcher=lambda url: (_ for _ in ()).throw(OSError()))
        self.assertEqual((first['edition'].number, first['imprime']), (1, False))
        self.assertTrue(first['pdf'].is_file())
        self.assertFalse(any(c[0] == 'lp' for c in calls))               # pas d'impression sans l'avoir demandée
        again = compose(NOW + timedelta(hours=2), press, agenda=self.agenda, fetcher=lambda url: b'')
        self.assertEqual(again['edition'].number, 1)                     # numéro du jour refait : même numéro
        press.set(imprimer=True, imprimante='Salon_HP; rm -rf /')         # nom d'imprimante douteux : ignoré
        self.assertEqual(press.settings()['imprimante'], '')
        press.set(imprimante='Salon_HP')
        tomorrow = compose(NOW + timedelta(days=1), press, agenda=self.agenda, fetcher=lambda url: b'')
        self.assertEqual((tomorrow['edition'].number, tomorrow['imprime']), (2, True))
        self.assertEqual(calls[-1], ['lp', '-d', 'Salon_HP', str(tomorrow['pdf'])])
        self.assertEqual([e['jour'] for e in press.editions()], ['2026-11-26', '2026-11-25'])
        self.assertIsNone(press.path('../../etc/passwd'))
        self.assertIsNone(press.path('2026-11-25', '.sh'))
        self.assertIsNotNone(press.path('2026-11-25', '.pdf'))

    def test_the_planner_publishes_every_morning(self):
        planner = Planner(':memory:')
        made: list[datetime] = []

        def journal(now: datetime) -> dict:
            made.append(now)
            return {'imprime': False, 'pdf': Path('x.pdf'), 'notes': ['Imprimante éteinte.']}
        runner = Runner(planner, journal=journal)
        self.assertEqual(runner.run_now('journal-matin', NOW), 'PDF prêt')
        note = planner.notifications()[0]
        self.assertEqual(note['titre'], 'Le journal du jour est prêt')
        self.assertIn('Imprimante éteinte.', note['texte'])
        runner.run_now('journal-matin', NOW)                             # refait dans la journée : pas de seconde annonce
        self.assertEqual(len(planner.notifications()), 1)
        self.assertEqual(Runner(planner).run_now('journal-matin', NOW), 'journal non configuré')
        planner.close()


if __name__ == '__main__':
    unittest.main()
