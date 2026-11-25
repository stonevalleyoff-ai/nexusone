"""Le tunnel privé : réglages WireGuard corrects, clés jamais gardées plus que nécessaire, autorité de la maison bornée."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import subprocess
import tempfile
import unittest

from nexus import server
from nexus.ollama import OllamaError
from nexus.vpn import SERVER_ADDRESS, Vpn, check_endpoint


class FakeWg:
    """Faux `wg` et `qrencode` : des clés reconnaissables, pour suivre où elles vont."""

    def __init__(self):
        self.count = 0
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], stdin: bytes = b'') -> tuple[int, bytes]:
        self.calls.append(argv)
        if argv[0] == 'qrencode':
            return 0, '█▀▀█ code QR █▀▀█\n'.encode()
        if argv[1] == 'pubkey':
            return 0, ('P' + stdin.decode().strip()[1:]).encode() + b'\n'       # publique dérivée de la privée
        self.count += 1
        return 0, (('S' if argv[1] == 'genkey' else 'K') + f'{self.count:042d}' + '=').encode() + b'\n'


class VpnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wg = FakeWg()
        self.vpn = Vpn(Path(self.tmp.name) / 'vpn', self.wg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_setup_then_devices(self):
        self.assertFalse(self.vpn.configured())
        with self.assertRaises(OllamaError):
            self.vpn.add_device('Téléphone')
        state = self.vpn.setup('203.0.113.7')
        self.assertEqual((state['adresse_publique'], state['port']), ('203.0.113.7', 51820))
        phone = self.vpn.add_device('Pixel de Léa')
        tablet = self.vpn.add_device('Tablette')
        server_conf = self.vpn.config_path.read_text()
        # le réglage du téléphone : sa clé, et seul le PC passe par le tunnel
        self.assertIn('Address = 10.73.0.2/32', phone)
        self.assertIn('Endpoint = 203.0.113.7:51820', phone)
        self.assertIn(f'AllowedIPs = {SERVER_ADDRESS}/32', phone)
        self.assertNotIn('0.0.0.0/0', phone)
        self.assertIn(f"PublicKey = {state['cle_publique']}", phone)
        self.assertIn('Address = 10.73.0.3/32', tablet)
        # le réglage du PC : une section par appareil, chacun limité à sa propre adresse
        self.assertIn('Address = 10.73.0.1/24', server_conf)
        self.assertEqual(server_conf.count('[Peer]'), 2)
        self.assertIn('# Pixel de Léa', server_conf)
        self.assertIn('AllowedIPs = 10.73.0.2/32', server_conf)
        self.assertNotIn('PostUp', server_conf)                    # aucun relais vers le reste du réseau
        self.assertEqual([d['nom'] for d in self.vpn.devices()], ['Pixel de Léa', 'Tablette'])

    def test_the_phone_private_key_is_shown_once_and_never_kept(self):
        self.vpn.setup('203.0.113.7')
        phone = self.vpn.add_device('Téléphone')
        private = next(line.split(' = ')[1] for line in phone.splitlines() if line.startswith('PrivateKey'))
        kept = ''.join(p.read_text() for p in self.vpn.base.iterdir() if p.is_file())
        self.assertNotIn(private, kept)                            # NEXUS ne garde que la clé publique de l'appareil
        self.assertIn('P' + private[1:], kept)
        for path in (self.vpn.state_path, self.vpn.config_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.vpn.base.stat().st_mode), 0o700)
        shared = [line for line in phone.splitlines() if line.startswith('PresharedKey')]
        self.assertEqual(len(shared), 1)                           # une clé partagée de plus, propre à l'appareil
        self.assertIn('code QR', self.vpn.qr(phone))

    def test_remove_and_reuse(self):
        self.vpn.setup('203.0.113.7')
        self.vpn.add_device('A')
        self.vpn.add_device('B')
        self.assertTrue(self.vpn.remove_device('A'))
        self.assertFalse(self.vpn.remove_device('A'))
        conf = self.vpn.config_path.read_text()
        self.assertEqual((conf.count('[Peer]'), '# A' in conf), (1, False))
        self.assertIn('Address = 10.73.0.2/32', self.vpn.add_device('C'))      # l'adresse libérée resservira
        with self.assertRaises(ValueError):
            self.vpn.add_device('B')                                           # nom déjà pris
        with self.assertRaises(ValueError):
            self.vpn.add_device('   ')
        # relancer « installer » change l'adresse publique sans changer les clés ni perdre les appareils
        before = self.vpn.state()['cle_publique']
        state = self.vpn.setup('maison.exemple.fr', 51999)
        self.assertEqual((state['cle_publique'], len(state['appareils'])), (before, 2))
        self.assertIn('ListenPort = 51999', self.vpn.config_path.read_text())

    def test_inputs_cannot_inject_settings(self):
        self.vpn.setup('2001:db8::7')
        phone = self.vpn.add_device('Tel]\n[Peer]\nPublicKey = pirate # = \\')
        self.assertIn('Endpoint = [2001:db8::7]:51820', phone)
        conf = self.vpn.config_path.read_text()
        self.assertEqual(conf.count('[Peer]'), 1)                  # le nom n'a pas pu ouvrir une seconde section
        self.assertNotIn('pirate #', conf.replace('# TelPeer PublicKey pirate', ''))
        for bad in ('', 'maison', 'a b.fr', '1.2.3.4; rm -rf /', 'http://x.fr', '203.0.113.7:51820'):
            with self.assertRaises(ValueError, msg=bad):
                check_endpoint(bad)
        self.assertEqual(check_endpoint(' Maison.Exemple.FR '), 'maison.exemple.fr')
        with self.assertRaises(ValueError):
            self.vpn.setup('203.0.113.7', 80)
        self.vpn.state_path.write_text('{"cle_privee": "x"}')
        self.assertIsNone(self.vpn.state())                        # fichier abîmé : considéré comme non préparé
        self.assertIn('pas préparé', self.vpn.describe()[0])

    def test_commands_are_printed_not_run(self):
        self.vpn.setup('203.0.113.7')
        commands = self.vpn.install_commands()
        self.assertTrue(all(c.startswith('sudo ') for c in commands))
        self.assertIn('wg-quick@nexus', commands[1])
        self.assertEqual({c[0] for c in self.wg.calls}, {'wg'})    # NEXUS n'a lancé que la création de clés

    # wg-quick se relance lui-même avec sudo quand il n'est pas root : un test ne doit jamais demander de droits.
    @unittest.skipUnless(shutil.which('wg-quick') and os.geteuid() == 0, 'wireguard-tools absent, ou pas root')
    def test_real_keys_are_accepted_by_wireguard(self):
        real = Vpn(Path(self.tmp.name) / 'reel')
        real.setup('203.0.113.7')
        phone = real.add_device('Téléphone')
        for name, text in (('pc.conf', real.config_path.read_text()), ('tel.conf', phone)):
            path = Path(self.tmp.name) / name
            path.write_text(text)
            path.chmod(0o600)
            stripped = subprocess.run(['wg-quick', 'strip', str(path)], capture_output=True, text=True)
            self.assertEqual(stripped.returncode, 0, stripped.stderr)          # l'outil officiel lit le fichier sans erreur
            self.assertIn('PublicKey', stripped.stdout)


@unittest.skipUnless(shutil.which('openssl'), 'openssl absent')
class AuthorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / 'serveur'

    def tearDown(self):
        self.tmp.cleanup()

    def openssl(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(['openssl', *argv], capture_output=True, text=True, cwd=self.folder)

    def test_only_home_addresses_are_certified(self):
        server.ensure_certificate(self.folder, ['10.73.0.1', '192.168.1.20', '192.0.2.2', '8.8.8.8', '2001:db8::1'])
        names = self.openssl('x509', '-in', 'certificat.pem', '-noout', '-ext', 'subjectAltName').stdout
        for expected in ('IP Address:10.73.0.1', 'IP Address:192.168.1.20', 'DNS:nexus.home.arpa', 'IP Address:127.0.0.1'):
            self.assertIn(expected, names)
        for excluded in ('8.8.8.8', '192.0.2.2', '2001'):
            self.assertNotIn(excluded, names)
        self.assertEqual(self.openssl('verify', '-CAfile', 'autorite.pem', 'certificat.pem').returncode, 0)
        for private in ('autorite.cle', 'cle.pem'):
            self.assertEqual(stat.S_IMODE((self.folder / private).stat().st_mode), 0o600)

    def test_a_stolen_authority_key_cannot_impersonate_an_internet_site(self):
        server.ensure_authority(self.folder)
        (self.folder / 'ext.cnf').write_text('subjectAltName=DNS:www.mabanque.example,IP:8.8.8.8\nbasicConstraints=critical,CA:FALSE\n')
        self.openssl('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-keyout', 'faux.cle',
                     '-out', 'faux.csr', '-subj', '/CN=www.mabanque.example')
        self.assertEqual(self.openssl('x509', '-req', '-in', 'faux.csr', '-CA', 'autorite.pem', '-CAkey', 'autorite.cle',
                                      '-CAcreateserial', '-out', 'faux.pem', '-days', '30', '-extfile', 'ext.cnf').returncode, 0)
        verdict = self.openssl('verify', '-CAfile', 'autorite.pem', 'faux.pem')
        self.assertNotEqual(verdict.returncode, 0)
        self.assertIn('permitted subtree violation', verdict.stderr + verdict.stdout)

    def test_the_authority_is_made_once_and_the_certificate_renewed_when_needed(self):
        import os
        import time
        server.ensure_certificate(self.folder, ['10.73.0.1'])
        authority = (self.folder / 'autorite.pem').read_bytes()
        first = (self.folder / 'certificat.pem').read_bytes()
        server.ensure_certificate(self.folder, ['10.73.0.1'])
        self.assertEqual((self.folder / 'certificat.pem').read_bytes(), first)          # rien n'a changé : pas refait
        server.ensure_certificate(self.folder, ['10.73.0.1', '192.168.1.50'])            # nouvelle adresse du PC
        second = (self.folder / 'certificat.pem').read_bytes()
        self.assertNotEqual(second, first)
        old = time.time() - 320 * 86400
        os.utime(self.folder / 'certificat.pem', (old, old))                             # il approche de son terme
        server.ensure_certificate(self.folder, ['10.73.0.1', '192.168.1.50'])
        self.assertNotEqual((self.folder / 'certificat.pem').read_bytes(), second)
        self.assertEqual((self.folder / 'autorite.pem').read_bytes(), authority)         # l'autorité, elle, ne change jamais
        context = ssl.create_default_context(cafile=str(self.folder / 'autorite.pem'))   # et le téléphone lui fait toujours confiance
        self.assertEqual(len(context.get_ca_certs()), 1)


if __name__ == '__main__':
    unittest.main()
