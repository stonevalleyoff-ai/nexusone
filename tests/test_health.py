"""La santé de NEXUS : ce qu'il constate sur lui-même, et quand il prévient."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from nexus.health import Finding, Health
from nexus.journal import Edition, essentials, gather, render
from nexus.ollama import OllamaError
from nexus.planner import Planner, Runner
from tests.test_planner import original_tasks_only

NOW = datetime(2026, 11, 25, 9, 0)


def ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat(timespec='minutes')


class FakeBackup:
    def __init__(self, destination=Path('/disque'), same=False, **state):
        self._destination, self._same, self._state = destination, same, state

    def configured(self):
        return self._destination is not None

    def destination(self):
        return self._destination

    def state(self):
        return self._state

    def same_disk(self):
        return self._same


class FakeMailbox:
    def __init__(self, *statuses):
        self._statuses = list(statuses)

    def accounts(self):
        return [{'nom': s['boite']} for s in self._statuses]

    def status(self):
        return self._statuses


def service(**state):
    full = {'systemd': True, 'installe': True, 'a_jour': True, 'actif': 'active', 'au_demarrage': 'enabled', **state}
    return SimpleNamespace(state=lambda: full)


def keys(health: Health) -> list[tuple[str, str]]:
    return [(f.key, f.level) for f in health.check(NOW)]


class HealthTest(unittest.TestCase):
    def test_nothing_to_say_when_everything_runs(self):
        planner = Planner(':memory:')
        health = Health(engine=lambda: 'qwen3.5:2b', planner=planner, service=service(),
                        mailbox=FakeMailbox({'boite': 'gmail', 'essai': ago(hours=6), 'reussie': ago(hours=6), 'erreur': None}),
                        backup=FakeBackup(derniere_reussie=ago(hours=5), erreur=None, preuve=6))
        self.assertEqual(health.check(NOW), [])
        self.assertEqual(health.report(NOW)[0], 'Santé de NEXUS : tout va bien.')
        self.assertEqual(health.good(NOW), ["Sauvegarde faite aujourd'hui, restauration prouvée sur 6 fichier(s).",
                                            'Courrier relevé : gmail.'])
        planner.close()

    def test_a_stopped_engine_comes_first(self):
        def down():
            raise OllamaError('Ollama ne répond pas sur http://127.0.0.1:11434')

        health = Health(engine=down, backup=FakeBackup(destination=None))
        self.assertEqual(keys(health), [('moteur', 'critical'), ('sauvegarde:absente', 'info')])
        self.assertIn('Ollama ne répond pas', health.check(NOW)[0].advice)

    def test_failed_and_stale_tasks(self):
        planner = Planner(':memory:')
        original_tasks_only(planner)
        health = Health(planner=planner)
        self.assertEqual(health.check(NOW), [])                              # jamais tournées : rien à reprocher
        planner.set_active('courrier-nuit', True)
        planner.db.execute('UPDATE taches SET derniere=?, resultat=? WHERE id=?', (ago(hours=6), 'échec : TimeoutError', 'courrier-nuit'))
        found = health.check(NOW)
        self.assertEqual([(f.key, f.level) for f in found], [('tache:courrier-nuit', 'warning')])
        self.assertIn('TimeoutError', found[0].advice)
        planner.db.execute('UPDATE taches SET derniere=?, resultat=? WHERE id=?', (ago(days=4), 'ok', 'courrier-nuit'))
        planner.db.execute('UPDATE taches SET derniere=? WHERE id=?', (ago(days=2), 'veille-tempo'))
        found = health.check(NOW)
        self.assertEqual([f.key for f in found], ['taches:arret'])
        self.assertIn('depuis 4 jours', found[0].title)
        self.assertIn('nexus service etat', found[0].advice)
        # Un échec de la sauvegarde n'est dit qu'une fois, par la vérification de la sauvegarde.
        planner.set_active('sauvegarde-nuit', True)
        planner.db.execute('UPDATE taches SET derniere=?, resultat=? WHERE id=?', (ago(hours=5), 'échec : disque absent', 'sauvegarde-nuit'))
        both = Health(planner=planner, backup=FakeBackup(derniere_reussie=ago(hours=29), erreur='disque absent')).check(NOW)
        self.assertEqual([f.key for f in both], ['taches:arret', 'sauvegarde:echec'])
        planner.set_active('sauvegarde-nuit', False)
        planner.set_active('courrier-nuit', False)                           # une tâche coupée exprès n'est pas en retard
        planner.set_active('veille-tempo', False)
        self.assertEqual(health.check(NOW), [])
        planner.close()

    def test_a_mailbox_silent_for_days_is_reported_but_not_one_bad_night(self):
        bad_night = {'boite': 'gmail', 'essai': ago(hours=6), 'reussie': ago(days=1, hours=6), 'erreur': 'délai dépassé'}
        self.assertEqual(Health(mailbox=FakeMailbox(bad_night)).check(NOW), [])
        silent = {'boite': 'laposte', 'essai': ago(hours=6), 'reussie': ago(days=5), 'erreur': 'identifiants refusés'}
        never = {'boite': 'gmail', 'essai': ago(hours=6), 'reussie': None, 'erreur': 'identifiants refusés'}
        found = Health(mailbox=FakeMailbox(silent, never)).check(NOW)
        self.assertEqual([f.key for f in found], ['courrier:laposte', 'courrier:gmail'])
        self.assertIn('il y a 5 jours', found[0].title)
        self.assertIn('jamais', found[1].title)
        self.assertIn('identifiants refusés', found[0].advice)

    def test_backup_late_missing_or_on_the_same_disk(self):
        self.assertEqual(keys(Health(backup=FakeBackup(derniere_reussie=ago(days=1)))), [])
        self.assertEqual(keys(Health(backup=FakeBackup(derniere_reussie=ago(days=3), erreur='x', absent=True))),
                         [('sauvegarde:retard', 'warning')])
        late = Health(backup=FakeBackup(derniere_reussie=ago(days=9), absent=True)).check(NOW)[0]
        self.assertEqual((late.level, late.title), ('critical', 'Sauvegarde : dernière réussie il y a 9 jours'))
        self.assertIn('disque de sauvegarde est absent', late.advice)
        self.assertEqual(keys(Health(backup=FakeBackup())), [('sauvegarde:retard', 'critical')])        # jamais réussie
        self.assertEqual(keys(Health(backup=FakeBackup(derniere_reussie=ago(hours=30), erreur='preuve ratée'))),
                         [('sauvegarde:echec', 'warning')])
        self.assertEqual(keys(Health(backup=FakeBackup(same=True, derniere_reussie=ago(hours=5)))),
                         [('sauvegarde:meme-disque', 'info')])

    def test_full_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage = {'free': 50}
            health = Health(folders=[Path(tmp), Path(tmp) / 'absent', Path(tmp)],
                            disk_usage=lambda path: SimpleNamespace(total=1000, free=usage['free']))
            self.assertEqual(health.check(NOW)[0].level, 'warning')
            self.assertEqual(len(health.check(NOW)), 1)                      # même disque : dit une seule fois
            self.assertIn('5 % libre', health.check(NOW)[0].title)
            usage['free'] = 20
            self.assertEqual(health.check(NOW)[0].level, 'critical')
            usage['free'] = 400
            self.assertEqual(health.check(NOW), [])

    def test_certificate_and_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / 'certificat.pem'
            self.assertEqual(Health(certificate=cert).check(NOW), [])        # pas de serveur chiffré : rien à dire
            cert.write_text('x')
            os.utime(cert, (NOW.timestamp() - 100 * 86400,) * 2)
            self.assertEqual(Health(certificate=cert).check(NOW), [])
            os.utime(cert, (NOW.timestamp() - 380 * 86400,) * 2)
            self.assertEqual(keys(Health(certificate=cert)), [('certificat', 'warning')])
        self.assertEqual(Health(service=service()).check(NOW), [])
        self.assertEqual(Health(service=service(systemd=False, installe=False)).check(NOW), [])       # pas de systemd : hors sujet
        self.assertEqual(keys(Health(service=service(installe=False))), [('service:absent', 'info')])
        self.assertEqual(keys(Health(service=service(a_jour=False))), [('service:ancien', 'warning')])

    def test_new_versions_and_rewritten_ones(self):
        updater = SimpleNamespace(state=lambda: {'disponible': '0.29.0', 'deplacees': []})
        found = Health(updater=updater).check(NOW)
        self.assertEqual([(f.key, f.level, f.title) for f in found], [('versions:0.29.0', 'info', 'NEXUS 0.29.0 est disponible')])
        self.assertIn('nexus mise-a-jour installer', found[0].advice)
        updater = SimpleNamespace(state=lambda: {'disponible': None, 'deplacees': ['v0.28.1']})
        found = Health(updater=updater).check(NOW)
        self.assertEqual([(f.key, f.level) for f in found], [('versions:deplacees', 'critical')])
        self.assertIn('v0.28.1', found[0].advice)
        self.assertEqual(Health(updater=SimpleNamespace(state=lambda: {})).check(NOW), [])

    def test_the_system_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(Health(system_root=root).check(NOW), [])        # pas un système à apt : rien à dire
            conf = root / 'etc' / 'apt' / 'apt.conf.d'
            conf.mkdir(parents=True)
            self.assertEqual(keys(Health(system_root=root)), [('systeme:correctifs', 'info')])
            (conf / '20auto-upgrades').write_text('APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "0";\n')
            self.assertEqual(keys(Health(system_root=root)), [('systeme:correctifs', 'info')])
            (conf / '20auto-upgrades').write_text('// APT::Periodic::Unattended-Upgrade "1";\n')
            self.assertEqual(keys(Health(system_root=root)), [('systeme:correctifs', 'info')])      # en commentaire : pas actif
            (conf / '20auto-upgrades').write_text('APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n')
            self.assertEqual(Health(system_root=root).check(NOW), [])
            (root / 'var' / 'run').mkdir(parents=True)
            (root / 'var' / 'run' / 'reboot-required').write_text('')
            found = Health(system_root=root).check(NOW)
            self.assertEqual([(f.key, f.level) for f in found], [('systeme:redemarrage', 'info')])
            self.assertIn('sudo reboot', found[0].advice)

    def test_a_broken_check_does_not_hide_the_others(self):
        broken = SimpleNamespace(tasks=lambda: 1 / 0)
        found = Health(planner=broken, backup=FakeBackup(derniere_reussie=ago(days=9))).check(NOW)
        self.assertEqual([(f.key, f.level) for f in found], [('sauvegarde:retard', 'critical'), ('sonde:_tasks', 'info')])


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.planner = Planner(':memory:')
        for task in self.planner.tasks():
            self.planner.set_active(task['id'], False)
        self.findings: list[Finding] = []
        self.runner = Runner(self.planner, health=SimpleNamespace(check=lambda now: list(self.findings)))

    def tearDown(self):
        self.planner.close()

    def test_only_lasting_serious_problems_reach_the_phone_once_a_day(self):
        engine = Finding('moteur', 'critical', 'Le moteur de dialogue ne répond pas', 'systemctl status ollama')
        advice = Finding('service:absent', 'info', 'NEXUS ne démarre pas avec le PC', 'nexus service installer')
        self.findings = [engine, advice]
        self.runner.tick(NOW)
        self.assertEqual(self.planner.notifications(), [])                   # vu une fois : peut-être passager
        self.runner.tick(NOW + timedelta(minutes=2))
        self.assertEqual(self.planner.notifications(), [])                   # pas encore l'heure de revérifier
        self.runner.tick(NOW + timedelta(minutes=6))
        notes = self.planner.notifications()
        self.assertEqual([(n['niveau'], n['titre'], n['tache']) for n in notes],
                         [('critical', 'NEXUS : Le moteur de dialogue ne répond pas', 'sante')])       # le conseil ne sonne pas
        for minutes in (12, 18, 24):
            self.runner.tick(NOW + timedelta(minutes=minutes))
        self.assertEqual(len(self.planner.notifications()), 1)               # une fois par jour, pas toutes les cinq minutes
        self.runner.tick(NOW + timedelta(days=1))
        self.assertEqual(len(self.planner.notifications()), 2)               # toujours là le lendemain : on le redit

    def test_a_passing_problem_disturbs_nobody(self):
        self.findings = [Finding('moteur', 'critical', 'Le moteur de dialogue ne répond pas', '…')]
        self.runner.tick(NOW)
        self.findings = []                                                   # le moteur a redémarré entre-temps
        self.runner.tick(NOW + timedelta(minutes=6))
        self.findings = [Finding('moteur', 'critical', 'Le moteur de dialogue ne répond pas', '…')]
        self.runner.tick(NOW + timedelta(minutes=12))
        self.assertEqual(self.planner.notifications(), [])

    def test_a_crashing_health_check_never_stops_the_tasks(self):
        self.runner.health = SimpleNamespace(check=lambda now: 1 / 0)
        self.planner.set_active('echeances', True, now=NOW - timedelta(days=8))
        self.assertEqual(self.runner.tick(NOW), ['echeances'])


class JournalTest(unittest.TestCase):
    def test_the_morning_paper_says_what_is_wrong_with_nexus(self):
        health = Health(backup=FakeBackup(derniere_reussie=ago(days=9), absent=True), service=service(installe=False))
        edition = gather(NOW, health=health)
        self.assertEqual([h['niveau'] for h in edition.health], ['critical', 'info'])
        self.assertIn('À régler sur NEXUS : Sauvegarde : dernière réussie il y a 9 jours.', essentials(edition))
        page = render(edition)
        self.assertIn('Santé de NEXUS', page)
        self.assertIn('<span class="alerte">Sauvegarde : dernière réussie il y a 9 jours</span>', page)
        self.assertIn('NEXUS ne démarre pas avec le PC', page)

    def test_a_healthy_nexus_takes_one_quiet_line(self):
        health = Health(backup=FakeBackup(derniere_reussie=ago(hours=5), erreur=None, preuve=6))
        edition = gather(NOW, health=health)
        self.assertEqual(edition.health, [])
        self.assertNotIn('À régler', essentials(edition))
        self.assertIn('restauration prouvée sur 6 fichier(s)', render(edition))
        self.assertNotIn('Santé de NEXUS', render(Edition(NOW.date(), 1, NOW)))      # rien à dire : pas de rubrique

    def test_a_failing_health_check_does_not_stop_the_presses(self):
        edition = gather(NOW, health=SimpleNamespace(check=lambda now: 1 / 0, good=lambda now: []))
        self.assertIn('Santé de NEXUS non relevée (ZeroDivisionError).', edition.notes)


if __name__ == '__main__':
    unittest.main()
