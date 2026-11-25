"""La sauvegarde chiffrée : avec le vrai restic quand il est installé (comme sur le mini-PC)."""
from datetime import datetime
import os
from pathlib import Path
import random
import shutil
import sqlite3
import stat
import tempfile
import unittest

from nexus.backup import FOLDER, Backup, human, new_key, tidy_key
from nexus.ollama import OllamaError
from nexus.planner import Planner, Runner
from tests.test_planner import original_tasks_only

RESTIC = shutil.which('restic')


class KeyTest(unittest.TestCase):
    def test_key_is_long_random_and_easy_to_copy(self):
        key = new_key()
        self.assertRegex(key, r'^([A-HJ-NP-Z2-9]{4}-){5}[A-HJ-NP-Z2-9]{4}$')      # ni 0/O ni 1/I
        self.assertNotEqual(key, new_key())
        self.assertEqual(tidy_key(key.lower().replace('-', ' ')), key)
        self.assertEqual(tidy_key('  ' + key.replace('-', '') + '\n'), key)

    def test_sizes_are_readable(self):
        self.assertEqual((human(0), human(900), human(2048), human(5 * 2**30)), ('0 octets', '900 octets', '2,0 Ko', '5,0 Go'))


class Home:
    """Une petite maison : documents, données avec deux bases, réglages, un « moteur » à ne pas copier."""

    def __init__(self, root: Path, run=None):
        self.root = root
        self.docs, self.data, self.conf = root / 'Documents', root / 'data', root / 'config'
        self.disk = root / 'disque'
        for folder in (self.docs / 'Factures', self.data / 'moteur', self.data / 'journal', self.conf, self.disk):
            folder.mkdir(parents=True)
        (self.docs / 'Factures' / 'edf.pdf').write_bytes(b'%PDF-1.4 facture EDF 84,30 EUR')
        (self.docs / 'notes.txt').write_text('Code du portail : 4821', encoding='utf-8')
        (self.data / 'moteur' / 'modele.gguf').write_bytes(b'x' * 50_000)
        (self.data / 'journal' / '2026-11-25.html').write_text('<p>journal</p>')
        (self.conf / 'courrier.json').write_text('{"boite": "secret"}')
        self.agenda, self.store = self.data / 'agenda.sqlite3', self.data / 'nexus.sqlite3'
        for database in (self.agenda, self.store):
            with sqlite3.connect(database) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE lignes (texte TEXT)')
                db.execute("INSERT INTO lignes VALUES ('Dentiste jeudi 14h30')")
            db.close()
        self.backup = self.make(self.data / 'sauvegarde', run)

    def make(self, base: Path, run=None) -> Backup:
        return Backup(base, documents=self.docs, data=self.data, config=self.conf,
                      databases=[self.agenda, self.store, self.data / 'absente.sqlite3'],
                      excluded=[self.data / 'moteur'], run=run)


@unittest.skipUnless(RESTIC, 'restic absent')
class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(Path(self.tmp.name))
        self.backup = self.home.backup

    def tearDown(self):
        self.tmp.cleanup()

    def listed(self) -> list[str]:
        code, out, _err = self.backup._restic('ls', 'latest')
        self.assertEqual(code, 0)
        return out.splitlines()

    def test_setup_creates_a_private_key_and_an_encrypted_repository(self):
        self.assertFalse(self.backup.configured())
        self.assertIn('pas réglée', self.backup.describe()[0])
        key = self.backup.setup(self.home.disk)
        self.assertEqual(key, self.backup.key())
        self.assertEqual(stat.S_IMODE(os.stat(self.backup.key_path).st_mode), 0o600)
        self.assertTrue((self.home.disk / FOLDER / 'config').is_file())
        self.assertTrue(self.backup.configured())
        self.assertEqual(self.backup.setup(self.home.disk), key)             # relancer ne change ni la clé ni le dépôt
        self.assertTrue(self.backup.same_disk())                             # ici tout est sur un seul disque : NEXUS le dit
        self.assertIn('même disque', '\n'.join(self.backup.describe()))
        with self.assertRaises(ValueError):
            self.backup.setup(self.home.docs / 'Factures')                   # pas dans ce qu'elle sauvegarde
        with self.assertRaises(ValueError):
            self.backup.setup(self.home.data / 'ici')
        with self.assertRaises(ValueError):
            self.backup.setup(self.home.root / 'pas-branche')

    def test_backup_copies_what_matters_and_proves_it_restores(self):
        self.backup.setup(self.home.disk)
        result = self.backup.run(datetime(2026, 11, 25, 4, 15), random.Random(1))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['preuve'], 4)                                # 2 documents et 2 bases ressortis et comparés
        self.assertEqual(result['dates'], 1)
        listed = '\n'.join(self.listed())
        for wanted in ('Documents/Factures/edf.pdf', 'Documents/notes.txt', 'config/courrier.json',
                       'data/journal/2026-11-25.html', 'sauvegarde/instantane/agenda.sqlite3',
                       'sauvegarde/instantane/nexus.sqlite3', 'sauvegarde/instantane/plan.json'):
            self.assertIn(wanted, listed)
        for unwanted in ('modele.gguf', 'sauvegarde/cle', 'data/agenda.sqlite3', 'agenda.sqlite3-wal', 'sauvegarde/cache'):
            self.assertNotIn(unwanted, listed)                               # ni moteur, ni clé, ni base en service
        # Chiffré : ni un nom de fichier ni un contenu ne se lit sur le disque de sauvegarde.
        for path in (self.home.disk / FOLDER).rglob('*'):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(b'4821', content)
                self.assertNotIn(b'edf.pdf', content)
                self.assertNotIn(b'Dentiste', content)
        state = self.backup.state()
        self.assertEqual((state['derniere_reussie'], state['erreur'], state['fichiers'] > 5), ('2026-11-25T04:15', None, True))
        text = '\n'.join(self.backup.describe(datetime(2026, 11, 26, 9, 0)))
        self.assertIn('dernière réussie : hier', text)
        self.assertIn('restauration prouvée sur 4 fichier(s)', text)

    def test_databases_are_copied_in_a_consistent_state_while_in_use(self):
        self.backup.setup(self.home.disk)
        busy = sqlite3.connect(self.home.agenda)
        busy.execute("INSERT INTO lignes VALUES ('écriture en cours, pas validée')")       # transaction ouverte
        try:
            self.assertTrue(self.backup.run(datetime(2026, 11, 25, 4, 15))['ok'])
        finally:
            busy.rollback()
            busy.close()
        restored = self.backup.restore(self.home.root / 'retour')
        with sqlite3.connect(restored['dossier'] / 'donnees' / 'agenda.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual([r[0] for r in db.execute('SELECT texte FROM lignes')], ['Dentiste jeudi 14h30'])
        db.close()

    def test_restore_puts_everything_back_in_order_and_overwrites_nothing(self):
        self.backup.setup(self.home.disk)
        self.assertTrue(self.backup.run(datetime(2026, 11, 25, 4, 15))['ok'])
        target = self.home.root / 'retour'
        result = self.backup.restore(target)
        self.assertEqual(result['fait_le'][:10], datetime.now().date().isoformat())
        self.assertEqual(result['places'], {'donnees': str(self.home.data), 'reglages': str(self.home.conf),
                                            'Documents': str(self.home.docs)})
        self.assertEqual((target / 'Documents' / 'notes.txt').read_text(encoding='utf-8'), 'Code du portail : 4821')
        self.assertEqual((target / 'Documents' / 'Factures' / 'edf.pdf').read_bytes(), b'%PDF-1.4 facture EDF 84,30 EUR')
        self.assertEqual((target / 'reglages' / 'courrier.json').read_text(), '{"boite": "secret"}')
        self.assertTrue((target / 'donnees' / 'journal' / '2026-11-25.html').is_file())
        self.assertTrue((target / 'donnees' / 'nexus.sqlite3').is_file())                   # les bases à leur place, à leur nom
        self.assertFalse((target / 'donnees' / 'sauvegarde').exists())
        self.assertFalse((target / '.brut').exists())
        self.assertFalse((target / 'donnees' / 'moteur').exists())
        self.assertEqual((self.home.docs / 'notes.txt').read_text(encoding='utf-8'), 'Code du portail : 4821')   # originaux intacts
        with self.assertRaises(ValueError):
            self.backup.restore(target)                                      # dossier non vide : refus

    def test_an_older_date_brings_back_a_file_as_it_was(self):
        self.backup.setup(self.home.disk)
        notes = self.home.docs / 'notes.txt'
        self.assertTrue(self.backup.run(datetime(2026, 11, 20, 4, 15))['ok'])
        notes.write_text('écrasé par erreur', encoding='utf-8')
        (self.home.docs / 'Factures' / 'edf.pdf').unlink()                   # et un fichier effacé
        second = self.backup.run(datetime(2026, 11, 23, 4, 15))
        self.assertTrue(second['ok'])
        self.assertEqual(second['dates'], 2)
        self.assertEqual([s['quand'][:10] for s in self.backup.snapshots()], ['2026-11-20', '2026-11-23'])
        self.assertIn('2026-11-20, 2026-11-23', '\n'.join(self.backup.describe_dates()))
        latest = self.backup.restore(self.home.root / 'dernier')['dossier']
        self.assertEqual((latest / 'Documents' / 'notes.txt').read_text(encoding='utf-8'), 'écrasé par erreur')
        before = self.backup.restore(self.home.root / 'avant', day='2026-11-22')['dossier']       # la dernière d'avant le 22
        self.assertEqual((before / 'Documents' / 'notes.txt').read_text(encoding='utf-8'), 'Code du portail : 4821')
        self.assertTrue((before / 'Documents' / 'Factures' / 'edf.pdf').is_file())
        with self.assertRaises(ValueError):
            self.backup.restore(self.home.root / 'trop-tot', day='2026-11-01')

    def test_a_new_pc_restores_with_the_key_written_on_paper(self):
        key = self.backup.setup(self.home.disk)
        self.assertTrue(self.backup.run(datetime(2026, 11, 25, 4, 15))['ok'])
        newcomer = self.home.make(self.home.root / 'pc-neuf' / 'sauvegarde')         # ne connaît ni disque ni clé
        with self.assertRaises(ValueError):
            newcomer.restore(self.home.root / 'r0')
        with self.assertRaises(OllamaError) as refused:
            newcomer.restore(self.home.root / 'r1', source=self.home.disk, key='AAAA-BBBB-CCCC-DDDD-EEEE-FFFF')
        self.assertIn('clé', str(refused.exception))
        typed = key.lower().replace('-', ' ')                                # tapée comme on la lit, sans soin
        result = newcomer.restore(self.home.root / 'r2', source=self.home.disk, key=typed)
        self.assertEqual((result['dossier'] / 'Documents' / 'notes.txt').read_text(encoding='utf-8'), 'Code du portail : 4821')
        self.assertEqual([p.name for p in self.home.root.glob('cle-*')], [])  # la clé tapée n'est pas restée sur le disque
        # Puis il reprend les sauvegardes dans le même dépôt, avec la même clé.
        with self.assertRaises(OllamaError):
            newcomer.setup(self.home.disk)                                   # sans la clé : refus, rien n'est écrasé
        self.assertIsNone(newcomer.key() if not newcomer.key_path.exists() else None)
        with self.assertRaises(OllamaError):
            newcomer.setup(self.home.disk, key='AAAA-BBBB-CCCC-DDDD-EEEE-FFFF')
        self.assertEqual(newcomer.setup(self.home.disk, key=typed), key)
        self.assertTrue(newcomer.run(datetime(2026, 11, 26, 4, 15))['ok'])

    def test_an_empty_pc_gets_everything_back_in_its_own_folders(self):
        key = self.backup.setup(self.home.disk)
        self.assertTrue(self.backup.run(datetime(2026, 11, 25, 4, 15))['ok'])
        # Le PC neuf : autre compte, autres dossiers ; restic y a déjà posé son cache dans les données.
        new = self.home.root / 'pc-neuf'
        (new / 'data' / 'sauvegarde' / 'cache').mkdir(parents=True)
        newcomer = Backup(new / 'data' / 'sauvegarde', documents=new / 'Documents', data=new / 'data',
                          config=new / 'config', databases=[new / 'data' / 'agenda.sqlite3', new / 'data' / 'nexus.sqlite3'])
        result = newcomer.restore_in_place(source=self.home.disk, key=key)
        self.assertEqual(result['laisses'], [])
        self.assertEqual((new / 'Documents' / 'notes.txt').read_text(encoding='utf-8'), 'Code du portail : 4821')
        self.assertEqual((new / 'config' / 'courrier.json').read_text(), '{"boite": "secret"}')
        with sqlite3.connect(new / 'data' / 'agenda.sqlite3') as db:        # la base est là où ce PC la cherche
            self.assertEqual([r[0] for r in db.execute('SELECT texte FROM lignes')], ['Dentiste jeudi 14h30'])
        db.close()
        self.assertFalse((new / 'data' / 'donnees').exists())                # pas rangé un cran trop bas
        self.assertEqual([p.name for p in new.glob('nexus-restauration-*')], [])
        with self.assertRaises(ValueError) as refused:                       # une seconde fois : il y a des données, refus
            newcomer.restore_in_place(source=self.home.disk, key=key)
        self.assertIn('déjà des données', str(refused.exception))
        (new / 'data' / 'agenda.sqlite3').rename(new / 'agenda.garde')
        (new / 'data' / 'nexus.sqlite3').rename(new / 'nexus.garde')
        with self.assertRaises(ValueError) as refused:                       # des documents sont là : refus aussi
            newcomer.restore_in_place(source=self.home.disk, key=key)
        self.assertIn("n'est pas vide", str(refused.exception))

    def test_a_missing_disk_is_reported_not_ignored(self):
        self.backup.setup(self.home.disk)
        self.assertTrue(self.backup.run(datetime(2026, 11, 24, 4, 15))['ok'])
        hidden = self.home.root / 'debranche'
        self.home.disk.rename(hidden)
        result = self.backup.run(datetime(2026, 11, 25, 4, 15))
        self.assertEqual((result['ok'], result['absent']), (False, True))
        self.assertIn('absent', result['message'])
        state = self.backup.state()
        self.assertEqual((state['derniere_reussie'], state['derniere_tentative'], state['absent']),
                         ('2026-11-24T04:15', '2026-11-25T04:15', True))
        self.assertIn('absent', '\n'.join(self.backup.describe(datetime(2026, 11, 25, 9, 0))))
        hidden.rename(self.home.disk)
        self.assertTrue(self.backup.run(datetime(2026, 11, 26, 4, 15))['ok'])                # rebranché : ça repart
        self.assertIsNone(self.backup.state()['erreur'])

    def test_a_damaged_backup_is_detected(self):
        self.backup.setup(self.home.disk)
        self.assertTrue(self.backup.run(datetime(2026, 11, 24, 4, 15))['ok'])
        self.assertIsNone(self.backup.verify_all())
        for pack in (self.home.disk / FOLDER / 'data').rglob('*'):           # le disque de sauvegarde s'abîme
            if pack.is_file():
                pack.chmod(0o600)
                content = bytearray(pack.read_bytes())
                content[len(content) // 2] ^= 0xFF
                pack.write_bytes(content)
        self.assertIn('abîmé', self.backup.verify_all() or '')
        shutil.rmtree(self.backup.base / 'cache', ignore_errors=True)
        result = self.backup.run(datetime(2026, 11, 25, 4, 15), random.Random(2))
        self.assertFalse(result['ok'], result)
        self.assertIn('preuve de restauration', result['message'])

    def test_the_nightly_task_warns_once_when_it_fails(self):
        planner = Planner(':memory:')
        original_tasks_only(planner)
        runner = Runner(planner, backup=self.backup)
        task = planner.get('sauvegarde-nuit')
        self.assertEqual(runner.run(task, datetime(2026, 11, 25, 4, 15)), 'aucun disque de sauvegarde réglé')
        self.assertEqual(planner.unread(), 0)
        self.backup.setup(self.home.disk)
        self.assertIn('fichiers', runner.run(task, datetime(2026, 11, 25, 4, 15)))
        self.assertEqual(planner.unread(), 0)                                # une sauvegarde réussie ne dérange personne
        self.home.disk.rename(self.home.root / 'debranche')
        for _ in range(2):
            self.assertIn('échec', runner.run(task, datetime(2026, 11, 26, 4, 15)))
        notes = planner.notifications()
        self.assertEqual(len(notes), 1)                                      # prévenu une fois par jour, pas à chaque essai
        self.assertEqual((notes[0]['niveau'], notes[0]['titre']), ('warning', 'Sauvegarde : échec cette nuit'))
        self.assertIn('absent', notes[0]['texte'])
        planner.close()


class NoResticTest(unittest.TestCase):
    def test_restic_is_called_with_fixed_arguments_and_a_minimal_environment(self):
        calls = []

        def run(argv, env, timeout):
            calls.append((argv, env))
            return (0, '', '') if argv[1] in ('cat', 'init', 'unlock') else (1, '', 'erreur')

        with tempfile.TemporaryDirectory() as tmp:
            home = Home(Path(tmp), run)
            home.backup.setup(home.disk)
            argv, env = calls[0]
            self.assertEqual(argv, ['restic', 'init'])
            self.assertEqual(set(env), {'PATH', 'HOME', 'LANG', 'RESTIC_REPOSITORY', 'RESTIC_PASSWORD_FILE', 'RESTIC_CACHE_DIR'})
            self.assertEqual(env['RESTIC_REPOSITORY'], str(home.disk / FOLDER))
            self.assertNotIn(home.backup.key(), ' '.join(argv) + ' '.join(env.values()))      # la clé ne passe que par son fichier
            result = home.backup.run(datetime(2026, 11, 25, 4, 15))
            self.assertFalse(result['ok'])
            backup_call = next(argv for argv, _env in calls if argv[1] == 'backup')
            self.assertEqual(backup_call[2:8], ['--json', '--tag', 'nexus', '--host', 'nexus', '--time'])
            self.assertIn(str(home.data / 'moteur'), backup_call)
            self.assertEqual(backup_call[-3:], [str(home.docs), str(home.data), str(home.conf)])


if __name__ == '__main__':
    unittest.main()
