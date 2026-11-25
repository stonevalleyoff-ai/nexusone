"""La santé de NEXUS : il se surveille lui-même, et le dit quand quelque chose cloche.

Un service qui tourne seul, la nuit, peut tomber en panne sans que personne le voie : une boîte
mail qui refuse son mot de passe depuis une semaine, un disque de sauvegarde débranché, un
moteur arrêté. Ici, rien que des faits vérifiés par le code (pas de modèle) : chaque constat
dit ce qui ne va pas et quoi faire. Les constats sérieux deviennent une notification (donc une
alerte sur le téléphone), une fois par jour au plus ; tous figurent dans le journal du matin.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
from typing import Any, Callable

LEVELS = ('critical', 'warning', 'info')       # du plus grave au simple conseil
STALE_TASK_HOURS = 36                           # une tâche de chaque jour qui n'a pas tourné depuis : NEXUS était arrêté
MAIL_DAYS = 3
BACKUP_DAYS, BACKUP_DAYS_CRITICAL = 2, 7
DISK_WARNING, DISK_CRITICAL = 10.0, 3.0         # % de place libre
CERTIFICATE_WARNING_DAYS = 367                  # le certificat du serveur vit 397 jours et se refait au démarrage


@dataclass(frozen=True)
class Finding:
    key: str            # stable : sert à ne prévenir qu'une fois par jour
    level: str
    title: str
    advice: str


def _days(now: datetime, stamp: str | None) -> int | None:
    try:
        return (now - datetime.fromisoformat(stamp)).days if stamp else None
    except (TypeError, ValueError):
        return None


def _since(days: int | None) -> str:
    return 'jamais' if days is None else "aujourd'hui" if days <= 0 else 'hier' if days == 1 else f'il y a {days} jours'


class Health:
    def __init__(self, *, engine: Callable[[], str] | None = None, planner: Any = None, mailbox: Any = None,
                 backup: Any = None, folders: list[Path] | None = None, certificate: Path | None = None,
                 service: Any = None, disk_usage: Callable[[str], Any] = shutil.disk_usage, updater: Any = None,
                 system_root: Path | None = None):
        self.engine, self.planner, self.mailbox, self.backup = engine, planner, mailbox, backup
        self.updater, self.system_root = updater, system_root
        self.folders = [Path(p) for p in (folders or [])]
        self.certificate, self.service, self._disk_usage = certificate, service, disk_usage

    def check(self, now: datetime | None = None) -> list[Finding]:
        """Tous les constats, du plus grave au simple conseil. Une vérification en panne ne cache pas les autres."""
        now = now or datetime.now()
        found: list[Finding] = []
        for probe in (self._engine, self._tasks, self._mail, self._backup, self._disks, self._certificate, self._service,
                      self._updates, self._system):
            try:
                found += probe(now)
            except Exception as exc:        # la surveillance ne doit jamais tomber avec ce qu'elle surveille
                found.append(Finding(f'sonde:{probe.__name__}', 'info', 'Une vérification n\'a pas pu se faire',
                                     f'{probe.__name__.strip("_")} : {type(exc).__name__}'))
        return sorted(found, key=lambda f: LEVELS.index(f.level))

    def good(self, now: datetime | None = None) -> list[str]:
        """Ce qui va bien, en clair (pour le journal et l'appli)."""
        now = now or datetime.now()
        lines = []
        if self.backup is not None and self.backup.configured():
            state = self.backup.state()
            days = _days(now, state.get('derniere_reussie'))
            if days is not None and days <= 1 and not state.get('erreur'):
                lines.append(f"Sauvegarde faite {_since(days)}, restauration prouvée sur {state.get('preuve', 0)} fichier(s).")
        if self.mailbox is not None and self.mailbox.accounts():
            fine = [s for s in self.mailbox.status()
                    if _days(now, s['reussie']) is not None and _days(now, s['reussie']) <= 1 and not s['erreur']]
            if fine:
                lines.append('Courrier relevé : ' + ', '.join(s['boite'] for s in fine) + '.')
        return lines

    def report(self, now: datetime | None = None) -> list[str]:
        findings = self.check(now)
        marks = {'critical': 'GRAVE', 'warning': 'à régler', 'info': 'conseil'}
        lines = ['Santé de NEXUS : ' + ('tout va bien.' if not findings else
                                         f"{len(findings)} constat{'s' if len(findings) > 1 else ''}.")]
        for finding in findings:
            lines += [f'  [{marks[finding.level]}] {finding.title}', f'      {finding.advice}']
        return lines + ['  ' + line for line in self.good(now)]

    # -- les vérifications ----------------------------------------------------------------
    def _engine(self, now: datetime) -> list[Finding]:
        if self.engine is None:
            return []
        try:
            self.engine()
        except Exception as exc:            # OllamaError le plus souvent : moteur arrêté, ou aucun modèle
            return [Finding('moteur', 'critical', 'Le moteur de dialogue ne répond pas',
                            f'{str(exc)[:160] or type(exc).__name__} — NEXUS ne peut ni répondre ni classer. '
                            'Vérifier : systemctl status ollama')]
        return []

    def _tasks(self, now: datetime) -> list[Finding]:
        if self.planner is None:
            return []
        found, stale = [], []
        for task in self.planner.tasks():
            if not task['active']:
                continue
            covered = task['type'] == 'sauvegarde' and self.backup is not None      # dit par la vérification de la sauvegarde
            if str(task['resultat'] or '').startswith('échec') and not covered:
                found.append(Finding(f"tache:{task['id']}", 'warning', f"La tâche « {task['nom']} » a échoué",
                                     f"{task['resultat']} (dernier passage : {str(task['derniere'] or '?')[:16].replace('T', ' à ')})"))
            last = task['derniere']
            if task['horaire'].kind == 'quotidien' and last:
                try:
                    hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600
                except ValueError:
                    continue
                if hours > STALE_TASK_HOURS:
                    stale.append((hours, task['nom']))
        if stale:
            days = int(max(stale)[0] // 24)
            found.append(Finding('taches:arret', 'warning',
                                 f"Les tâches de chaque jour n'ont pas tourné depuis {days} jour{'s' if days > 1 else ''}",
                                 'NEXUS était arrêté ? Sur le mini-PC : nexus service etat. '
                                 f"Concernées : {', '.join(sorted(name for _h, name in stale))[:200]}."))
        return found

    def _mail(self, now: datetime) -> list[Finding]:
        if self.mailbox is None or not self.mailbox.accounts():
            return []
        found = []
        for status in self.mailbox.status():
            if not status['erreur']:
                continue
            days = _days(now, status['reussie'])
            if days is None or days >= MAIL_DAYS:
                found.append(Finding(f"courrier:{status['boite']}", 'warning',
                                     f"Courrier : la boîte « {status['boite']} » n'est plus relevée (dernière réussite : {_since(days)})",
                                     f"{status['erreur'][:160]} — mot de passe d'application changé ou révoqué ? "
                                     f"Le redonner : nexus courrier ajouter {status['boite']} …"))
        return found

    def _backup(self, now: datetime) -> list[Finding]:
        backup = self.backup
        if backup is None:
            return []
        if not backup.configured():
            return [Finding('sauvegarde:absente', 'info', 'Aucune sauvegarde : une panne de disque emporterait tout',
                            'Brancher un disque puis : nexus sauvegarde installer /chemin/du/disque')]
        found = []
        state = backup.state()
        days = _days(now, state.get('derniere_reussie'))
        if days is None or days >= BACKUP_DAYS:
            level = 'critical' if days is None or days >= BACKUP_DAYS_CRITICAL else 'warning'
            reason = ('le disque de sauvegarde est absent' if state.get('absent') else state.get('erreur') or 'aucune raison notée')
            found.append(Finding('sauvegarde:retard', level, f'Sauvegarde : dernière réussie {_since(days)}',
                                 f'{reason[:200]} — voir : nexus sauvegarde etat'))
        elif state.get('erreur'):
            found.append(Finding('sauvegarde:echec', 'warning', 'Sauvegarde : le dernier essai a échoué',
                                 f"{state['erreur'][:200]} — voir : nexus sauvegarde etat"))
        if backup.same_disk():
            found.append(Finding('sauvegarde:meme-disque', 'info', 'La sauvegarde est sur le même disque que tes documents',
                                 'Elle ne protège pas d\'une panne de ce disque : la mettre sur un disque à part.'))
        return found

    def _disks(self, now: datetime) -> list[Finding]:
        folders = list(self.folders)
        if self.backup is not None and self.backup.destination() is not None:
            folders.append(self.backup.destination())
        found, seen = [], set()
        for folder in folders:
            try:
                device = os.stat(folder).st_dev
                usage = self._disk_usage(str(folder))
            except OSError:
                continue                    # dossier absent : d'autres vérifications en parlent
            if device in seen or not usage.total:
                continue
            seen.add(device)
            free = 100.0 * usage.free / usage.total
            if free < DISK_WARNING:
                found.append(Finding(f'disque:{device}', 'critical' if free < DISK_CRITICAL else 'warning',
                                     f'Disque presque plein : {free:.0f} % libre ({folder})',
                                     'Faire de la place : quand le disque est plein, NEXUS ne peut plus rien enregistrer.'))
        return found

    def _certificate(self, now: datetime) -> list[Finding]:
        if self.certificate is None or not self.certificate.is_file():
            return []
        age = (now.timestamp() - self.certificate.stat().st_mtime) / 86400
        if age < CERTIFICATE_WARNING_DAYS:
            return []
        return [Finding('certificat', 'warning', 'Le certificat du serveur arrive à son terme',
                        'Redémarrer NEXUS suffit, il le refait : sudo systemctl restart nexus')]

    def _updates(self, now: datetime) -> list[Finding]:
        """Ce que la veille de nuit a noté sur les versions de NEXUS (aucun appel au réseau ici)."""
        if self.updater is None:
            return []
        state = self.updater.state()
        found = []
        if state.get('deplacees'):
            found.append(Finding('versions:deplacees', 'critical', 'Une version publiée de NEXUS a changé de contenu',
                                 f"Étiquettes concernées : {', '.join(state['deplacees'])[:120]}. Une version ne doit jamais "
                                 'changer après coup : n\'installe rien et vérifie qui a accès à ton dépôt.'))
        if state.get('disponible'):
            found.append(Finding(f"versions:{state['disponible']}", 'info', f"NEXUS {state['disponible']} est disponible",
                                 'Sur le PC : nexus mise-a-jour installer (tests, sauvegarde et retour arrière compris).'))
        return found

    def _system(self, now: datetime) -> list[Finding]:
        """Le système lui-même : correctifs de sécurité automatiques, redémarrage en attente."""
        if self.system_root is None:
            return []
        found = []
        apt = self.system_root / 'etc' / 'apt' / 'apt.conf.d'
        if apt.is_dir():        # Debian, Ubuntu : les correctifs de sécurité doivent s'installer seuls
            enabled = False
            for conf in sorted(apt.glob('*')):
                try:
                    text = conf.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                for line in text.splitlines():
                    if 'Unattended-Upgrade' in line and 'APT::Periodic' in line and not line.lstrip().startswith(('//', '#')):
                        enabled = '"1"' in line or "'1'" in line
            if not enabled:
                found.append(Finding('systeme:correctifs', 'info', 'Les correctifs de sécurité du système ne s\'installent pas seuls',
                                     'Une fois pour toutes : sudo apt install unattended-upgrades && sudo dpkg-reconfigure -plow unattended-upgrades'))
        if (self.system_root / 'var' / 'run' / 'reboot-required').exists():
            found.append(Finding('systeme:redemarrage', 'info', 'Le système attend un redémarrage pour finir ses mises à jour',
                                 'Quand ça t\'arrange : sudo reboot (NEXUS repart tout seul).'))
        return found

    def _service(self, now: datetime) -> list[Finding]:
        if self.service is None:
            return []
        state = self.service.state()
        if not state['systemd']:
            return []
        if not state['installe']:
            return [Finding('service:absent', 'info', 'NEXUS ne démarre pas avec le PC',
                            'Il s\'arrête avec ce terminal. Pour qu\'il tourne en permanence : nexus service installer')]
        if state['a_jour'] is False:
            return [Finding('service:ancien', 'warning', 'Le service installé n\'est plus à jour',
                            'Relancer les commandes affichées par : nexus service installer')]
        return []
