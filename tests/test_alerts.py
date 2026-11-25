"""Le relais d'alertes pour téléphone verrouillé : réglages, reprise après coupure, WebSocket."""
import base64
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest

from nexus.alerts import CACHE_HOURS, DISCREET_TEXT, FIRST_HOURS, Relay, ws_accept, ws_frame, ws_parse
from nexus.planner import Planner


def masked(opcode: int, payload: bytes, mask: bytes = b'\x01\x02\x03\x04') -> bytes:
    """Une trame comme en envoie un téléphone (toujours masquée)."""
    assert len(payload) < 65536
    head = bytes([0x80 | opcode]) + (bytes([0x80 | len(payload)]) if len(payload) < 126
                                     else bytes([0x80 | 126]) + len(payload).to_bytes(2, 'big'))
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.planner = Planner(':memory:')
        self.relay = Relay(Path(self.tmp.name) / 'data' / 'alertes.json', self.planner)

    def tearDown(self):
        self.planner.close()
        self.tmp.cleanup()

    def test_off_by_default_and_the_topic_is_a_private_random_key(self):
        self.assertFalse(self.relay.enabled())
        self.assertFalse(self.relay.matches(''))
        self.assertIsNone(self.relay.link('10.73.0.1:8765'))
        topic = self.relay.enable()
        self.assertRegex(topic, r'^nexus-[-_A-Za-z0-9]{24}$')
        self.assertTrue(re.fullmatch(r'[-_A-Za-z0-9]{1,64}', topic))        # la règle de l'appli ntfy
        self.assertEqual(self.relay.enable(), topic)                        # rallumer ne change pas la clé
        self.assertEqual(stat.S_IMODE(os.stat(self.relay.path).st_mode), 0o600)
        self.assertTrue(self.relay.matches(topic))
        self.assertTrue(self.relay.matches('autre,' + topic))               # l'appli regroupe ses sujets par serveur
        self.assertFalse(self.relay.matches(topic[:-1]))
        self.assertFalse(self.relay.matches(','.join(['x'] * 5 + [topic])))
        self.assertEqual(self.relay.link('10.73.0.1:8765'), f'ntfy://10.73.0.1:8765/{topic}?display=NEXUS')
        self.assertTrue(self.relay.link('pc:8765', secure=False).endswith('&secure=false'))
        renewed = self.relay.renew()
        self.assertNotEqual(renewed, topic)
        self.assertFalse(self.relay.matches(topic))
        self.relay.disable()
        self.assertFalse(self.relay.matches(renewed))

    def test_a_damaged_settings_file_means_off(self):
        self.relay.path.parent.mkdir(parents=True)
        for content in ('pas du json', '[]', '{"sujet": "court"}', '{"sujet": "avec/une/barre/oblique/123456"}'):
            self.relay.path.write_text(content)
            self.assertFalse(self.relay.enabled(), content)

    def test_events_carry_what_the_phone_needs(self):
        topic = self.relay.enable()
        self.planner.notify('info', 'Le journal du jour est prêt', 'Onglet Maison.')
        self.planner.notify('warning', 'Rappel : dentiste à 14h30', 'Dans 30 minutes.')
        cursor = self.relay.cursor('all')
        first, second = self.relay.events(cursor, 'https://10.73.0.1:8765/')
        self.assertEqual((first['id'], first['event'], first['topic']), ('nx0000000001', 'message', topic))
        self.assertEqual((first['title'], first['message'], first['priority']), ('Le journal du jour est prêt', 'Onglet Maison.', 3))
        self.assertEqual(first['click'], 'https://10.73.0.1:8765/')
        self.assertNotIn('tags', first)
        self.assertEqual((second['priority'], second['tags']), (4, ['warning']))
        self.assertLess(abs(first['time'] - datetime.now().timestamp()), 120)
        self.assertEqual(first['expires'], first['time'] + CACHE_HOURS * 3600)
        self.assertEqual(self.relay.events(cursor), [])                     # rien de neuf : rien n'est renvoyé deux fois
        self.planner.notify('info', 'Troisième', '')
        self.assertEqual([e['message'] for e in self.relay.events(cursor)], ['Troisième'])       # texte vide : le titre

    def test_discreet_mode_keeps_the_content_off_the_lock_screen(self):
        self.relay.enable()
        self.relay.set_discreet(True)
        self.planner.notify('warning', 'Courrier : 1 courriel important', 'Impôts : avis à payer avant le 15.')
        event = self.relay.events(self.relay.cursor('all'), 'https://pc/')[0]
        self.assertEqual((event['title'], event['message']), ('NEXUS', DISCREET_TEXT))
        self.assertNotIn('tags', event)
        self.assertNotIn('Impôts', str(event))
        self.assertEqual(event['priority'], 4)                              # l'urgence reste : le téléphone vibre

    def test_a_phone_coming_back_gets_only_what_it_missed(self):
        self.relay.enable()
        for n in range(1, 5):
            self.planner.notify('info', f'Alerte {n}', 'texte')
        titles = lambda since: [e['title'] for e in self.relay.events(self.relay.cursor(since))]
        self.assertEqual(titles('all'), ['Alerte 1', 'Alerte 2', 'Alerte 3', 'Alerte 4'])
        self.assertEqual(titles(''), titles('all'))
        self.assertEqual(titles('nx0000000002'), ['Alerte 3', 'Alerte 4'])
        self.assertEqual(titles('none'), [])
        self.assertEqual(titles('latest'), ['Alerte 4'])
        self.assertEqual(titles('10m'), titles('all'))
        self.assertEqual(titles(str(int(datetime.now().timestamp()) - 600)), titles('all'))
        self.assertEqual(titles('n-importe-quoi'), titles('all'))
        self.assertEqual(titles('nx9999999999'), [])                        # numéro inventé : pas d'erreur, rien d'ancien
        cursor = self.relay.cursor('latest')                                # après « latest », la suite arrive normalement
        self.relay.events(cursor)
        self.planner.notify('info', 'Alerte 5', 'texte')
        self.assertEqual([e['title'] for e in self.relay.events(cursor)], ['Alerte 5'])

    def test_old_alerts_are_not_replayed(self):
        self.relay.enable()
        self.planner.notify('info', 'Vieille', 'texte')
        old = (datetime.now() - timedelta(hours=CACHE_HOURS + 1)).isoformat(timespec='minutes')
        self.planner.db.execute('UPDATE notifications SET creee=?', (old,))
        self.planner.notify('info', 'Récente', 'texte')
        self.assertEqual([e['title'] for e in self.relay.events(self.relay.cursor('all'))], ['Récente'])
        self.assertEqual([e['title'] for e in self.relay.events(self.relay.cursor('30d'))], ['Récente'])
        self.assertEqual([e['title'] for e in self.relay.events(self.relay.cursor('nx0000000000'))], ['Récente'])
        # Entre les deux : un téléphone déjà abonné la rattrape, un nouveau venu ne reçoit pas tout l'historique.
        yesterday = (datetime.now() - timedelta(hours=FIRST_HOURS + 1)).isoformat(timespec='minutes')
        self.planner.db.execute('UPDATE notifications SET creee=? WHERE titre=?', (yesterday, 'Vieille'))
        self.assertEqual([e['title'] for e in self.relay.events(self.relay.cursor('nx0000000000'))], ['Vieille', 'Récente'])
        self.assertEqual([e['title'] for e in self.relay.events(self.relay.cursor('all'))], ['Récente'])

    def test_signals(self):
        topic = self.relay.enable()
        opened = self.relay.signal('open')
        self.assertEqual((opened['event'], opened['topic']), ('open', topic))
        self.assertEqual(set(opened), {'id', 'time', 'event', 'topic'})


class WebSocketTest(unittest.TestCase):
    def test_handshake_answer_is_the_one_of_the_standard(self):
        self.assertEqual(ws_accept('dGhlIHNhbXBsZSBub25jZQ=='), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')      # exemple de la RFC 6455

    def test_server_frames(self):
        self.assertEqual(ws_frame(0x1, b'Hello'), b'\x81\x05Hello')                                  # exemple de la RFC 6455
        long = ws_frame(0x1, b'x' * 300)
        self.assertEqual((long[:2], int.from_bytes(long[2:4], 'big'), len(long)), (b'\x81\x7e', 300, 304))
        self.assertEqual(ws_frame(0x1, b'x' * 70000)[1], 127)

    def test_phone_frames_are_unmasked_and_may_arrive_in_pieces(self):
        self.assertEqual(ws_parse(bytes.fromhex('818537fa213d7f9f4d5158')), ([(1, b'Hello')], b''))  # exemple de la RFC 6455
        data = masked(0x9, b'ping') + masked(0x1, b'y' * 200) + masked(0x8, b'\x03\xe8')
        frames, rest = ws_parse(data)
        self.assertEqual((frames, rest), ([(0x9, b'ping'), (0x1, b'y' * 200), (0x8, b'\x03\xe8')], b''))
        collected, rest = [], b''
        for i in range(len(data)):                         # octet par octet : rien n'est perdu, rien n'est inventé
            frames, rest = ws_parse(rest + data[i:i + 1])
            collected += frames
        self.assertEqual((len(collected), rest), (3, b''))

    def test_a_misbehaving_phone_is_refused(self):
        with self.assertRaises(ValueError):
            ws_parse(b'\x81\x05Hello')                     # non masquée
        with self.assertRaises(ValueError):
            ws_parse(masked(0x1, b'z' * 5000))             # trop longue
        with self.assertRaises(ValueError):
            ws_parse(bytes([0x81, 0x80 | 127]) + b'\x00' * 12)
        self.assertEqual(base64.b64decode(ws_accept('x' * 24))[:0], b'')      # une clé farfelue ne fait pas planter


if __name__ == '__main__':
    unittest.main()
