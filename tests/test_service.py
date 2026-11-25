"""NEXUS en service du PC : le fichier systemd, son enfermement, les commandes affichées."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from nexus.config import Settings
from nexus.ollama import OllamaError
from nexus.service import HARDENING, Plan, Service, quoted

ANALYZE = shutil.which('systemd-analyze')


def plan(**changes) -> Plan:
    base = dict(user='lea', python='/usr/bin/python3', code=Path('/home/lea/nexus'), mode='vpn', port=8765,
                writable=[Path('/home/lea/.local/share/nexus'), Path('/home/lea/.config/nexus'), Path('/home/lea/Documents')],
                settings={}, readable=[Path('/home/lea/nexus')])
    base.update(changes)
    return Plan(**base)


class UnitTest(unittest.TestCase):
    def test_the_unit_runs_nexus_as_the_user_and_never_gives_up(self):
        text = plan().text()
        self.assertIn('User=lea\n', text)
        self.assertNotIn('User=root', text)
        self.assertIn('ExecStart=/usr/bin/python3 -m nexus serveur --vpn --port 8765\n', text)
        self.assertIn('WorkingDirectory=/home/lea/nexus\n', text)
        for line in ('Restart=always', 'StartLimitIntervalSec=0', 'KillSignal=SIGINT', 'WantedBy=multi-user.target',
                     'Environment="NEXUS_SERVICE=1"'):
            self.assertIn(line + '\n', text)
        # dans le tunnel seulement : le service attend le tunnel
        self.assertIn('After=network-online.target ollama.service wg-quick@nexus.service\n', text)
        self.assertIn('Wants=network-online.target wg-quick@nexus.service\n', text)
        home = plan(mode='reseau', port=9000).text()
        self.assertIn('serveur --reseau --port 9000\n', home)
        self.assertNotIn('wg-quick', home)
        self.assertIn('ExecStart=/usr/bin/python3 -m nexus serveur --port 8765\n', plan(mode='local').text())

    def test_the_unit_is_locked_down(self):
        text = plan().text()
        for directive, _reason in HARDENING:
            self.assertIn('\n' + directive + '\n', text)       # seule sur sa ligne : systemd refuse un commentaire à la suite
        for needed in ('NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=tmpfs', 'PrivateDevices=yes',
                       'CapabilityBoundingSet=', 'RestrictNamespaces=yes', 'SystemCallFilter=@system-service'):
            self.assertIn(needed, [d for d, _r in HARDENING])
        denied = ' '.join(d for d, _r in HARDENING if d.startswith('SystemCallFilter=~'))
        for call in ('setuid', 'setresuid', 'setgroups', 'chroot', 'pivot_root', 'bpf', '@module', '@raw-io', '@clock', '@chown'):
            self.assertIn(call, denied)
        self.assertNotIn('capset', denied)                 # le navigateur du journal en a besoin pour abandonner ses droits
        # Du dossier personnel, seuls ces dossiers existent pour lui ; le code est en lecture seule.
        self.assertEqual([l for l in text.splitlines() if l.startswith('BindPaths=')],
                         ['BindPaths=-/home/lea/.local/share/nexus', 'BindPaths=-/home/lea/.config/nexus',
                          'BindPaths=-/home/lea/Documents'])
        self.assertEqual([l for l in text.splitlines() if l.startswith('BindReadOnlyPaths=')],
                         ['BindReadOnlyPaths=/home/lea/nexus'])
        self.assertEqual(text.count('ReadWritePaths=-'), 3)
        # Hors dossier personnel : pas besoin de le rendre visible, seulement inscriptible.
        other = plan(code=Path('/opt/nexus'), readable=[Path('/opt/nexus')],
                     writable=[Path('/home/lea/.local/share/nexus'), Path('/srv/documents')]).text()
        self.assertNotIn('BindReadOnlyPaths', other)
        self.assertIn('ReadWritePaths=-/srv/documents\n', other)
        self.assertNotIn('BindPaths=-/srv/documents', other)

    def test_strange_values_cannot_inject_directives(self):
        self.assertEqual(quoted('/home/léa/Mes documents'), '"/home/léa/Mes documents"')
        for bad in ('', '/x\nExecStartPre=/bin/sh', '/x"y', 'a\\b', '/x/%h', '$HOME/x', "/x'y"):
            with self.assertRaises(ValueError, msg=bad):
                quoted(bad)
        with self.assertRaises(ValueError):
            plan(user='lea\nUser=root').text()
        with self.assertRaises(ValueError):
            plan(writable=[Path('/home/lea/a:b')]).text()
        with self.assertRaises(ValueError):
            plan(settings={'NEXUS_MODEL': 'x"\nExecStart=/bin/sh'}).text()
        with self.assertRaises(ValueError):
            plan(port=80).text()
        with self.assertRaises(ValueError):
            plan(mode='ouvert').text()
        kept = plan(settings={'NEXUS_BIBLIOTHEQUE': '/home/léa/Mes documents'}).text()
        self.assertIn('Environment="NEXUS_BIBLIOTHEQUE=/home/léa/Mes documents"\n', kept)

    @unittest.skipUnless(ANALYZE, 'systemd-analyze absent')
    def test_systemd_itself_accepts_the_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nexus.service'
            path.write_text(plan(python='/usr/bin/python3', user='root', writable=[
                Path('/home/léa/.local/share/nexus'), Path('/home/léa/Mes documents')],
                settings={'NEXUS_BIBLIOTHEQUE': '/home/léa/Mes documents'}).text(), encoding='utf-8')
            done = subprocess.run([ANALYZE, 'verify', str(path)], capture_output=True, text=True, timeout=60)
            complaints = [line for line in (done.stdout + done.stderr).splitlines()
                          if 'nexus.service' in line and 'wg-quick' not in line and 'ollama' not in line]
            self.assertEqual(complaints, [])


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(ollama_url='http://127.0.0.1:1', model=None, db_path=root / 'data' / 'nexus.sqlite3',
                                 rules_path=root / 'config' / 'rules.json', num_ctx=8192, library_root=root / 'Documents',
                                 autonomy_levels=root / 'config' / 'autonomie.json', engine_dir=root / 'data' / 'moteur')
        self.calls: list[list[str]] = []
        self.answers = {'is-active': 'active', 'is-enabled': 'enabled'}

        def run(argv):
            self.calls.append(argv)
            return 0, self.answers.get(argv[1], '')

        self.service = Service(self.settings, run, environ={'NEXUS_MODEL': 'qwen3.5:2b', 'PATH': '/bin', 'SECRET': 'x'},
                               home=root / 'maison', system_unit=root / 'etc' / 'nexus.service')

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepare_writes_the_unit_at_home_and_changes_nothing_in_the_system(self):
        root = Path(self.tmp.name)
        with mock.patch('os.geteuid', return_value=1000):
            made = self.service.prepare('reseau', 8765)
        text = self.service.prepared.read_text()
        self.assertEqual(text, made.text())
        self.assertEqual(made.writable, [root / 'data', root / 'config', root / 'Documents'])     # le moteur est dans les données
        self.assertTrue((root / 'Documents').is_dir())
        self.assertIn('Environment="NEXUS_MODEL=qwen3.5:2b"', text)        # le réglage choisi suit dans le service
        self.assertNotIn('SECRET', text)                                    # pas le reste de l'environnement
        self.assertNotIn('PATH=', text)
        self.assertFalse(self.service.system_unit.exists())
        self.assertEqual(self.calls, [])                                    # aucune commande lancée, encore moins sudo
        launcher = self.service.launcher
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn('-m nexus "$@"', launcher.read_text())
        commands = self.service.install_commands()
        self.assertEqual(len(commands), 4)
        self.assertTrue(all(c.startswith('sudo ') for c in commands))
        self.assertIn(str(self.service.prepared), commands[0])
        self.assertEqual(commands[2:], ['sudo systemctl enable nexus.service', 'sudo systemctl restart nexus.service'])
        self.assertTrue(all(c.startswith('sudo ') for c in self.service.remove_commands()))

    def test_with_official_versions_the_service_follows_the_link(self):
        root = Path(self.tmp.name)
        app = root / 'nexus-programme'                      # à côté des données, jamais dedans
        (app / 'versions' / '0.28.0-abc').mkdir(parents=True)
        os.symlink(Path('versions') / '0.28.0-abc', app / 'actuelle')
        with mock.patch('os.geteuid', return_value=1000):
            plan = self.service.prepare('vpn', 8765)
        self.assertEqual(plan.code, app / 'actuelle')
        self.assertIn(f'WorkingDirectory={app}/actuelle\n', plan.text())       # le lien, pas la version : une mise à jour suffit
        self.assertEqual(plan.readable[0], app)
        self.assertNotIn(app, plan.writable)                                 # il ne peut pas réécrire son propre code
        self.assertFalse(any(app == w or w in app.parents for w in plan.writable))
        self.assertIn(f'cd {app}/actuelle && exec', self.service.launcher.read_text())

    def test_root_is_refused(self):
        with mock.patch('os.geteuid', return_value=0), self.assertRaises(OllamaError):
            self.service.prepare('vpn', 8765)

    def test_state_and_outdated_unit(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.service.prepare('vpn', 8765)
        self.assertFalse(self.service.state()['installe'])
        self.assertEqual(self.calls, [])
        self.service.system_unit.parent.mkdir()
        shutil.copy(self.service.prepared, self.service.system_unit)
        state = self.service.state()
        self.assertEqual((state['installe'], state['a_jour'], state['actif'], state['au_demarrage']),
                         (True, True, 'active', 'enabled'))
        with mock.patch('os.geteuid', return_value=1000):
            self.service.prepare('reseau', 8765)                            # réglage changé, pas encore réinstallé
        self.assertIs(self.service.state()['a_jour'], False)
        self.answers['is-active'] = 'failed'
        with mock.patch.object(Path, 'is_dir', return_value=True):
            lines = self.service.describe()
        self.assertIn('en échec', lines[0])
        self.assertIn('nexus service installer', lines[1])

    def test_missing_programs_are_listed_with_the_package_to_install(self):
        with mock.patch('shutil.which', side_effect=lambda name, path=None: None if name in ('wg', 'ollama') else '/usr/bin/' + name), \
                mock.patch.object(Path, 'is_dir', return_value=True):
            self.assertEqual([m[0] for m in self.service.missing()], ['ollama', 'wg'])
            text = '\n'.join(self.service.describe())
        self.assertIn('il manque « ollama »', text)
        self.assertIn('indispensable', text)
        self.assertIn('sudo apt install wireguard-tools', text)

    def test_journal_explains_when_it_cannot_be_read(self):
        self.assertIn('sudo journalctl -u nexus', self.service.journal())
        self.answers['-u'] = 'nov. 25 10:00 nexus[1]: NEXUS — serveur maison'
        self.assertIn('serveur maison', self.service.journal())


if __name__ == '__main__':
    unittest.main()
