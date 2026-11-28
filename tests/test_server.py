"""Le serveur maison : appairage, droits, dialogue en flux, dépôt et lecture de documents."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import tempfile
import threading
import time
import unittest

from nexus import access as access_module
from nexus import library as library_module
from nexus import server
from nexus.access import Access
from nexus.config import Settings
from tests.fake_ollama import FakeOllama
from tests.pdfgen import write_pdf
from tests.test_planner import original_tasks_only

REAL_OCR = bool(library_module.ocr_languages())


class AccessTest(unittest.TestCase):
    def test_code_is_single_use_and_expires(self):
        with Access(':memory:') as access:
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            code = access.new_code(start)
            self.assertRegex(code, r'^\d{4}-\d{4}$')
            self.assertIsNone(access.pair(code, 'Tel', start + timedelta(minutes=11)))   # trop tard
            code = access.new_code(start)
            token = access.pair(code.replace('-', ' '), '  Mon\ttéléphone\n', start)
            self.assertIsNotNone(token)
            self.assertIsNone(access.pair(code, 'Autre', start))                         # déjà servi
            self.assertEqual(access.check(token)['nom'], 'Mon téléphone')
            self.assertIsNone(access.check(token + 'x'))
            self.assertIsNone(access.check(None))

    def test_only_fingerprints_are_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'appareils.sqlite3'
            with Access(path) as access:
                code = access.new_code()
                token = access.pair(code, 'Tel')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            raw = b''.join(p.read_bytes() for p in Path(tmp).iterdir())
            self.assertNotIn(token.encode(), raw)
            self.assertNotIn(code.replace('-', '').encode(), raw)

    def test_guessing_cancels_the_pending_code(self):
        with Access(':memory:') as access:
            code = access.new_code()
            for _ in range(access_module.MAX_FAILURES):
                self.assertIsNone(access.pair('00000000', 'Curieux'))
            self.assertIsNone(access.pair(code, 'Tel'))        # le bon code ne vaut plus rien
            self.assertIsNotNone(access.pair(access.new_code(), 'Tel'))

    def test_revoke(self):
        with Access(':memory:') as access:
            token = access.pair(access.new_code(), 'Tel')
            device = access.devices()[0]
            self.assertTrue(access.revoke(device['id']))
            self.assertIsNone(access.check(token))
            self.assertFalse(access.revoke(device['id']))


class ServerCase(unittest.TestCase):
    tls = False
    forced_model: str | None = 'qwen3:4b'
    autonomous = False          # la plupart des tests veulent voir les propositions, pas un rangement d'office

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.docs = root / 'Documents'
        self.docs.mkdir()
        self.fake = FakeOllama().__enter__()
        self.settings = Settings(
            ollama_url=self.fake.url, model=self.forced_model, db_path=root / 'data' / 'nexus.sqlite3',
            measures_path=root / 'data' / 'modeles.json',
            rules_path=root / 'none.json', num_ctx=8192, library_root=self.docs,
            library_db=root / 'data' / 'bib.sqlite3', embed_model='fake-embed:1', memory=False,
            skills_dir=root / 'savoir-faire', planner_db=root / 'data' / 'taches.sqlite3',
            agenda_db=root / 'data' / 'agenda.sqlite3',
            autonomy_db=root / 'data' / 'activite.sqlite3' if self.autonomous else None,
            autonomy_levels=root / 'config' / 'autonomie.json',
            mail_db=root / 'data' / 'courrier.sqlite3', mail_accounts=root / 'config' / 'courrier.json')
        self.httpd = server.make_server(self.settings, port=0, tls=self.tls)
        self.home, self.port = self.httpd.home, self.httpd.server_port
        original_tasks_only(self.home.planner)       # ni journal (Internet, navigateur) ni tournée pendant les tests
        self.home.start(warm_up=False)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home.close()
        self.httpd.access.close()
        self.fake.__exit__()
        self.tmp.cleanup()

    def connect(self) -> http.client.HTTPConnection:
        if self.tls:
            # Le téléphone ne fait confiance qu'à l'autorité de la maison, installée une fois.
            context = ssl.create_default_context(cafile=str(self.settings.db_path.parent / 'serveur' / 'autorite.pem'))
            return http.client.HTTPSConnection('127.0.0.1', self.port, timeout=20, context=context)
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)

    def call(self, method: str, path: str, body=None, *, cookie: str | None = '', headers: dict | None = None,
             raw: bool = False):
        """Renvoie (statut, corps décodé, en-têtes). cookie='' : celui de l'appareil appairé."""
        sent = {'X-Nexus': '1'} if method != 'GET' else {}
        if cookie == '':
            cookie = self.cookie
        if cookie:
            sent['Cookie'] = cookie
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            sent['Content-Type'] = 'application/json'
        sent.update(headers or {})
        sent = {k: v for k, v in sent.items() if v is not None}
        conn = self.connect()
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            data = response.read()
            if not raw:
                data = json.loads(data) if data and 'json' in response.headers.get('Content-Type', '') else data
            return response.status, data, response.headers
        finally:
            conn.close()

    def pair(self, name: str = 'Téléphone de test') -> str:
        status, _data, headers = self.call('POST', '/api/appairage',
                                           {'code': self.httpd.access.new_code(), 'nom': name}, cookie=None)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        return headers['Set-Cookie']

    def ask(self, question: str, conversation=None) -> list[dict]:
        status, data, _headers = self.call('POST', '/api/question',
                                           {'question': question, 'conversation': conversation}, raw=True)
        self.assertEqual(status, 200, data)
        return [json.loads(line) for line in data.decode().splitlines() if line.strip()]

    def pdf(self, lines: list[str]) -> bytes:
        path = Path(self.tmp.name) / 'source.pdf'
        write_pdf(path, [lines])
        return path.read_bytes()

    def upload(self, name: str, content: bytes):
        from urllib.parse import quote
        return self.call('POST', '/api/documents?nom=' + quote(name), content,
                         headers={'Content-Type': 'application/octet-stream'})

    def wait_idle(self, seconds: float = 15) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and (self.home.busy.locked() or self.home.indexing):
            time.sleep(0.05)
        self.assertFalse(self.home.busy.locked() or self.home.indexing)


class ServerTest(ServerCase):
    def test_the_app_is_public_but_holds_no_data(self):
        for path in ('/', '/app.js', '/app.css', '/manifest.webmanifest', '/icone.svg'):
            status, _data, headers = self.call('GET', path, cookie=None)
            self.assertEqual(status, 200, path)
            self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        for path in ('/api/etat', '/api/conversations', '/api/documents', '/api/notifications',
                     '/api/documents/1/fichier', '/api/conversations/1'):
            self.assertEqual(self.call('GET', path, cookie=None)[0], 401, path)
        self.assertEqual(self.call('POST', '/api/question', {'question': 'x'}, cookie='nexus_appareil=' + 'a' * 43)[0], 401)
        self.assertEqual(self.call('GET', '/etc/passwd', cookie=None)[0], 404)
        self.assertEqual(self.call('GET', '/../nexus/server.py', cookie=None)[0], 404)

    def test_pairing_then_state(self):
        status, data, _h = self.call('POST', '/api/appairage', {'code': '1234-5678', 'nom': 'Intrus'}, cookie=None)
        self.assertEqual(status, 403)
        self.assertIn('nexus serveur appairer', data['erreur'])
        header = self.pair('Pixel de Léa')
        self.assertIn('HttpOnly', header)
        self.assertIn('SameSite=Strict', header)
        self.assertNotIn('Secure', header)       # pas de chiffrement dans ce test (écoute locale)
        status, state, _h = self.call('GET', '/api/etat')
        self.assertEqual(status, 200)
        self.assertEqual(state['appareil'], 'Pixel de Léa')
        self.assertEqual(state['moteur'], 'Ollama')
        self.assertFalse(state['occupe'])
        self.assertEqual(state['documents']['nombre'], 0)
        self.assertEqual([d['nom'] for d in self.httpd.access.devices()], ['Pixel de Léa'])
        self.httpd.access.revoke(self.httpd.access.devices()[0]['id'])
        self.assertEqual(self.call('GET', '/api/etat')[0], 401)

    def test_question_streams_and_is_saved(self):
        self.pair()
        events = self.ask('Bonjour ?')
        kinds = [e['t'] for e in events]
        self.assertEqual(kinds[0], 'debut')
        self.assertEqual(kinds[-1], 'fin')
        self.assertEqual(''.join(e['v'] for e in events if e['t'] == 'texte'), 'Bonjour !')
        conversation = events[0]['conversation']
        self.assertEqual(events[-1]['jetons'], 3)
        events = self.ask('Et encore ?', conversation)
        self.assertEqual(events[0]['conversation'], conversation)
        status, data, _h = self.call('GET', f'/api/conversations/{conversation}')
        self.assertEqual([m['role'] for m in data['messages']], ['user', 'assistant', 'user', 'assistant'])
        listed = self.call('GET', '/api/conversations')[1]['conversations']
        self.assertEqual(listed[0]['title'], 'Bonjour ?')
        self.assertEqual(self.call('POST', '/api/question', {'question': 'x', 'conversation': 9999})[0], 404)
        self.assertEqual(self.call('POST', '/api/question', {'question': '   '})[0], 400)
        self.assertTrue(self.call('DELETE', f'/api/conversations/{conversation}')[1]['efface'])
        self.assertEqual(self.call('GET', '/api/conversations')[1]['conversations'], [])

    def test_one_question_at_a_time_and_stop(self):
        self.pair()
        self.fake.delay = 1.5
        results: list = []
        thread = threading.Thread(target=lambda: results.append(self.ask('Longue question')))
        thread.start()
        end = time.monotonic() + 5
        while not self.home.busy.locked() and time.monotonic() < end:
            time.sleep(0.02)
        status, data, _h = self.call('POST', '/api/question', {'question': 'Moi aussi'})
        self.assertEqual(status, 409)
        self.assertIn('déjà', data['erreur'])
        self.assertTrue(self.call('GET', '/api/etat')[1]['occupe'])
        self.assertTrue(self.call('POST', '/api/arret', {})[1]['arrete'])
        thread.join(10)
        self.assertEqual(results[0][-1]['t'], 'fin')
        self.assertTrue(results[0][-1]['interrompu'])
        self.wait_idle()
        self.assertFalse(self.call('POST', '/api/arret', {})[1]['arrete'])

    def test_the_answer_survives_a_phone_that_disconnects(self):
        self.pair()
        self.fake.delay = 1.0
        conn = self.connect()
        conn.request('POST', '/api/question', body=json.dumps({'question': 'Tu finis sans moi ?'}).encode(),
                     headers={'X-Nexus': '1', 'Cookie': self.cookie, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        first = json.loads(response.fp.readline() and response.fp.readline())   # taille du bloc, puis l'événement
        self.assertEqual(first['t'], 'debut')
        self.assertEqual(self.call('GET', '/api/etat')[1]['conversation_en_cours'], first['conversation'])
        conn.close()                              # écran éteint, réseau perdu…
        self.wait_idle()
        messages = self.call('GET', f"/api/conversations/{first['conversation']}")[1]['messages']
        self.assertEqual(messages[-1], {'role': 'assistant', 'content': 'Bonjour !'})

    def test_only_the_app_can_change_things(self):
        self.pair()
        body = {'question': 'Bonjour ?'}
        self.assertEqual(self.call('POST', '/api/question', body, headers={'X-Nexus': None})[0], 403)
        self.assertEqual(self.call('POST', '/api/question', body, headers={'Origin': 'https://exemple.test'})[0], 403)
        self.assertEqual(self.call('DELETE', '/api/conversations/1', headers={'X-Nexus': None})[0], 403)
        self.assertEqual(self.call('POST', '/api/appairage', {'code': self.httpd.access.new_code(), 'nom': 'x'},
                                   cookie=None, headers={'X-Nexus': None})[0], 403)
        same = {'Origin': f'http://127.0.0.1:{self.port}'}
        self.assertEqual(self.call('POST', '/api/question', body, headers=same, raw=True)[0], 200)
        self.assertEqual(self.call('GET', '/api/etat', headers={'Host': 'pirate.exemple.test'})[0], 421)
        self.assertEqual(self.call('GET', '/', cookie=None, headers={'Host': 'pirate.exemple.test'})[0], 421)

    def test_a_connection_is_reused_like_a_browser_does(self):
        """Plusieurs requêtes sur la même connexion, dont des actions au corps « {} » jamais lu."""
        self.pair()
        self.home.planner.notify('info', 'Titre', 'Texte')
        conn = self.connect()
        try:
            for method, path, body in (('POST', '/api/arret', b'{}'), ('GET', '/api/etat', None),
                                       ('POST', '/api/notifications/lues', b'{}'), ('GET', '/api/notifications', None),
                                       ('POST', '/api/rangement/annuler', b'{"sans": "objet"}'), ('GET', '/api/rangement', None),
                                       ('GET', '/api/inconnu', None), ('GET', '/api/etat', None)):
                headers = {'Cookie': self.cookie}
                if body is not None:
                    headers.update({'X-Nexus': '1', 'Content-Type': 'application/json'})
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 404 if path == '/api/inconnu' else 200, (method, path))
        finally:
            conn.close()

    def test_bad_requests_do_not_break_the_server(self):
        self.pair()
        self.assertEqual(self.call('POST', '/api/question', b'{pas du json', headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.call('POST', '/api/question', b'[1, 2]')[0], 400)
        self.assertEqual(self.call('POST', '/api/question', b'x' * (server.MAX_JSON + 1))[0], 413)
        self.assertEqual(self.call('POST', '/api/souvenirs/1', {'garder': True})[1], {'ok': False})   # mémoire coupée
        self.assertEqual(self.call('GET', '/api/inconnu')[0], 404)
        self.assertEqual(self.call('GET', '/api/conversations/99999999999999999999')[0], 404)
        self.assertEqual(self.call('GET', '/api/etat')[0], 200)


class DocumentsTest(ServerCase):
    def test_upload_index_search_download(self):
        self.pair()
        self.fake.json_reply = {'categorie': 'energie', 'titre': 'Facture électricité mars', 'emetteur': 'Voltéa',
                                'date': '2026-03-05', 'montant': '84,20', 'resume': 'Facture'}
        content = self.pdf(['Facture électricité Voltéa', 'Date : 05/03/2026', 'Montant total TTC : 84,20 EUR',
                            'Consommation heures creuses 312 kWh'])
        status, data, _h = self.upload('Facture mars.pdf', content)
        self.assertEqual(status, 200, data)
        self.assertTrue(data['chemin'].startswith(server.INBOX + '/'))
        self.assertTrue(data['chemin'].endswith(' Facture mars.pdf'))
        self.assertEqual((self.docs / data['chemin']).read_bytes(), content)
        self.wait_idle()
        listed = self.call('GET', '/api/documents')[1]['documents']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['titre'], 'Facture électricité mars')     # lu ET classé en tâche de fond
        self.assertEqual(listed[0]['categorie'], 'Énergie')
        self.assertEqual(listed[0]['montant'], '84,20 €')
        found = self.call('GET', '/api/documents?q=heures%20creuses')[1]['passages']
        self.assertEqual(found[0]['id'], listed[0]['id'])
        self.assertIn('312 kWh', found[0]['extrait'])
        status, body, headers = self.call('GET', f"/api/documents/{listed[0]['id']}/fichier", raw=True)
        self.assertEqual((status, body), (200, content))
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertEqual([p.name for p in (self.docs / server.INBOX).iterdir() if p.name.startswith('.')], [])

    def test_nothing_is_ever_overwritten_and_names_stay_inside(self):
        self.pair()
        first, second = self.pdf(['Premier document']), self.pdf(['Second document'])
        one = self.upload('scan.pdf', first)[1]['chemin']
        two = self.upload('scan.pdf', second)[1]['chemin']
        self.assertNotEqual(one, two)
        self.assertEqual((self.docs / one).read_bytes(), first)
        self.assertEqual((self.docs / two).read_bytes(), second)
        for nasty in ('../../hors.pdf', '/etc/cron.d/x.pdf', '..\\..\\hors.pdf', '.caché.pdf'):
            status, data, _h = self.upload(nasty, first)
            self.assertEqual(status, 200, nasty)
            saved = (self.docs / data['chemin']).resolve()
            self.assertEqual(saved.parent, (self.docs / server.INBOX).resolve(), nasty)
            self.assertFalse(saved.name.startswith('.'))
        self.assertFalse((Path(self.tmp.name) / 'hors.pdf').exists())
        self.wait_idle()

    def test_refused_uploads(self):
        self.pair()
        self.assertEqual(self.upload('virus.exe', b'MZ' + b'\0' * 50)[0], 415)
        self.assertEqual(self.upload('faux.pdf', b'<html>pas un pdf</html>')[0], 415)
        self.assertEqual(self.upload('binaire.txt', b'\xff\xfe\x00\x01')[0], 415)
        self.assertEqual(self.upload('vide.pdf', b'')[0], 413)
        self.assertEqual(self.upload('gros.txt', b'a' * (server.MAX_TEXT_UPLOAD + 1))[0], 413)
        self.assertEqual(self.upload('note.txt', 'Penser à relever le compteur.'.encode())[0], 200)
        self.assertEqual(self.call('POST', '/api/documents?nom=x.pdf', self.pdf(['x']), cookie=None)[0], 401)
        self.wait_idle()
        inbox = self.docs / server.INBOX
        self.assertEqual(len(list(inbox.iterdir())), 1)       # les refus ne laissent aucun fichier

    def test_fake_images_are_refused(self):
        self.pair()
        self.assertEqual(self.upload('photo.jpg', b'<html>pas une image</html>')[0], 415)
        self.assertEqual(self.upload('photo.png', b'\xff\xd8\xff pas un png')[0], 415)
        self.assertEqual(self.upload('gros.jpg', b'\xff\xd8\xff' + b'0' * server.MAX_IMAGE_UPLOAD)[0], 413)
        self.assertFalse((self.docs / server.INBOX).exists() and list((self.docs / server.INBOX).iterdir()))

    @unittest.skipUnless(REAL_OCR, 'tesseract (avec le français) absent')
    def test_a_photo_sent_from_the_phone_is_read(self):
        from tests.scans import rasterize, write_png
        self.pair()
        source = Path(self.tmp.name) / 'source.pdf'
        write_pdf(source, [['Garage du Centre', 'Date : 14/11/2026', 'Remplacement des plaquettes de frein avant',
                            'Montant total TTC : 286,40 EUR']])
        photo = Path(self.tmp.name) / 'photo.png'
        write_png(photo, rasterize(source))
        status, data, _h = self.upload('Photo facture.png', photo.read_bytes())
        self.assertEqual(status, 200, data)
        self.wait_idle(60)
        card = self.call('GET', '/api/documents')[1]['documents'][0]
        self.assertTrue(card['reconnu'] and card['lisible'])
        found = self.call('GET', '/api/documents?q=plaquettes%20de%20frein')[1]['passages']
        self.assertIn('286,40', found[0]['extrait'])
        status, body, headers = self.call('GET', f"/api/documents/{card['id']}/fichier", raw=True)
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/png', photo.read_bytes()))

    def test_download_never_leaves_the_library(self):
        self.pair()
        (Path(self.tmp.name) / 'secret.txt').write_text('mot de passe')
        self.upload('note.txt', b'Une note.')
        self.wait_idle()
        doc_id = self.call('GET', '/api/documents')[1]['documents'][0]['id']
        self.assertEqual(self.call('GET', f'/api/documents/{doc_id}/fichier', raw=True)[1], b'Une note.')
        with self.home.library_lock:
            self.home.library.db.execute('UPDATE documents SET path=? WHERE id=?', ('../secret.txt', doc_id))
            self.home.library.db.commit()
        self.assertEqual(self.call('GET', f'/api/documents/{doc_id}/fichier')[0], 404)
        self.assertEqual(self.call('GET', '/api/documents/424242/fichier')[0], 404)
        self.assertEqual(self.call('GET', '/api/documents/abc/fichier')[0], 404)

    def test_notifications(self):
        self.pair()
        self.home.planner.notify('info', 'Demain : jour rouge', 'Décaler la recharge de la voiture.')
        self.assertEqual(self.call('GET', '/api/etat')[1]['notifications'], 1)
        notes = self.call('GET', '/api/notifications')[1]['notifications']
        self.assertEqual(notes[0]['titre'], 'Demain : jour rouge')
        self.call('POST', '/api/notifications/lues', {})
        self.assertEqual(self.call('GET', '/api/etat')[1]['notifications'], 0)


class TidyTest(ServerCase):
    """Ranger depuis le téléphone : NEXUS propose, la personne décide, tout est annulable."""

    def receive(self, name: str, lines: list[str], reply: dict | None) -> str:
        self.fake.json_reply = reply
        status, data, _h = self.upload(name, self.pdf(lines))
        self.assertEqual(status, 200, data)
        self.wait_idle()
        return data['chemin']

    def proposals(self) -> dict:
        status, data, _h = self.call('GET', '/api/rangement')
        self.assertEqual(status, 200, data)
        return data

    FACTURE = {'categorie': 'energie', 'titre': 'Facture électricité mars', 'emetteur': 'Voltéa',
               'date': '2026-03-05', 'montant': '84,20', 'resume': 'Facture'}

    def test_propose_tidy_and_undo(self):
        self.pair()
        here = self.receive('scan 1.pdf', ['Facture électricité Voltéa', 'Date : 05/03/2026',
                                           'Montant total TTC : 84,20 EUR'], self.FACTURE)
        content = (self.docs / here).read_bytes()
        state = self.proposals()
        self.assertEqual(len(state['propositions']), 1)
        proposal = state['propositions'][0]
        self.assertEqual(proposal['chemin'], here)
        self.assertEqual(proposal['propose'], 'Énergie/2026/2026-03-05 Voltéa - Facture électricité mars.pdf')
        self.assertIsNone(state['dernier'])
        self.assertEqual(self.call('GET', '/api/etat')[1]['a_ranger'], 1)
        self.assertTrue((self.docs / here).is_file())                   # proposer ne déplace rien

        done = self.call('POST', '/api/rangement', {'ids': [proposal['id']]})[1]
        self.assertEqual(done['ranges'], [{'de': here, 'vers': proposal['propose']}])
        self.assertFalse((self.docs / here).exists())
        self.assertEqual((self.docs / proposal['propose']).read_bytes(), content)
        state = self.proposals()
        self.assertEqual((state['propositions'], state['dernier']['nombre']), ([], 1))
        self.assertEqual(self.call('GET', '/api/etat')[1]['a_ranger'], 0)
        self.assertEqual(self.call('GET', '/api/documents')[1]['documents'][0]['chemin'], proposal['propose'])
        status, body, _h = self.call('GET', f"/api/documents/{proposal['id']}/fichier", raw=True)
        self.assertEqual((status, body), (200, content))                # toujours téléchargeable après rangement

        undone = self.call('POST', '/api/rangement/annuler', {})[1]
        self.assertEqual(undone, {'annules': [{'de': proposal['propose'], 'vers': here}], 'non_annules': []})
        self.assertEqual((self.docs / here).read_bytes(), content)
        self.assertFalse((self.docs / 'Énergie').exists())              # dossiers vides retirés
        self.assertEqual(len(self.proposals()['propositions']), 1)
        self.assertEqual(self.call('POST', '/api/rangement/annuler', {})[1]['annules'], [])

    def test_tidying_never_overwrites(self):
        self.pair()
        self.receive('a.pdf', ['Facture Voltéa', 'Date : 05/03/2026', 'Premier exemplaire'], self.FACTURE)
        self.receive('b.pdf', ['Facture Voltéa', 'Date : 05/03/2026', 'Second exemplaire, différent'], self.FACTURE)
        ids = [p['id'] for p in self.proposals()['propositions']]
        done = self.call('POST', '/api/rangement', {'ids': ids})[1]['ranges']
        targets = sorted(move['vers'] for move in done)
        self.assertEqual(targets, ['Énergie/2026/2026-03-05 Voltéa - Facture électricité mars (2).pdf',
                                   'Énergie/2026/2026-03-05 Voltéa - Facture électricité mars.pdf'])
        texts = {(self.docs / t).read_bytes() for t in targets}
        self.assertEqual(len(texts), 2)                                 # les deux contenus sont là, intacts
        self.assertEqual(self.proposals()['dernier']['nombre'], 2)
        self.assertEqual(len(self.call('POST', '/api/rangement/annuler', {})[1]['annules']), 2)   # le lot entier

    def test_correct_by_hand_then_tidy(self):
        self.pair()
        here = self.receive('photo.pdf', ['Cabinet médical', 'Ordonnance du 18/11/2026'], None)   # l'IA n'a pas su classer
        proposal = self.proposals()['propositions'][0]
        self.assertIsNone(proposal['propose'])
        fixed = self.call('POST', f"/api/documents/{proposal['id']}",
                          {'categorie': 'sante', 'titre': 'Ordonnance  Dr Marchand/../x', 'emetteur': '', 'date': '2026-11-18'})
        self.assertEqual(fixed[0], 200, fixed[1])
        # Le titre est nettoyé : les barres obliques disparaissent, il reste un simple nom de fichier.
        self.assertEqual(fixed[1]['propose'], 'Santé/2026/2026-11-18 Ordonnance Dr Marchand .. x.pdf')
        for bad in ({'categorie': 'inconnue'}, {'date': 'demain'}, {'titre': '   '}, {'titre': '../..'}, {'titre': 5},
                    {'titre': 'x' * 300}):
            self.assertEqual(self.call('POST', f"/api/documents/{proposal['id']}", bad)[0], 400, bad)
        self.assertEqual(self.call('POST', '/api/documents/9999', {'titre': 'x'})[0], 404)
        done = self.call('POST', '/api/rangement', {'ids': [proposal['id']]})[1]['ranges']
        self.assertEqual(done, [{'de': here, 'vers': 'Santé/2026/2026-11-18 Ordonnance Dr Marchand .. x.pdf'}])
        saved = (self.docs / done[0]['vers']).resolve()
        self.assertEqual(saved.parent, (self.docs / 'Santé' / '2026').resolve())

    def test_leave_here(self):
        self.pair()
        here = self.receive('note.pdf', ['Facture Voltéa', 'Date : 05/03/2026'], self.FACTURE)
        proposal = self.proposals()['propositions'][0]
        self.assertEqual(self.call('POST', f"/api/documents/{proposal['id']}/laisser", {})[1], {'ok': True})
        self.assertEqual(self.proposals()['propositions'], [])
        self.assertTrue((self.docs / here).is_file())
        self.assertEqual(self.call('POST', '/api/rangement', {'ids': [proposal['id']]})[1]['ranges'], [])

    def test_the_phone_only_tidies_what_it_received(self):
        self.pair()
        write_pdf(self.docs / 'vrac.pdf', [['Facture Voltéa ancienne', 'Date : 05/03/2026', 'Rangée à ma façon']])
        self.fake.json_reply = self.FACTURE
        self.home.library.llm_model = 'qwen3:4b'
        with self.home.library_lock:
            self.home.library.scan(classify=True)
        other = self.home.library.pending()[0]                          # sur le PC, NEXUS le rangerait ailleurs
        state = self.proposals()
        self.assertEqual((state['propositions'], state['ailleurs']), ([], 1))
        self.assertEqual(self.call('GET', '/api/etat')[1]['a_ranger'], 0)
        self.assertEqual(self.call('POST', '/api/rangement', {'ids': [other['id']]})[1]['ranges'], [])
        self.assertTrue((self.docs / 'vrac.pdf').is_file())             # le téléphone ne touche pas au reste

    def test_requests_are_checked(self):
        self.pair()
        for bad in ({}, {'ids': []}, {'ids': ['1']}, {'ids': [True]}, {'ids': 3}, {'ids': list(range(500))}):
            self.assertEqual(self.call('POST', '/api/rangement', bad)[0], 400, bad)
        self.assertEqual(self.call('POST', '/api/rangement', {'ids': [424242]})[1]['ranges'], [])
        self.assertEqual(self.call('GET', '/api/rangement', cookie=None)[0], 401)
        self.assertEqual(self.call('POST', '/api/rangement', {'ids': [1]}, cookie=None)[0], 401)
        self.assertEqual(self.call('POST', '/api/rangement', {'ids': [1]}, headers={'X-Nexus': None})[0], 403)
        self.assertEqual(self.call('POST', '/api/rangement/annuler', {}, headers={'X-Nexus': None})[0], 403)


class AutonomyApiTest(ServerCase):
    """Un document envoyé par le téléphone est rangé et ses dates notées, sans rien demander."""
    autonomous = True

    def test_received_document_is_handled_alone_and_can_be_undone_from_the_phone(self):
        self.pair()
        self.fake.json_reply = {'categorie': 'sante', 'titre': 'Ordonnance', 'emetteur': 'Cabinet des Aravis',
                                'date': '2026-11-18', 'montant': '', 'resume': 'Ordonnance'}
        later = (datetime.now() + timedelta(days=20)).strftime('%d/%m/%Y')
        status, data, _h = self.upload('photo.pdf', self.pdf(['Cabinet des Aravis', 'Date : 18/11/2026',
                                                              f'Prochain rendez-vous de contrôle : {later} à 9h30']))
        self.assertEqual(status, 200, data)
        self.wait_idle()
        activity = self.call('GET', '/api/activite')[1]
        done = {a['geste']: a for a in activity['actes']}
        self.assertEqual({g: a['etat'] for g, a in done.items()}, {'ranger': 'fait', 'rendez-vous': 'fait'})
        self.assertIn('Rangé : Ordonnance → Santé/2026/', done['ranger']['resume'])
        self.assertEqual(activity['niveaux'], {'ranger': 'prevenir', 'rendez-vous': 'prevenir', 'tache': 'prevenir',
                                               'rendez-vous-courriel': 'demander', 'tache-courriel': 'demander',
                                               'reprendre-tache': 'prevenir'})
        self.assertFalse((self.docs / data['chemin']).exists())                       # parti du dossier d'arrivée
        self.assertEqual(self.call('GET', '/api/rangement')[1]['propositions'], [])
        events = self.call('GET', '/api/agenda')[1]['evenements']
        self.assertEqual((events[0]['titre'], events[0]['heure']), ('Prochain rendez-vous de contrôle (Cabinet des Aravis)', '9h30'))
        notes = self.call('GET', '/api/notifications')[1]['notifications']
        self.assertEqual(notes[0]['titre'], 'Document reçu : Ordonnance (Santé)')
        # défaire depuis le téléphone
        undone = self.call('POST', f"/api/activite/{done['ranger']['id']}/annuler", {})[1]['message']
        self.assertIn('Remis à sa place', undone)
        self.assertTrue((self.docs / data['chemin']).is_file())
        self.call('POST', f"/api/activite/{done['rendez-vous']['id']}/annuler", {})
        self.assertEqual(self.call('GET', '/api/agenda')[1]['evenements'], [])
        self.home.autonomy.tour()                                                     # la tournée ne le refait pas
        self.assertTrue((self.docs / data['chemin']).is_file())

    def test_levels_are_set_from_the_phone_and_checked(self):
        self.pair()
        self.assertEqual(self.call('POST', '/api/autonomie', {'geste': 'ranger', 'niveau': 'demander'})[1]['niveaux']['ranger'],
                         'demander')
        self.assertEqual(self.call('POST', '/api/autonomie', {'geste': 'supprimer', 'niveau': 'prevenir'})[0], 400)
        self.assertEqual(self.call('POST', '/api/autonomie', {'geste': 'ranger', 'niveau': 'toujours'})[0], 400)
        self.fake.json_reply = {'categorie': 'sante', 'titre': 'Ordonnance', 'emetteur': 'Cabinet', 'date': '2026-11-18',
                                'montant': '', 'resume': 'x'}
        data = self.upload('photo.pdf', self.pdf(['Cabinet', 'Date : 18/11/2026', 'Ordonnance']))[1]
        self.wait_idle()
        proposal = self.call('GET', '/api/activite')[1]['actes'][0]
        self.assertEqual(proposal['etat'], 'propose')
        self.assertTrue((self.docs / data['chemin']).is_file())
        self.assertIn('Rangé', self.call('POST', f"/api/activite/{proposal['id']}/valider", {})[1]['message'])
        self.assertFalse((self.docs / data['chemin']).exists())
        self.assertEqual(self.call('GET', '/api/activite', cookie=None)[0], 401)
        self.assertEqual(self.call('POST', '/api/activite/1/annuler', {}, headers={'X-Nexus': None})[0], 403)
        self.assertEqual(self.call('POST', '/api/activite/1/autre', {})[0], 404)


class VoiceApiTest(ServerCase):
    def test_a_dictated_question_is_transcribed_on_the_pc(self):
        from nexus.voice import MAX_AUDIO, Voice
        from tests.test_voice import FakeEngines
        self.pair()
        self.assertFalse(self.call('GET', '/api/etat')[1]['voix'])                       # pas installée : pas de bouton micro
        status, data, _h = self.call('POST', '/api/voix', b'\x1aE\xdf\xa3 son', headers={'Content-Type': 'audio/webm'})
        self.assertEqual(status, 422)
        self.assertIn('pas installée', data['erreur'])
        engines = FakeEngines(' Ajoute du lait à ma liste. ')
        self.home.voice = Voice(Path(self.tmp.name) / 'voix', engines)
        self.assertTrue(self.call('GET', '/api/etat')[1]['voix'])
        status, data, headers = self.call('POST', '/api/voix', b'\x1aE\xdf\xa3 son', headers={'Content-Type': 'audio/webm'})
        self.assertEqual((status, data), (200, {'texte': 'Ajoute du lait à ma liste.'}))
        self.assertIn('microphone=(self)', headers['Permissions-Policy'])
        self.assertEqual(self.call('POST', '/api/voix', b'', headers={'Content-Type': 'audio/webm'})[0], 413)
        self.assertEqual(self.call('POST', '/api/voix', b'x' * (MAX_AUDIO + 1))[0], 413)
        self.assertEqual(self.call('POST', '/api/voix', b'son', cookie=None)[0], 401)
        self.assertEqual(self.call('POST', '/api/voix', b'son', headers={'X-Nexus': None})[0], 403)
        engines.heard = '[BLANK_AUDIO]'
        self.assertEqual(self.call('POST', '/api/voix', b'silence')[1], {'texte': ''})


class MailApiTest(ServerCase):
    def test_important_mail_is_listed_and_accounts_stay_on_the_pc(self):
        self.pair()
        state = self.call('GET', '/api/courrier')[1]
        self.assertEqual(state, {'boites': [], 'importants': []})
        self.assertEqual(self.call('GET', '/api/courrier', cookie=None)[0], 401)
        # aucune adresse de l'appli ne permet de renseigner une boîte ou un mot de passe
        self.assertEqual(self.call('POST', '/api/courrier', {'identifiant': 'a@b.c', 'mot_de_passe': 'x'})[0], 404)


class AgendaApiTest(ServerCase):
    def test_add_by_phrase_list_check_and_delete(self):
        self.pair()
        self.assertEqual(self.call('GET', '/api/agenda')[1], {'evenements': [], 'taches': [], 'faites': []})
        added = self.call('POST', '/api/agenda', {'phrase': 'Dentiste demain à 14h30'})[1]['message']
        self.assertEqual(added, 'Noté dans ton agenda : Dentiste, demain à 14h30 (rappel 30 min avant).')
        self.assertEqual(self.call('POST', '/api/agenda', {'phrase': 'acheter du pain'})[1]['message'],
                         'Ajouté à ta liste : Acheter du pain.')
        state = self.call('GET', '/api/agenda')[1]
        event, todo = state['evenements'][0], state['taches'][0]
        self.assertEqual((event['titre'], event['jour'], event['heure']), ('Dentiste', 'demain', '14h30'))
        self.assertEqual((todo['texte'], todo['faite']), ('Acheter du pain', False))
        self.assertEqual(self.call('POST', f"/api/agenda/taches/{todo['id']}", {'faite': True})[1], {'ok': True})
        state = self.call('GET', '/api/agenda')[1]
        self.assertEqual((state['taches'], [t['texte'] for t in state['faites']]), ([], ['Acheter du pain']))
        self.call('POST', f"/api/agenda/taches/{todo['id']}", {'faite': False})           # décoché : il revient
        self.assertEqual(len(self.call('GET', '/api/agenda')[1]['taches']), 1)
        self.assertEqual(self.call('DELETE', f"/api/agenda/taches/{todo['id']}")[1], {'ok': True})
        self.assertEqual(self.call('DELETE', f"/api/agenda/evenements/{event['id']}")[1], {'ok': True})
        self.assertEqual(self.call('GET', '/api/agenda')[1], {'evenements': [], 'taches': [], 'faites': []})
        self.assertEqual(self.call('DELETE', '/api/agenda/evenements/999')[1], {'ok': False})

    def test_requests_are_checked(self):
        self.assertEqual(self.call('GET', '/api/agenda', cookie=None)[0], 401)
        self.assertEqual(self.call('POST', '/api/agenda', {'phrase': 'x demain'}, cookie=None)[0], 401)
        self.pair()
        self.assertEqual(self.call('POST', '/api/agenda', {'phrase': '   '})[0], 400)
        self.assertEqual(self.call('POST', '/api/agenda', {'phrase': 5})[0], 400)
        self.assertEqual(self.call('POST', '/api/agenda', {'phrase': 'x demain'}, headers={'X-Nexus': None})[0], 403)
        self.assertEqual(self.call('DELETE', '/api/agenda/autre/1')[0], 404)
        self.assertEqual(self.call('POST', '/api/agenda/evenements/1', {})[0], 404)

    def test_a_reminder_asked_in_the_chat_needs_no_model_and_reaches_the_notifications(self):
        from datetime import datetime, timedelta
        self.pair()
        before = len(self.fake.requests)
        events = self.ask("Rappelle-moi d'arroser les plantes dans 2 minutes")
        self.assertIn('Noté dans ton agenda : **Arroser les plantes**', ''.join(e['v'] for e in events if e['t'] == 'texte'))
        self.assertEqual([p for p, _ in self.fake.requests[before:] if p == '/api/chat'], [])     # écrit par le code
        self.assertIsNone(events[-1]['modele'])
        self.home.runner.tick(datetime.now() + timedelta(minutes=3))           # le planificateur passe : rappel envoyé
        notes = self.call('GET', '/api/notifications')[1]['notifications']
        self.assertIn('Arroser les plantes', notes[0]['titre'])
        self.assertEqual(self.call('GET', '/api/etat')[1]['notifications'], 1)


class AutoModelTest(ServerCase):
    """Aucun modèle imposé : le serveur choisit, et propose une réponse approfondie s'il y a mieux."""
    forced_model = None

    def test_the_server_chooses_and_offers_a_thorough_answer(self):
        from nexus.models import Measures, machine_signature
        sample = self.home.telemetry.sample()
        measures = Measures(self.settings.measures_path, machine_signature(sample.cores, sample.mem_total_mib))
        measures.save('llama3.2:1b', {'outils': 0.6, 'essais': 8, 'secondes': 6.0, 'jetons_s': 20.0})
        measures.save('qwen3:4b', {'outils': 0.9, 'essais': 8, 'secondes': 120.0, 'jetons_s': 3.0})
        self.pair()
        state = self.call('GET', '/api/etat')[1]
        self.assertEqual((state['modele'], state['modele_approfondi']), ('llama3.2:1b', 'qwen3:4b'))
        self.assertIn('qwen3:4b est meilleur mais trop lent', state['choix'])
        events = self.ask('Bonjour ?')
        self.assertEqual(events[-1]['modele'], 'llama3.2:1b')
        self.assertEqual(self.fake.last('/api/chat')['model'], 'llama3.2:1b')
        status, data, _h = self.call('POST', '/api/question', {'question': 'Bonjour ?', 'approfondi': True}, raw=True)
        deep = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        self.assertEqual(deep[-1]['modele'], 'qwen3:4b')
        self.assertEqual(self.fake.last('/api/chat')['model'], 'qwen3:4b')
        events = self.ask('Prends ton temps et explique-moi tout.')        # dit dans la question : même effet
        self.assertEqual(events[-1]['modele'], 'qwen3:4b')

    def test_a_received_document_is_classified_by_the_background_model(self):
        self.pair()
        self.fake.json_reply = {'categorie': 'energie', 'titre': 'Facture', 'emetteur': 'Voltéa',
                                'date': '2026-03-05', 'montant': '84,20', 'resume': 'Facture'}
        self.assertEqual(self.upload('f.pdf', self.pdf(['Facture électricité Voltéa', 'Date : 05/03/2026']))[0], 200)
        self.wait_idle()
        doc = self.home.library.documents()[0]
        self.assertEqual((doc['category'], doc['classified_by']), ('energie', 'qwen3:4b'))   # estimé le meilleur des deux


@unittest.skipUnless(shutil.which('openssl'), 'openssl absent')
class HandOverServerTest(ServerCase):
    def test_the_server_steps_down_when_another_version_takes_over(self):
        self.assertFalse(self.home.updater.outdated())      # lancé depuis un dossier de travail : pas concerné
        time.sleep(0.3)
        self.assertFalse(self.home.replaced.is_set())
        self.home.updater.outdated = lambda: True           # une mise à jour vient de changer la version en place
        self.assertTrue(self.home.replaced.wait(8))


class HealthApiTest(ServerCase):
    def test_the_phone_sees_what_nexus_found_about_itself(self):
        self.assertEqual(self.call('GET', '/api/sante', cookie=None)[0], 401)
        self.pair()
        report = self.call('GET', '/api/sante')[1]
        self.assertEqual(set(report), {'constats', 'bien'})
        titles = {c['titre']: c['niveau'] for c in report['constats']}
        self.assertEqual(titles.get('Aucune sauvegarde : une panne de disque emporterait tout'), 'info')
        self.assertNotIn('Le moteur de dialogue ne répond pas', titles)
        from nexus.ollama import OllamaError

        def down(*_args, **_kwargs):                        # le moteur s'arrête
            raise OllamaError('Ollama ne répond pas sur http://127.0.0.1:11434')

        self.home.client.models = down
        first = self.call('GET', '/api/sante')[1]['constats'][0]
        self.assertEqual((first['niveau'], first['titre']), ('critical', 'Le moteur de dialogue ne répond pas'))
        self.assertIn('systemctl status ollama', first['conseil'])


class AlertsCase(ServerCase):
    """Un faux téléphone qui s'abonne au relais d'alertes, comme l'appli ntfy."""

    def enable(self) -> str:
        self.pair()
        state = self.call('POST', '/api/alertes', {'active': True})[1]
        self.assertTrue(state['active'])
        return state['sujet']

    def subscribe(self, path: str):
        """Ouvre un flux JSON ; renvoie (connexion, réponse) une fois le « open » lu."""
        conn = self.connect()
        conn.request('GET', path)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.readline())['event'], 'open')
        return conn, response

    def websocket(self, path: str):
        """Poignée de main WebSocket ; renvoie (prise, lecteur de trames)."""
        import base64
        import socket
        from nexus.alerts import ws_accept
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=10)
        if self.tls:
            context = ssl.create_default_context(cafile=str(self.settings.db_path.parent / 'serveur' / 'autorite.pem'))
            sock = context.wrap_socket(sock, server_hostname='127.0.0.1')
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n'
                      f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        reader = sock.makefile('rb')
        self.assertIn(b'101', reader.readline())
        headers = {}
        while (line := reader.readline().strip()):
            name, _, value = line.decode().partition(':')
            headers[name.lower()] = value.strip()
        self.assertEqual(headers['sec-websocket-accept'], ws_accept(key))
        self.assertEqual(headers['upgrade'], 'websocket')

        def frame() -> tuple[int, bytes]:
            head = reader.read(2)
            length = head[1] & 0x7F
            self.assertFalse(head[1] & 0x80)              # le serveur ne masque jamais
            if length == 126:
                length = int.from_bytes(reader.read(2), 'big')
            return head[0] & 0x0F, reader.read(length)

        def hang_up() -> None:
            reader.close()                                # le lecteur tient la prise ouverte : lui d'abord
            sock.close()

        self.addCleanup(hang_up)
        frame.hang_up = hang_up
        return sock, frame

    def wait_listeners(self, expected: int) -> None:
        end = time.monotonic() + 8
        while time.monotonic() < end and self.home.listeners != expected:
            time.sleep(0.05)
        self.assertEqual(self.home.listeners, expected)


class AlertsApiTest(AlertsCase):
    def test_off_by_default_and_invisible_without_the_key(self):
        missing = self.call('GET', '/nimporte/quoi', cookie=None)
        self.assertEqual(missing[0], 404)
        for path in ('/nexus-abcdefghijklmnopqrstuvwx/json', '/x/auth', '/x/ws'):
            self.assertEqual(self.call('GET', path, cookie=None)[:2], missing[:2], path)
        self.assertEqual(self.call('GET', '/api/alertes', cookie=None)[0], 401)
        self.assertEqual(self.call('POST', '/api/alertes', {'active': True}, cookie=None)[0], 401)
        self.pair()
        state = self.call('GET', '/api/alertes')[1]
        self.assertEqual((state['disponible'], state['active'], state['sujet'], state['lien']), (True, False, None, None))
        topic = self.call('POST', '/api/alertes', {'active': True})[1]['sujet']
        state = self.call('GET', '/api/alertes')[1]
        self.assertEqual(state['lien'], f'ntfy://127.0.0.1:{self.port}/{topic}?display=NEXUS&secure=false')
        self.assertEqual((state['serveur'], state['relies']), (f'http://127.0.0.1:{self.port}', 0))
        self.assertEqual(self.call('GET', f'/{topic}/auth', cookie=None)[:2], (200, {'success': True}))
        self.assertEqual(self.call('GET', f'/{topic[:-1]}x/auth', cookie=None)[:2], missing[:2])     # mauvaise clé : rien
        self.assertEqual(self.call('GET', f'/{topic}/sse', cookie=None)[0], 404)
        # Lecture seule : personne ne peut déposer une alerte par le réseau, même avec la clé.
        for method in ('POST', 'DELETE'):
            for path in (f'/{topic}', f'/{topic}/json', '/'):
                self.assertEqual(self.call(method, path, b'Fausse alerte', cookie=None)[0], 404, (method, path))
        self.assertNotIn('Fausse alerte', str(self.home.planner.notifications()))
        self.call('POST', '/api/alertes', {'active': False})
        self.assertEqual(self.call('GET', f'/{topic}/auth', cookie=None)[:2], missing[:2])           # éteint : plus rien

    def test_poll_returns_what_was_missed(self):
        topic = self.enable()
        self.home.planner.notify('info', 'Première', 'un')
        self.home.planner.notify('warning', 'Deuxième', 'deux')
        poll = lambda since, headers=None: [json.loads(line) for line in
                                            self.call('GET', f'/{topic}/json?poll=1{since}', cookie=None, raw=True,
                                                      headers=headers)[1].decode().splitlines()]
        events = poll('&since=all')
        self.assertEqual([(e['event'], e['title'], e['priority']) for e in events],
                         [('message', 'Première', 3), ('message', 'Deuxième', 4)])
        self.assertEqual(events[0]['click'], f'http://127.0.0.1:{self.port}/')
        self.assertEqual([e['title'] for e in poll(f"&since={events[0]['id']}")], ['Deuxième'])
        self.assertEqual(poll('&since=none'), [])
        self.assertEqual([e['title'] for e in poll('', {'X-Since': 'latest'})], ['Deuxième'])        # en-tête accepté aussi

    def test_stream_delivers_alerts_as_they_come(self):
        topic = self.enable()
        self.home.planner.notify('info', 'Avant', 'déjà là')
        conn, response = self.subscribe(f'/{topic}/json?since=all')
        self.assertEqual(json.loads(response.readline())['title'], 'Avant')
        self.wait_listeners(1)
        self.assertEqual(self.call('GET', '/api/alertes')[1]['relies'], 1)
        started = time.monotonic()
        self.assertEqual(self.call('POST', '/api/alertes/essai', {})[1], {'ok': True})
        event = json.loads(response.readline())
        self.assertEqual((event['event'], event['title']), ('message', "Essai d'alerte"))
        self.assertLess(time.monotonic() - started, 5)
        conn.close()                                       # écran éteint, réseau perdu…
        self.wait_listeners(0)
        # … le téléphone revient et demande la suite : il ne reçoit que ce qu'il a manqué.
        self.home.planner.notify('info', 'Pendant la coupure', 'texte')
        conn, response = self.subscribe(f"/{topic}/json?since={event['id']}")
        self.assertEqual(json.loads(response.readline())['title'], 'Pendant la coupure')
        conn.close()
        self.wait_listeners(0)

    def test_stream_sends_signs_of_life(self):
        topic = self.enable()
        original = server.KEEPALIVE
        server.KEEPALIVE = 0.5
        self.addCleanup(setattr, server, 'KEEPALIVE', original)
        conn, response = self.subscribe(f'/{topic}/json?since=none')
        self.assertEqual(json.loads(response.readline())['event'], 'keepalive')
        conn.close()

    def test_renewing_the_key_hangs_up_on_old_phones(self):
        topic = self.enable()
        conn, response = self.subscribe(f'/{topic}/json?since=none')
        self.wait_listeners(1)
        renewed = self.call('POST', '/api/alertes', {'renouveler': True})[1]['sujet']
        self.assertNotEqual(renewed, topic)
        self.assertEqual(response.readline(), b'')         # le flux se termine proprement
        conn.close()
        self.wait_listeners(0)
        self.assertEqual(self.call('GET', f'/{topic}/auth', cookie=None)[0], 404)
        self.assertEqual(self.call('GET', f'/{renewed}/auth', cookie=None)[0], 200)

    def test_too_many_listeners_drop_the_oldest(self):
        topic = self.enable()
        original = server.MAX_LISTENERS
        server.MAX_LISTENERS = 2
        self.addCleanup(setattr, server, 'MAX_LISTENERS', original)
        opened = [self.subscribe(f'/{topic}/json?since=none') for _ in range(3)]
        self.wait_listeners(2)
        with self.assertRaises((OSError, http.client.HTTPException, ValueError)):       # le plus ancien est coupé
            if opened[0][1].readline() == b'':
                raise OSError('flux terminé')
        self.home.planner.notify('info', 'Pour les autres', 'texte')
        for _conn, response in opened[1:]:
            self.assertEqual(json.loads(response.readline())['title'], 'Pour les autres')
        for conn, _response in opened:
            conn.close()
        self.wait_listeners(0)

    def test_discreet_mode_over_the_wire(self):
        topic = self.enable()
        self.assertTrue(self.call('POST', '/api/alertes', {'discret': True})[1]['discret'])
        self.home.planner.notify('warning', 'Courrier : impôts', 'Avis à payer.')
        body = self.call('GET', f'/{topic}/json?poll=1', cookie=None, raw=True)[1].decode()
        self.assertNotIn('impôts', body)
        self.assertEqual(json.loads(body)['title'], 'NEXUS')

    def test_the_key_never_reaches_the_log(self):
        import contextlib
        import io
        topic = self.enable()
        self.httpd.verbose = True
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            self.call('GET', f'/{topic}/auth', cookie=None)
            self.call('GET', '/api/alertes')
            time.sleep(0.2)
        self.httpd.verbose = False
        self.assertIn('/api/alertes', log.getvalue())
        self.assertNotIn(topic, log.getvalue())

    def test_websocket_subscription(self):
        from tests.test_alerts import masked
        topic = self.enable()
        self.assertEqual(self.call('GET', f'/{topic}/ws', cookie=None)[0], 400)        # sans poignée de main WebSocket
        self.home.planner.notify('info', 'Ancienne', 'texte')
        sock, frame = self.websocket(f'/{topic}/ws?since=none')
        opcode, payload = frame()
        self.assertEqual((opcode, json.loads(payload)['event']), (1, 'open'))
        self.wait_listeners(1)
        sock.sendall(masked(0x9, b'tu es la ?'))           # l'appli vérifie la ligne toutes les trois minutes
        self.assertEqual(frame(), (0xA, b'tu es la ?'))
        self.home.planner.notify('warning', 'Rappel : dentiste à 14h30', 'Dans 30 minutes. ' + 'é' * 200)
        opcode, payload = frame()
        event = json.loads(payload)
        self.assertEqual((opcode, event['title'], event['priority']), (1, 'Rappel : dentiste à 14h30', 4))
        self.assertTrue(event['message'].endswith('é' * 200))                           # trame longue, accents intacts
        sock.sendall(masked(0x8, b'\x03\xe8'))
        self.assertEqual(frame(), (0x8, b'\x03\xe8'))
        sock.settimeout(5)
        self.assertEqual(sock.recv(64), b'')               # rien après l'adieu : un navigateur y verrait une erreur
        self.wait_listeners(0)

    def test_websocket_is_closed_politely_when_the_key_changes(self):
        topic = self.enable()
        sock, frame = self.websocket(f'/{topic}/ws?since=none')
        frame()
        self.wait_listeners(1)
        self.call('POST', '/api/alertes', {'renouveler': True})
        self.assertEqual(frame(), (0x8, b'\x03\xe8'))     # fermeture normale, annoncée
        self.wait_listeners(0)

    def test_websocket_refuses_a_phone_that_breaks_the_rules(self):
        topic = self.enable()
        sock, frame = self.websocket(f'/{topic}/ws')
        frame()
        self.wait_listeners(1)
        sock.sendall(b'\x81\x05Hello')                     # trame non masquée : interdit par le protocole
        self.wait_listeners(0)


class EncryptedAlertsTest(AlertsCase):
    tls = True

    def test_alerts_over_the_home_certificate(self):
        topic = self.enable()
        self.assertEqual(self.call('GET', '/api/alertes')[1]['lien'], f'ntfy://127.0.0.1:{self.port}/{topic}?display=NEXUS')
        conn, response = self.subscribe(f'/{topic}/json?since=none')
        sock, frame = self.websocket(f'/{topic}/ws?since=none')
        self.assertEqual(json.loads(frame()[1])['event'], 'open')
        self.wait_listeners(2)
        self.home.planner.notify('info', 'Chiffrée', 'texte')
        self.assertEqual(json.loads(response.readline())['click'], f'https://127.0.0.1:{self.port}/')
        self.assertEqual(json.loads(frame()[1])['title'], 'Chiffrée')
        conn.close()
        frame.hang_up()
        self.wait_listeners(0)


class EncryptedTest(ServerCase):
    tls = True

    def test_encrypted_end_to_end(self):
        folder = self.settings.db_path.parent / 'serveur'
        self.assertEqual(stat.S_IMODE((folder / 'cle.pem').stat().st_mode), 0o600)
        self.assertRegex(server.fingerprint(folder / 'certificat.pem'), r'^([0-9A-F]{2}:){31}[0-9A-F]{2}$')
        header = self.pair()
        self.assertIn('Secure', header)
        self.assertEqual(self.ask('Bonjour ?')[-1]['t'], 'fin')
        before = (folder / 'certificat.pem').read_bytes()
        from nexus.vpn import SERVER_ADDRESS
        server.ensure_certificate(folder, server.local_addresses() + [SERVER_ADDRESS])     # comme au démarrage
        self.assertEqual((folder / 'certificat.pem').read_bytes(), before)    # pas refait à chaque démarrage

    def test_the_home_authority_can_be_fetched_to_install_on_the_phone(self):
        status, body, headers = self.call('GET', '/autorite.crt', cookie=None, raw=True)
        self.assertEqual((status, headers['Content-Type']), (200, 'application/x-x509-ca-cert'))
        folder = self.settings.db_path.parent / 'serveur'
        self.assertEqual(body, (folder / 'autorite.pem').read_bytes())
        self.assertIn(b'BEGIN CERTIFICATE', body)
        self.assertNotIn(b'PRIVATE KEY', body)                                # un certificat public, aucune clé
        untrusting = http.client.HTTPSConnection('127.0.0.1', self.port, timeout=5, context=ssl.create_default_context())
        with self.assertRaises(ssl.SSLError):                                 # sans l'autorité installée : refus net
            untrusting.request('GET', '/')
        untrusting.close()

    def test_a_client_that_does_not_speak_tls_changes_nothing(self):
        plain = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        with self.assertRaises((OSError, http.client.HTTPException)):
            plain.request('GET', '/')
            plain.getresponse()
        plain.close()
        self.assertEqual(self.call('GET', '/', cookie=None)[0], 200)


if __name__ == '__main__':
    unittest.main()
