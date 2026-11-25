"""Les mises à jour : un vrai dépôt git local joue le dépôt officiel."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from nexus import update as update_module
from nexus.ollama import OllamaError
from nexus.planner import Planner, Runner
from nexus.update import Updater, clean, dotted, parse_version

GIT = shutil.which('git')
NOW = datetime(2026, 11, 25, 5, 10)


class Source:
    """Le dépôt officiel, en petit : un paquet « nexus » avec son numéro de version, et un test."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir()
        self.git('init', '-q', '-b', 'main')

    def git(self, *argv: str) -> str:
        return subprocess.run([GIT, '-C', str(self.folder), '-c', 'user.name=t', '-c', 'user.email=t@t',
                               '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false', *argv],
                              check=True, capture_output=True, text=True).stdout.strip()

    def publish(self, version: str, *, tests_pass: bool = True, declared: str | None = None, notes: str = '',
                extra: dict[str, str] | None = None, tag: bool = True) -> str:
        (self.folder / 'nexus').mkdir(exist_ok=True)
        (self.folder / 'tests').mkdir(exist_ok=True)
        (self.folder / 'nexus' / '__init__.py').write_text(f"__version__ = '{declared or version}'\n")
        (self.folder / 'tests' / '__init__.py').write_text('')
        (self.folder / 'tests' / 'test_tout.py').write_text(
            'import unittest\n\n\nclass T(unittest.TestCase):\n    def test_la_version(self):\n'
            f'        self.assertTrue({tests_pass})\n')
        for name, content in (extra or {}).items():
            (self.folder / name).write_text(content)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', f'version {version}')
        if tag:
            self.git('tag', '-a', f'v{version}', '-m', notes or f'NEXUS {version}')
        return self.git('rev-parse', 'HEAD')


@unittest.skipUnless(GIT, 'git absent')
class UpdateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = Source(root / 'officiel')
        self.first = self.source.publish('1.0.0', notes='Première version.')
        self.data = root / 'data'
        self.data.mkdir()
        self.database = self.data / 'agenda.sqlite3'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE lignes (texte TEXT)')
            db.execute("INSERT INTO lignes VALUES ('avant')")
        db.close()
        self.app = root / 'nexus-programme'
        self.updater = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, code: Path | None = None) -> Updater:
        return Updater(self.app, self.data, source=str(self.source.folder), code=code or Path(self.tmp.name) / 'travail',
                       python=sys.executable, databases=[self.database])

    def install(self, version: str | None = None, **options) -> dict:
        return self.updater.install(self.updater.plan(version), say=lambda _line: None, **options)


class PlainTest(unittest.TestCase):
    def test_versions_and_notes(self):
        self.assertEqual(parse_version('v0.28.1'), (0, 28, 1))
        self.assertEqual(parse_version('0.28.1'), (0, 28, 1))
        for bad in ('v1', '1.2', 'v1.2.3-rc1', 'v1.2.3.4', 'latest', 'v1.2.x', ''):
            self.assertIsNone(parse_version(bad), bad)
        self.assertEqual(dotted((0, 28, 1)), '0.28.1')
        self.assertGreater(parse_version('0.100.0'), parse_version('0.99.9'))        # des nombres, pas du texte
        self.assertEqual(clean('Corrige\x1b[31m la voix\x00.\n\n  suite  \n'), 'Corrige[31m la voix.\n\n  suite')
        with self.assertRaises(ValueError):
            Updater(Path('/x'), Path('/y'), source='ext::sh -c "id"')                # pas de commande déguisée en adresse


class CheckTest(UpdateCase):
    def test_only_real_version_tags_count(self):
        self.source.git('tag', 'v9')
        self.source.git('tag', 'v2.0.0-rc1')
        self.source.git('tag', 'derniere')
        second = self.source.publish('1.1.0')
        remote = self.updater.remote_versions()
        self.assertEqual(sorted(remote), [(1, 0, 0), (1, 1, 0)])
        self.assertEqual(remote[(1, 1, 0)], {'etiquette': 'v1.1.0', 'commit': second})     # le commit, pas l'objet étiquette

    def test_a_newer_version_is_announced_never_installed(self):
        self.install()
        self.assertEqual(self.updater.check(NOW), {'ok': True, 'actuelle': '1.0.0', 'disponible': None, 'deplacees': []})
        self.assertIn('À jour (vérifié le 2026-11-25 à 05:10)', '\n'.join(self.updater.describe()))
        self.source.publish('1.1.0')
        self.assertEqual(self.updater.check(NOW)['disponible'], '1.1.0')
        self.assertEqual(self.updater.installed()['version'], '1.0.0')                    # rien n'a bougé
        self.assertIn('Version 1.1.0 disponible', '\n'.join(self.updater.describe()))

    def test_an_unreachable_source_is_reported(self):
        shutil.move(str(self.source.folder), str(self.source.folder) + '-ailleurs')
        result = self.updater.check(NOW)
        self.assertFalse(result['ok'])
        self.assertIn('injoignable', self.updater.state()['erreur'])
        shutil.move(str(self.source.folder) + '-ailleurs', str(self.source.folder))

    def test_a_published_version_that_changes_is_refused(self):
        self.source.publish('1.1.0')
        self.assertEqual(self.updater.check(NOW)['deplacees'], [])
        # Quelqu'un réécrit la version 1.1.0 après coup (compte piraté, ou erreur).
        self.source.publish('1.1.0', extra={'porte.txt': 'ouverte'}, tag=False)
        self.source.git('tag', '-f', '-a', 'v1.1.0', '-m', 'la même, promis')
        self.assertEqual(self.updater.check(NOW)['deplacees'], ['v1.1.0'])
        with self.assertRaises(OllamaError) as refused:
            self.updater.plan()
        self.assertIn('ne désigne plus le code annoncé', str(refused.exception))
        self.assertIn('ALERTE', '\n'.join(self.updater.describe()))
        self.assertFalse(self.updater.current.exists())

    def test_the_nightly_task_announces_once_per_version(self):
        self.install()
        planner = Planner(':memory:')
        runner = Runner(planner, updater=self.updater)
        task = planner.get('veille-versions')
        self.assertEqual(runner.run(task, NOW), 'à jour (1.0.0)')
        self.assertEqual(planner.notifications(), [])
        self.source.publish('1.1.0')
        for _ in range(2):
            self.assertEqual(runner.run(task, NOW), 'version 1.1.0 disponible')
        notes = planner.notifications()
        self.assertEqual([(n['niveau'], n['titre']) for n in notes], [('info', 'NEXUS 1.1.0 est disponible')])
        self.assertIn('nexus mise-a-jour installer', notes[0]['texte'])
        self.assertEqual(Runner(planner).run(task, NOW), 'mises à jour non configurées')
        planner.close()


class InstallTest(UpdateCase):
    def test_install_puts_the_version_beside_and_switches_a_link(self):
        result = self.install()
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['version'], result['commit'], result['notes']), ('1.0.0', self.first, 'Première version.'))
        folder = self.app / 'versions' / f'1.0.0-{self.first[:12]}'
        self.assertEqual(self.updater.current.resolve(), folder.resolve())
        self.assertEqual(os.readlink(self.updater.current), f'versions/{folder.name}')      # relatif : juste aussi vu de la cage
        self.assertEqual(self.updater.installed(), {'version': '1.0.0', 'commit': self.first, 'dossier': folder.resolve()})
        self.assertFalse((folder / '.git').exists())                                      # du code, pas un dépôt
        self.assertFalse(self.updater.previous.exists())
        self.assertFalse(self.updater.managed())                                          # la commande tournait d'ailleurs
        self.assertTrue(self.make(code=folder).managed())
        # la photo des bases d'avant
        copy = self.data / 'avant-mise-a-jour' / 'ancienne-vers-1.0.0' / 'agenda.sqlite3'
        with sqlite3.connect(copy) as db:
            self.assertEqual(db.execute('SELECT texte FROM lignes').fetchone()[0], 'avant')
        db.close()

    def test_a_second_version_keeps_the_first_for_the_way_back(self):
        self.install()
        second = self.source.publish('1.1.0')
        third = self.source.publish('1.2.0')
        self.assertTrue(self.install('1.1.0')['ok'])
        self.assertEqual((self.updater.installed()['commit'], self.updater.version_of(self.updater.previous.resolve())),
                         (second, '1.0.0'))
        self.assertTrue(self.install()['ok'])                                             # la dernière : 1.2.0
        self.assertEqual(self.updater.installed()['commit'], third)
        self.assertIn('Version précédente gardée : 1.1.0', '\n'.join(self.updater.describe()))
        back = self.updater.rollback()
        self.assertEqual((back['version'], self.updater.installed()['version']), ('1.1.0', '1.1.0'))
        self.assertEqual(self.updater.version_of(self.updater.previous.resolve()), '1.2.0')   # et on peut y retourner
        self.updater.tidy()
        self.assertEqual(sorted(p.name.split('-')[0] for p in (self.app / 'versions').iterdir()), ['1.1.0', '1.2.0'])
        with self.assertRaises(ValueError):
            self.updater.plan('7.7.7')

    def test_a_version_failing_its_own_tests_changes_nothing(self):
        self.install()
        self.source.publish('1.1.0', tests_pass=False)
        result = self.install()
        self.assertEqual((result['ok'], result['etape']), (False, 'tests'))
        self.assertIn('rien n\'a été changé', result['message'])
        self.assertIn('test_la_version', result['message'])
        self.assertEqual(self.updater.installed()['version'], '1.0.0')
        self.assertFalse(self.updater.previous.exists())
        self.assertTrue(self.install(skip_tests=True)['ok'])                              # possible, mais il faut le demander

    def test_the_approved_fingerprint_is_the_one_installed(self):
        plan = self.updater.plan()
        self.source.publish('1.0.0', extra={'porte.txt': 'ouverte'}, tag=False)           # la version est réécrite entre
        self.source.git('tag', '-f', '-a', 'v1.0.0', '-m', 'x')                           # l'approbation et le téléchargement
        with self.assertRaises(OllamaError) as refused:
            self.updater.install(plan, say=lambda _line: None)
        self.assertIn("n'a pas l'empreinte annoncée", str(refused.exception))
        self.assertFalse(self.updater.current.exists())

    def test_code_lying_about_its_version_or_hiding_links_is_refused(self):
        self.source.publish('1.1.0', declared='9.9.9')
        with self.assertRaises(OllamaError) as refused:
            self.install()
        self.assertIn('ne se dit pas en version 1.1.0', str(refused.exception))
        os.symlink('/etc/passwd', self.source.folder / 'lien')
        self.source.publish('1.2.0')
        with self.assertRaises(OllamaError) as refused:
            self.install()
        self.assertIn('entrée inattendue', str(refused.exception))
        self.assertFalse(self.updater.current.exists())
        self.assertEqual([p.name for p in (self.app / 'versions').iterdir()], [])         # rien de posé à moitié

    def test_the_tests_never_see_the_real_home(self):
        self.source.publish('1.1.0', extra={'tests/test_maison.py':
            'import os, unittest\n\n\nclass M(unittest.TestCase):\n    def test_maison(self):\n'
            f"        self.assertNotEqual(os.environ['HOME'], {os.environ.get('HOME', '')!r})\n"
            "        self.assertEqual(sorted(os.environ), ['HOME', 'LANG', 'PATH', 'PYTHONDONTWRITEBYTECODE', 'TMPDIR'])\n"})
        self.assertTrue(self.install()['ok'])


class HandOverTest(UpdateCase):
    """Le serveur en marche : il note sa version en démarrant, et laisse la main quand le lien change."""

    def setUp(self):
        super().setUp()
        for name, value in (('HANDOVER', 1.0), ('START_TIMEOUT', 3.0), ('STEADY', 0.3)):
            self.addCleanup(setattr, update_module, name, getattr(update_module, name))
            setattr(update_module, name, value)
        self.install()
        self.server = subprocess.Popen([sys.executable, '-c', 'import signal, time; '       # « le serveur »
                                        'signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(120)'])
        self.addCleanup(self.stop)
        self.mark(self.updater.current.resolve())
        self.follow = threading.Event()

    def stop(self):
        self.follow.set()
        self.server.kill()
        self.server.wait()

    def mark(self, folder: Path) -> None:
        self.updater.running_path.parent.mkdir(parents=True, exist_ok=True)
        self.updater.running_path.write_text(json.dumps({'version': '?', 'pid': self.server.pid, 'code': str(folder)}))

    def service(self, starts: bool) -> None:
        """Imite systemd : quand le lien change, le serveur repart… ou pas, si la nouvelle version est cassée."""
        def loop():
            seen = self.updater.current.resolve()
            while not self.follow.wait(0.05):
                now = self.updater.current.resolve()
                if now != seen:
                    seen = now
                    if starts or self.updater.version_of(now) == '1.0.0':
                        self.mark(now)
        threading.Thread(target=loop, daemon=True).start()

    def test_the_running_server_is_seen_and_knows_when_it_is_replaced(self):
        folder = self.updater.current.resolve()
        inside = self.make(code=folder)
        inside.announce_running()
        mark = inside.running()
        self.assertEqual((mark['version'], mark['pid'], mark['code']), ('1.0.0', os.getpid(), str(folder)))
        self.assertFalse(inside.outdated())
        self.source.publish('1.1.0')
        self.assertTrue(self.updater.install(self.updater.plan(), say=lambda _l: None, wait=False)['ok'])
        self.assertTrue(inside.outdated())                                   # l'ancien serveur doit laisser la main
        self.assertFalse(self.make(code=self.updater.current.resolve()).outdated())
        self.assertFalse(self.updater.outdated())                            # une commande lancée d'ailleurs n'est pas concernée
        self.updater.running_path.write_text(json.dumps({'pid': 2**22 + 12345, 'code': 'x'}))
        self.assertIsNone(self.updater.running())                            # processus disparu : pas de serveur

    def test_the_service_moves_to_the_new_version(self):
        self.source.publish('1.1.0')
        self.service(starts=True)
        result = self.install()
        self.assertEqual((result['ok'], result['redemarre'], result['version']), (True, True, '1.1.0'))
        self.assertEqual(self.updater.installed()['version'], '1.1.0')

    def test_a_version_that_does_not_stay_up_is_rolled_back(self):
        self.source.publish('1.1.0')
        self.service(starts=False)
        result = self.install()
        self.assertEqual((result['ok'], result['etape']), (False, 'demarrage'))
        self.assertIn('retour à la version 1.0.0', result['message'])
        self.assertIn('qui tourne de nouveau', result['message'])
        self.assertEqual(self.updater.installed()['version'], '1.0.0')       # revenu tout seul
        self.assertFalse(self.updater.previous.is_symlink())                 # la version fautive n'est pas « la précédente »
        with self.assertRaises(OllamaError):
            self.updater.rollback()                                          # « annuler » ne peut pas y ramener
        self.assertEqual(self.updater.state()['refusee']['version'], '1.1.0')
        # Elle n'est plus annoncée ni proposée ; la suivante le sera.
        self.assertIsNone(self.updater.check(NOW)['disponible'])
        self.assertIn('essayée et refusée', '\n'.join(self.updater.describe()))
        self.assertEqual(self.updater.plan()['version'], '1.0.0')
        self.assertEqual(self.updater.plan('1.1.0')['version'], '1.1.0')    # la redemander par son numéro reste possible
        self.source.publish('1.2.0')
        self.assertEqual(self.updater.check(NOW)['disponible'], '1.2.0')
        self.assertEqual(self.updater.plan()['version'], '1.2.0')

    def test_no_running_server_means_nothing_to_wait_for(self):
        self.stop()
        self.source.publish('1.1.0')
        started = time.monotonic()
        result = self.install()
        self.assertEqual((result['ok'], result['redemarre']), (True, False))
        self.assertLess(time.monotonic() - started, 20)


if __name__ == '__main__':
    unittest.main()
