"""NEXUS en service du PC de la maison : il démarre avec la machine et se relance s'il tombe.

Sans service, rappels, relève du courrier, journal du matin et alertes s'arrêtent dès que le
terminal de `nexus serveur` se ferme. Ici, NEXUS prépare un service systemd et, fidèle à sa
règle, ne prend aucun droit d'administrateur : il écrit le fichier chez toi et affiche les
commandes `sudo` à lancer une fois.

Le service tourne sous ton compte, jamais en root, et systemd l'enferme : de ton dossier
personnel il ne voit que ses données, ses réglages et tes documents (pas tes clés, pas ton
navigateur, pas le reste) ; le système lui est en lecture seule, il ne voit pas les
périphériques, ne peut acquérir aucun privilège ni charger de module. Ce n'est plus seulement
NEXUS qui se retient : c'est le système qui le tient.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
from typing import Callable

from .ollama import OllamaError

UNIT = 'nexus.service'
SYSTEM_UNIT = Path('/etc/systemd/system') / UNIT
TOOLS_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
MODES = {'vpn': '--vpn', 'reseau': '--reseau', 'local': ''}
# Réglages de NEXUS (variables d'environnement) repris dans le service s'ils sont posés à l'installation.
KEPT_SETTINGS = ('NEXUS_SOURCE', 'NEXUS_BIBLIOTHEQUE', 'NEXUS_MODEL', 'NEXUS_BACKEND', 'NEXUS_MOTEUR', 'NEXUS_MOTEUR_VEILLE',
                 'NEXUS_COLIBRI_DIR', 'NEXUS_COLIBRI_MODELE', 'NEXUS_OLLAMA_URL', 'NEXUS_EMBED_MODEL', 'NEXUS_NUM_CTX',
                 'NEXUS_MEMOIRE', 'NEXUS_TOOLS', 'NEXUS_RULES', 'NEXUS_NAVIGATEUR', 'NEXUS_SERVEUR_NOMS',
                 'XDG_DATA_HOME', 'XDG_CONFIG_HOME')
# L'enfermement, ligne par ligne : ce que systemd interdit au service, et pourquoi.
HARDENING = (
    ('NoNewPrivileges=yes', "ne peut jamais gagner de droits (sudo, programmes « setuid »)"),
    ('ProtectSystem=strict', 'tout le système est en lecture seule'),
    ('ProtectHome=tmpfs', 'les dossiers personnels lui sont cachés, hors exceptions ci-dessous'),
    ('PrivateTmp=yes', 'fichiers temporaires à lui seul'),
    ('PrivateDevices=yes', 'ne voit ni disques ni périphériques'),
    ('ProtectKernelTunables=yes', 'ne peut pas régler le noyau'),
    ('ProtectKernelModules=yes', 'ne peut pas charger de module'),
    ('ProtectKernelLogs=yes', 'ne lit pas le journal du noyau'),
    ('ProtectControlGroups=yes', 'ne touche pas aux groupes de contrôle'),
    ('ProtectClock=yes', "ne peut pas changer l'heure"),
    ('ProtectHostname=yes', 'ne peut pas renommer la machine'),
    ('RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK', 'réseau ordinaire seulement'),
    ('RestrictNamespaces=yes', "ne peut pas se fabriquer d'espaces isolés"),
    ('RestrictRealtime=yes', 'pas de priorité temps réel'),
    ('RestrictSUIDSGID=yes', 'ne peut pas créer de programme « setuid »'),
    ('LockPersonality=yes', "ne change pas de mode d'exécution"),
    ('CapabilityBoundingSet=', 'aucune capacité de root, même par accident'),
    ('AmbientCapabilities=', ''),
    ('SystemCallArchitectures=native', 'appels système de cette machine seulement'),
    ('SystemCallFilter=@system-service', "appels système d'un service ordinaire"),
    ('SystemCallFilter=~@chown @clock @module @raw-io @reboot @swap', "pas ceux d'administration"),
    # Toute la famille « privilégiée » de systemd sauf capset : le navigateur qui fabrique le PDF du journal
    # s'en sert pour abandonner ses droits (mesuré : sans lui, il s'arrête). Il n'y a rien à y gagner,
    # la liste des capacités permises étant vide.
    ('SystemCallFilter=~_sysctl acct bpf chroot fanotify_init fanotify_mark nfsservctl open_by_handle_at pivot_root '
     'quotactl quotactl_fd setdomainname setfsuid setgroups sethostname setresuid setreuid setuid vhangup',
     "ni changer d'identité, ni changer de racine"),
    ('SystemCallErrorNumber=EPERM', 'un appel interdit est refusé, sans tuer le service'),
    ('UMask=0077', 'ce qu\'il écrit n\'est lisible que par toi'),
)
# Programmes dont NEXUS se sert : (commande, paquet apt, à quoi ça sert, indispensable ?).
PROGRAMS = (
    ('ollama', None, 'le moteur des petits modèles', True),
    ('pdftotext', 'poppler-utils', 'lire les PDF', True),
    ('pdftoppm', 'poppler-utils', 'lire les PDF scannés', False),
    ('tesseract', 'tesseract-ocr tesseract-ocr-fra', 'lire les scans et les photos', False),
    ('openssl', 'openssl', 'chiffrer les échanges avec le téléphone', True),
    ('wg', 'wireguard-tools', 'le tunnel privé', False),
    ('qrencode', 'qrencode', 'le code QR du tunnel', False),
    ('ffmpeg', 'ffmpeg', 'la voix', False),
    ('chromium', 'chromium', 'le PDF du journal', False),
    ('lp', 'cups-client', 'imprimer le journal', False),
    ('git', 'git', 'installer le gros moteur et la voix', False),
    ('restic', 'restic', 'la sauvegarde chiffrée', False),
    ('cmake', 'cmake build-essential', 'installer le gros moteur et la voix', False),
)

Run = Callable[[list[str]], tuple[int, str]]


def _exec(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20, check=False)
        return done.returncode, (done.stdout + done.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ''


def quoted(value: str) -> str:
    """Une valeur pour un fichier de service. Refuse ce que systemd pourrait interpréter."""
    if not value or any(ord(ch) < 32 or ch in '"\\$%\'' for ch in value):
        raise ValueError(f'chemin ou réglage refusé pour le service (caractère spécial) : {value!r}')
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def checked(value: str) -> str:
    """La valeur telle quelle (pour les réglages qui prennent toute la ligne), une fois vérifiée."""
    quoted(value)
    return value


def personal(path: Path) -> bool:
    """Vrai si le chemin est dans un dossier que ProtectHome cache (/home, /root, /run/user)."""
    return any(Path(path).is_relative_to(home) for home in ('/home', '/root', '/run/user'))


def bindable(path: Path) -> str:
    if ':' in str(path):
        raise ValueError(f'chemin refusé pour le service (deux-points) : {str(path)!r}')
    return quoted(str(path))


@dataclass
class Plan:
    """Tout ce qui décrit le service : qui, où, comment."""
    user: str
    python: str
    code: Path                  # dossier qui contient le paquet « nexus »
    mode: str                   # vpn | reseau | local
    port: int
    writable: list[Path]        # les seuls dossiers où le service peut écrire
    settings: dict[str, str] = field(default_factory=dict)
    readable: list[Path] = field(default_factory=list)      # ce qu'il doit pouvoir lire dans un dossier personnel

    def text(self) -> str:
        if self.mode not in MODES:
            raise ValueError('mode de service inconnu')
        if not 1024 <= self.port <= 65535:
            raise ValueError('port hors limites (1024 à 65535)')
        after = ['network-online.target', 'ollama.service']
        wants = ['network-online.target']
        if self.mode == 'vpn':       # le serveur n'écoute que dans le tunnel : il lui faut le tunnel
            after.append('wg-quick@nexus.service')
            wants.append('wg-quick@nexus.service')
        command = ' '.join(filter(None, [quoted(self.python), '-m', 'nexus', 'serveur', MODES[self.mode],
                                         '--port', str(self.port)]))
        environment = {'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'NEXUS_SERVICE': '1', **self.settings}
        lines = [
            '# Service de NEXUS. Fichier écrit par `nexus service installer` : ne pas le modifier à la main,',
            '# relancer la commande à la place.',
            '[Unit]',
            "Description=NEXUS, l'assistant de la maison",
            'After=' + ' '.join(after),
            'Wants=' + ' '.join(wants),
            'StartLimitIntervalSec=0',
            '',
            '[Service]',
            'Type=simple',
            f'User={quoted(self.user)}',
            f'WorkingDirectory={checked(str(self.code))}',
            *[f'Environment="{name}={value}"' for name, value in environment.items() if quoted(value)],
            f'ExecStart={command}',
            '# Il se relance toujours, et ne renonce jamais : un tunnel pas encore monté, un moteur absent,',
            '# une panne passagère ne doivent pas laisser la maison sans NEXUS.',
            'Restart=always',
            'RestartSec=10',
            'KillSignal=SIGINT',
            'TimeoutStopSec=45',
            'OOMPolicy=continue',
            '',
            "# -- l'enfermement ----------------------------------------------------------------",
        ]
        for directive, reason in HARDENING:      # le commentaire au-dessus : systemd n'en accepte pas en fin de ligne
            lines += ([f'# {reason}'] if reason else []) + [directive]
        lines.append('# Les seuls endroits où il peut écrire (le « - » : un dossier absent n\'empêche pas de démarrer).')
        lines.append('# Ceux qui sont dans un dossier personnel y sont rendus visibles, eux seuls :')
        for path in self.writable:
            lines.append(f'ReadWritePaths=-{bindable(path)}')
            if personal(path):
                lines.append(f'BindPaths=-{bindable(path)}')
        if any(personal(path) for path in self.readable):
            lines.append('# Visible en lecture seule (le programme lui-même) :')
            lines += [f'BindReadOnlyPaths={bindable(path)}' for path in self.readable if personal(path)]
        lines += ['', '[Install]', 'WantedBy=multi-user.target', '']
        return '\n'.join(lines)


class Service:
    def __init__(self, settings, run: Run | None = None, *, environ: dict[str, str] | None = None,
                 home: Path | None = None, system_unit: Path = SYSTEM_UNIT):
        self.settings, self._run = settings, run or _exec
        self.environ = os.environ if environ is None else environ
        self.home = Path(home) if home is not None else Path.home()
        self.system_unit = system_unit
        self.prepared = settings.db_path.parent / 'service' / UNIT
        self.launcher = self.home / '.local' / 'bin' / 'nexus'

    # -- le plan --------------------------------------------------------------------------
    def plan(self, mode: str, port: int) -> Plan:
        if os.geteuid() == 0:
            raise OllamaError('NEXUS ne s\'installe pas en root : relancer la commande sous ton compte, sans sudo.')
        settings = self.settings
        data = settings.db_path.parent
        conf = settings.autonomy_levels.parent if settings.autonomy_levels is not None else None
        writable = [data] + [p for p in (conf, settings.library_root, settings.engine_dir) if p is not None]
        destination = settings.open_backup().destination()
        if destination is not None:          # le disque de sauvegarde : le service doit pouvoir y écrire
            writable.append(destination)
        unique: list[Path] = []
        for path in writable:
            path = Path(path).expanduser().absolute()
            if not any(path == kept or kept in path.parents for kept in unique):      # déjà couvert par un dossier parent
                unique.append(path)
        python = sys.executable if sys.executable and os.path.isabs(sys.executable) else '/usr/bin/python3'
        kept = {name: self.environ[name] for name in KEPT_SETTINGS if self.environ.get(name)}
        updater = settings.open_updater()
        if updater.current.is_symlink():      # versions officielles : le service suit le lien « actuelle », que
            code, programme = updater.current, updater.app      # la mise à jour fait basculer ; tout le dossier lui est lisible
        else:
            code = programme = Path(__file__).resolve().parent.parent
        # Ce que le service doit lire pour tourner : son code, et Python s'il n'est pas celui du système.
        readable = [programme] + [Path(p) for p in {sys.prefix, sys.base_prefix} if personal(Path(p))]
        if kept.get('NEXUS_NAVIGATEUR'):
            readable.append(Path(kept['NEXUS_NAVIGATEUR']).parent)
        readable = [p for p in readable if not any(p == w or w in p.parents for w in unique)]
        return Plan(pwd.getpwuid(os.getuid()).pw_name, python, code, mode, port, unique, kept, readable)

    def prepare(self, mode: str, port: int) -> Plan:
        """Écrit le fichier du service chez l'utilisateur, crée les dossiers et la commande `nexus`."""
        plan = self.plan(mode, port)
        text = plan.text()
        for path in plan.writable:
            path.mkdir(parents=True, exist_ok=True)
        self.prepared.parent.mkdir(parents=True, exist_ok=True)
        self.prepared.write_text(text, encoding='utf-8')
        self.write_launcher(plan.code, plan.python)
        return plan

    def write_launcher(self, code: Path, python: str) -> None:
        """Pose la commande « nexus » dans ~/.local/bin : elle lance le code de ce dossier."""
        self.launcher.parent.mkdir(parents=True, exist_ok=True)
        self.launcher.write_text('#!/bin/sh\n# La commande « nexus » (écrite par NEXUS).\n'
                                 f'cd {_shell(str(code))} && exec {_shell(python)} -m nexus "$@"\n', encoding='utf-8')
        self.launcher.chmod(0o755)

    def install_commands(self) -> list[str]:
        return [f'sudo install -m 644 {_shell(str(self.prepared))} {self.system_unit}',
                'sudo systemctl daemon-reload',
                f'sudo systemctl enable {UNIT}',
                f'sudo systemctl restart {UNIT}']      # « restart » démarre aussi : la même suite sert après une mise à jour

    def remove_commands(self) -> list[str]:
        return [f'sudo systemctl disable --now {UNIT}', f'sudo rm {self.system_unit}', 'sudo systemctl daemon-reload']

    # -- l'état ---------------------------------------------------------------------------
    def state(self) -> dict[str, object]:
        installed = self.system_unit.is_file()
        current = None
        if installed and self.prepared.is_file():
            try:
                current = self.system_unit.read_text(encoding='utf-8') == self.prepared.read_text(encoding='utf-8')
            except OSError:
                current = None
        active = self._run(['systemctl', 'is-active', UNIT])[1].splitlines()[-1:] if installed else []
        enabled = self._run(['systemctl', 'is-enabled', UNIT])[1].splitlines()[-1:] if installed else []
        return {'systemd': Path('/run/systemd/system').is_dir(), 'installe': installed, 'a_jour': current,
                'actif': (active or ['inconnu'])[0], 'au_demarrage': (enabled or ['inconnu'])[0]}

    def journal(self, lines: int = 40) -> str:
        code, out = self._run(['journalctl', '-u', UNIT, '-n', str(lines), '--no-pager', '-o', 'short'])
        if code != 0 or not out:
            return ('Journal illisible depuis ce compte. Essayer : sudo journalctl -u nexus -n 40\n'
                    '(ou entrer dans le groupe qui a le droit de le lire : sudo usermod -aG systemd-journal $USER, '
                    'puis fermer et rouvrir la session)')
        return out

    def missing(self) -> list[tuple[str, str | None, str, bool]]:
        """Les programmes absents : (commande, paquet, utilité, indispensable)."""
        found = []
        for command, package, purpose, required in PROGRAMS:
            names = (command, 'chromium-browser', 'google-chrome') if command == 'chromium' else (command,)
            if not any(shutil.which(name, path=TOOLS_PATH) for name in names):
                found.append((command, package, purpose, required))
        return found

    def describe(self) -> list[str]:
        state = self.state()
        if not state['systemd']:
            lines = ['Service : cette machine ne démarre pas avec systemd, le service ne peut pas y être installé.']
        elif not state['installe']:
            lines = ['Service : pas installé (nexus service installer). NEXUS ne tourne que si tu le lances.']
        else:
            words = {'active': 'en marche', 'inactive': 'arrêté', 'failed': 'en échec', 'activating': 'en cours de démarrage'}
            boot = {'enabled': 'démarre avec le PC', 'disabled': 'ne démarre pas avec le PC'}
            lines = [f"Service : installé, {words.get(str(state['actif']), state['actif'])}, "
                     f"{boot.get(str(state['au_demarrage']), state['au_demarrage'])}."]
            if state['a_jour'] is False:
                lines.append('  Le fichier installé n\'est plus celui que NEXUS a préparé : relancer les commandes de '
                             '`nexus service installer`.')
        missing = self.missing()
        packages = sorted({p for _c, package, _u, _r in missing if package for p in package.split()})
        for command, package, purpose, required in missing:
            lines.append(f"  il manque « {command} » ({purpose}){' : indispensable' if required else ''}"
                         + ('' if package else ' : voir https://ollama.com/download'))
        if packages:
            lines.append('  pour les installer : sudo apt install ' + ' '.join(packages))
        return lines


def _shell(value: str) -> str:
    """Un argument pour une ligne de commande affichée à l'utilisateur ou écrite dans le lanceur."""
    return "'" + value.replace("'", "'\\''") + "'" if not value.replace('/', '').replace('.', '').replace('-', '').replace('_', '').isalnum() else value
