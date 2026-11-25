"""Outils en LECTURE SEULE que le modèle peut demander pour examiner la machine.

Règles, vérifiées par les tests :
- aucun outil ne modifie quoi que ce soit (pas d'écriture, pas de réseau sortant) ;
- les rares commandes externes ont des arguments figés ; le modèle ne fournit que
  des valeurs validées (énumérations, entiers bornés, chemin confiné au dossier
  personnel), jamais une ligne de commande, et aucun interpréteur shell n'est utilisé ;
- les fichiers et dossiers cachés du dossier personnel (~/.ssh, ~/.config…) sont exclus ;
- chaque résultat est borné en taille, et présenté au modèle comme une donnée.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable

MAX_RESULT_CHARS = 2400
COMMAND_TIMEOUT = 6
SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
TEXT_SUFFIXES = {'.txt', '.md', '.csv', '.log', '.json', '.py', '.sh', '.html', '.xml',
                 '.ini', '.conf', '.yaml', '.yml', '.srt', '.tex'}
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png'}       # lisibles une fois indexés (reconnaissance de texte)
DOC_SUFFIXES = TEXT_SUFFIXES | {'.pdf'} | IMAGE_SUFFIXES
OCR_NOTE = 'texte reconnu automatiquement sur un scan ou une photo : vérifier les chiffres importants'
PSEUDO_FS = {'proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'securityfs', 'cgroup', 'cgroup2',
             'pstore', 'efivarfs', 'bpf', 'debugfs', 'tracefs', 'configfs', 'fusectl', 'mqueue',
             'hugetlbfs', 'autofs', 'binfmt_misc', 'rpc_pipefs', 'nsfs', 'ramfs', 'squashfs',
             'fuse.portal', 'fuse.gvfsd-fuse'}
SECURE_BOOT_VAR = 'SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c'


class ToolError(Exception):
    """Erreur renvoyée au modèle sous forme de texte (il peut corriger sa demande)."""


@dataclass
class Env:
    """Racines du système, remplaçables par des dossiers factices dans les tests."""
    proc: Path = Path('/proc')
    sys: Path = Path('/sys')
    etc: Path = Path('/etc')
    home: Path = field(default_factory=Path.home)
    run: Callable[[list[str]], tuple[int, str]] | None = None
    ids: tuple[Path, ...] = (Path('/usr/share/misc'), Path('/usr/share/hwdata'))
    library: Any = None  # nexus.library.Library, si la bibliothèque de documents est configurée
    agenda: Any = None   # nexus.agenda.Agenda : rendez-vous et choses à faire
    mailbox: Any = None  # nexus.mail.Mailbox : courriels importants déjà relevés (résumés)

    def command(self, argv: list[str]) -> tuple[int, str]:
        if self.run is not None:
            return self.run(argv)
        program = shutil.which(argv[0], path=SAFE_PATH)
        if program is None:
            raise ToolError(f'commande {argv[0]} absente de ce système')
        try:
            done = subprocess.run([program, *argv[1:]], capture_output=True, timeout=COMMAND_TIMEOUT,
                                  stdin=subprocess.DEVNULL, env={'PATH': SAFE_PATH, 'LANG': 'C.UTF-8'},
                                  check=False)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f'{argv[0]} n\'a pas répondu à temps') from exc
        output = (done.stdout + done.stderr)[:200_000].decode('utf-8', errors='replace')
        return done.returncode, output


def _read(path: Path, limit: int = 65536) -> str | None:
    try:
        with open(path, 'rb') as handle:
            return handle.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return None


def _mib(value: float) -> str:
    return f'{value / 1024:.1f} Go' if value >= 1024 else f'{value:.0f} Mo'


# --------------------------------------------------------------------------- système
def infos_systeme(env: Env) -> str:
    lines = []
    osr = _read(env.etc / 'os-release') or ''
    name = next((l.split('=', 1)[1].strip('"') for l in osr.splitlines() if l.startswith('PRETTY_NAME=')), '?')
    lines.append(f'Système : {name}, noyau {os.uname().release}')
    cpu = _read(env.proc / 'cpuinfo', 200_000) or ''
    model = next((l.split(':', 1)[1].strip() for l in cpu.splitlines() if l.startswith('model name')), '?')
    flags = next((l.split(':', 1)[1].split() for l in cpu.splitlines() if l.startswith('flags')), [])
    lines.append(f'Processeur : {model}, {os.cpu_count()} cœurs logiques'
                 + (', AVX2 oui' if 'avx2' in flags else ', AVX2 non (IA plus lente)'))
    mem = {l.split(':')[0]: int(l.split()[1]) // 1024 for l in (_read(env.proc / 'meminfo') or '').splitlines()
           if l.split(':')[0] in ('MemTotal', 'MemAvailable', 'SwapTotal') and len(l.split()) >= 2}
    if mem:
        lines.append(f"Mémoire : {_mib(mem.get('MemAvailable', 0))} libres sur {_mib(mem.get('MemTotal', 0))}"
                     f", swap {_mib(mem.get('SwapTotal', 0))}")
    efi = env.sys / 'firmware/efi'
    if efi.is_dir():
        raw = None
        try:
            raw = (efi / 'efivars' / SECURE_BOOT_VAR).read_bytes()
        except OSError:
            pass
        secure = 'inconnu' if not raw else ('activé' if raw[-1] == 1 else 'désactivé')
        lines.append(f'Démarrage : UEFI, Secure Boot {secure}')
    else:
        lines.append('Démarrage : BIOS (ancien mode)')
    live = Path('/run/live/medium').exists() if env.proc == Path('/proc') else False
    if live:
        persistent = any(Path('/run/live/persistence').glob('*'))
        lines.append('Session : Live sur clé USB, ' + ('avec persistance' if persistent else 'sans persistance'))
    for supply in sorted((env.sys / 'class/power_supply').glob('*')):
        if (_read(supply / 'type') or '').strip() != 'Battery':
            continue
        cap = (_read(supply / 'capacity') or '?').strip()
        status = (_read(supply / 'status') or '?').strip()
        full = _read(supply / 'energy_full') or _read(supply / 'charge_full')
        design = _read(supply / 'energy_full_design') or _read(supply / 'charge_full_design')
        health = ''
        try:
            health = f', santé {100 * int(full) / int(design):.0f} % de la capacité d\'origine'
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        lines.append(f'Batterie {supply.name} : {cap} %, {status}{health}')
    uptime = _read(env.proc / 'uptime')
    if uptime:
        minutes = int(float(uptime.split()[0])) // 60
        lines.append(f'Allumé depuis : {minutes // 60} h {minutes % 60:02d} min')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- processus
def _process_table(env: Env) -> dict[int, tuple[str, int, int]]:
    """pid -> (nom, temps CPU en ticks, mémoire résidente en Mo)."""
    page_kib = os.sysconf('SC_PAGE_SIZE') // 1024
    table = {}
    for entry in env.proc.iterdir():
        if not entry.name.isdigit():
            continue
        stat = _read(entry / 'stat', 4096)
        statm = _read(entry / 'statm', 512)
        if not stat or not statm or ')' not in stat:
            continue
        name = stat[stat.find('(') + 1:stat.rfind(')')]
        fields = stat[stat.rfind(')') + 2:].split()
        try:
            ticks = int(fields[11]) + int(fields[12])
            rss = int(statm.split()[1]) * page_kib // 1024
        except (IndexError, ValueError):
            continue
        table[int(entry.name)] = (name, ticks, rss)
    return table


def processus_gourmands(env: Env, critere: str = 'memoire', nombre: int = 8) -> str:
    first = _process_table(env)
    start = time.monotonic()
    time.sleep(0.6)
    second = _process_table(env)
    elapsed = max(time.monotonic() - start, 0.01)
    hz = os.sysconf('SC_CLK_TCK')
    groups: dict[str, list[float]] = {}
    for pid, (name, ticks, rss) in second.items():
        before = first.get(pid)
        cpu = 0.0 if before is None or before[0] != name else 100 * (ticks - before[1]) / hz / elapsed
        group = groups.setdefault(name, [0, 0.0, 0])
        group[0] += 1
        group[1] += cpu
        group[2] += rss
    key = 1 if critere == 'processeur' else 2
    # Les fils du noyau (aucune mémoire propre) n'intéressent pas l'utilisateur.
    ranked = sorted(((n, g) for n, g in groups.items() if g[2] > 0), key=lambda item: -item[1][key])[:nombre]
    lines = [f'Programmes classés par {"processeur" if key == 1 else "mémoire"} '
             f'(100 % = un cœur entier ; {os.cpu_count()} cœurs) :']
    for name, (count, cpu, rss) in ranked:
        lines.append(f'- {name}' + (f' ({count} processus)' if count > 1 else '')
                     + f' : mémoire {_mib(rss)}, processeur {cpu:.0f} %')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- réseau
def etat_reseau(env: Env) -> str:
    lines = []
    for iface in sorted((env.sys / 'class/net').glob('*')):
        if iface.name == 'lo':
            continue
        kind = 'Wi-Fi' if (iface / 'wireless').exists() or (iface / 'phy80211').exists() else (
            'filaire' if (iface / 'device').exists() else 'virtuelle')
        state = (_read(iface / 'operstate') or '?').strip()
        driver = ''
        try:
            driver = os.path.basename(os.readlink(iface / 'device/driver'))
        except OSError:
            pass
        lines.append(f'Interface {iface.name} ({kind}) : {state}' + (f', pilote {driver}' if driver else ''))
    if not lines:
        lines.append('Aucune interface réseau détectée (pilote manquant ?)')
    for kill in sorted((env.sys / 'class/rfkill').glob('rfkill*')):
        kind = (_read(kill / 'type') or '').strip()
        soft, hard = (_read(kill / 'soft') or '0').strip(), (_read(kill / 'hard') or '0').strip()
        if hard == '1':
            lines.append(f'{kind} BLOQUÉ par un interrupteur matériel ou une touche Fn du PC')
        elif soft == '1':
            lines.append(f'{kind} désactivé par logiciel (mode avion ?)')
    route = _read(env.proc / 'net/route') or ''
    gateway = None
    for row in route.splitlines()[1:]:
        cols = row.split()
        if len(cols) > 2 and cols[1] == '00000000':
            gw = int(cols[2], 16)
            gateway = f'{gw & 255}.{gw >> 8 & 255}.{gw >> 16 & 255}.{gw >> 24 & 255} via {cols[0]}'
            break
    lines.append(f'Passerelle par défaut : {gateway or "aucune (pas de connexion au réseau)"}')
    dns = [l.split()[1] for l in (_read(env.etc / 'resolv.conf') or '').splitlines()
           if l.startswith('nameserver') and len(l.split()) > 1]
    lines.append('DNS : ' + (', '.join(dns[:3]) if dns else 'aucun'))
    try:
        code, out = env.command(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'])
        if code == 0:
            for row in out.splitlines()[:8]:
                device, kind, state, conn = (row.split(':') + ['', '', '', ''])[:4]
                if kind not in ('loopback', ''):
                    lines.append(f'NetworkManager {device} : {state}' + (f' ({conn})' if conn else ''))
        code, out = env.command(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list',
                                 '--rescan', 'no'])
        if code == 0 and out.strip():
            seen = [r.split(':') for r in out.splitlines() if r.split(':')[0]][:6]
            lines.append('Wi-Fi visibles : ' + ', '.join(f'{s[0]} ({s[1]} %)' for s in seen if len(s) > 1))
    except ToolError:
        pass
    return '\n'.join(lines)


# --------------------------------------------------------------------------- matériel
def _ids(env: Env, filename: str) -> dict[str, str]:
    """Lit pci.ids / usb.ids : 'vvvv' -> fabricant, 'vvvv:dddd' -> modèle."""
    for folder in env.ids:
        text = _read(folder / filename, 8_000_000)
        if text:
            names, vendor = {}, None
            for line in text.splitlines():
                if not line or line.startswith('#') or line.startswith('C '):
                    vendor = None if line.startswith('C ') else vendor
                    continue
                if not line.startswith('\t') and len(line) > 6 and line[4:6] == '  ':
                    vendor = line[:4].lower()
                    names[vendor] = line[6:]
                elif vendor and line.startswith('\t') and not line.startswith('\t\t') and line[5:7] == '  ':
                    names[f'{vendor}:{line[1:5].lower()}'] = line[7:]
            return names
    return {}


PCI_CLASSES = {'03': 'graphique', '02': 'reseau', '0d': 'reseau', '04': 'audio', '01': 'stockage'}


def materiel(env: Env, categorie: str = 'tout') -> str:
    names = _ids(env, 'pci.ids')
    lines = []
    for dev in sorted((env.sys / 'bus/pci/devices').glob('*')):
        cls = (_read(dev / 'class') or '0x000000').strip()[2:]
        kind = PCI_CLASSES.get(cls[:2])
        if cls.startswith('0c03'):
            kind = 'usb'
        if kind is None or (categorie != 'tout' and categorie != kind):
            continue
        vendor = (_read(dev / 'vendor') or '').strip()[2:].lower()
        device = (_read(dev / 'device') or '').strip()[2:].lower()
        label = f"{names.get(vendor, vendor)} {names.get(f'{vendor}:{device}', device)}"
        try:
            driver = 'pilote ' + os.path.basename(os.readlink(dev / 'driver'))
        except OSError:
            driver = 'AUCUN PILOTE CHARGÉ'
        lines.append(f'[{kind}] {label} — {driver}')
    if categorie in ('tout', 'usb'):
        usb_names = _ids(env, 'usb.ids')
        for dev in sorted((env.sys / 'bus/usb/devices').glob('*')):
            if (_read(dev / 'bDeviceClass') or '').strip() == '09' or not (dev / 'idVendor').exists():
                continue
            vendor = (_read(dev / 'idVendor') or '').strip()
            product = (_read(dev / 'product') or '').strip()
            pid = (_read(dev / 'idProduct') or '').strip()
            label = product or usb_names.get(f'{vendor}:{pid}', f'{vendor}:{pid}')
            lines.append(f'[usb] {(_read(dev / "manufacturer") or usb_names.get(vendor, "")).strip()} {label}'.strip())
    counted: dict[str, int] = {}
    for line in lines:
        counted[line] = counted.get(line, 0) + 1
    return ('\n'.join(line + (f' (×{n})' if n > 1 else '') for line, n in counted.items())
            or f'Aucun périphérique dans la catégorie « {categorie} ».')


# --------------------------------------------------------------------------- journal
JOURNAL_DENIED = ('insufficient permissions', 'No journal files were opened')


def _journal(env: Env, argv: list[str]) -> str:
    code, out = env.command(['journalctl', '--no-pager', '-q', *argv])
    if any(marker in out for marker in JOURNAL_DENIED):
        raise ToolError('journal système illisible avec les droits actuels')
    if code not in (0, 1):
        raise ToolError('journal système indisponible')
    return out


def erreurs_recentes(env: Env, minutes: int = 60) -> str:
    out = _journal(env, ['-b', '-p', 'err', '--since', f'-{minutes}min', '-o', 'short', '-n', '40'])
    rows = [line for line in out.splitlines() if line.strip()]
    if not rows:
        return f'Aucune erreur enregistrée dans les {minutes} dernières minutes.'
    # Les lignes répétées n'apportent rien : on les compte.
    counts: dict[str, int] = {}
    for row in rows:
        message = row.split(': ', 1)[-1]
        counts[message] = counts.get(message, 0) + 1
    return (f'Erreurs des {minutes} dernières minutes (journal système, données brutes) :\n'
            + '\n'.join(f'- {m}' + (f' (×{n})' if n > 1 else '') for m, n in counts.items()))


def firmwares_manquants(env: Env) -> str:
    out = _journal(env, ['-k', '-b', '-o', 'cat', '-n', '4000'])
    missing = []
    for line in out.splitlines():
        low = line.lower()
        if 'firmware' in low and ('failed' in low or 'error' in low or 'missing' in low):
            if line not in missing:
                missing.append(line.strip())
    if not missing:
        return 'Aucun firmware manquant signalé par le noyau depuis le démarrage.'
    return 'Firmwares que le noyau n\'a pas pu charger :\n' + '\n'.join(f'- {m}' for m in missing[:15])


# --------------------------------------------------------------------------- stockage
def stockage(env: Env) -> str:
    lines = ['Disques :']
    for block in sorted((env.sys / 'block').glob('*')):
        if block.name.startswith(('loop', 'ram', 'zram', 'sr', 'dm-')):
            continue
        try:
            size_gb = int(_read(block / 'size') or 0) * 512 / 1e9
        except ValueError:
            continue
        if size_gb < 0.1:
            continue
        model = (_read(block / 'device/model') or '').strip()
        removable = (_read(block / 'removable') or '0').strip() == '1'
        rotational = (_read(block / 'queue/rotational') or '0').strip() == '1'
        kind = 'amovible (clé USB ?)' if removable else ('disque dur' if rotational else 'SSD')
        lines.append(f'- {block.name} : {size_gb:.0f} Go, {kind}' if size_gb >= 10 else
                     f'- {block.name} : {size_gb:.1f} Go, {kind}' + (f', {model}' if model else ''))
    lines.append('Systèmes de fichiers montés :')
    seen = set()
    for row in (_read(env.proc / 'mounts') or '').splitlines():
        cols = row.split()
        if len(cols) < 3 or cols[2] in PSEUDO_FS or cols[1] in seen:
            continue
        seen.add(cols[1])
        mount = cols[1].replace('\\040', ' ')
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize / 2**20
        free = st.f_bavail * st.f_frsize / 2**20
        if total <= 0:
            continue
        note = ' (clé Live, lecture seule)' if mount == '/run/live/medium' else ''
        lines.append(f'- {mount} ({cols[2]}) : {_mib(free)} libres sur {_mib(total)}{note}')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- documents
def _visible(path: Path, home: Path) -> bool:
    return not any(part.startswith('.') for part in path.relative_to(home).parts)


def chercher_documents(env: Env, mot: str = '') -> str:
    home = env.home.resolve()
    needle, found, scanned = mot.lower().strip(), [], 0
    for root, dirs, files in os.walk(home):
        dirs[:] = [d for d in sorted(dirs) if not d.startswith('.')]
        if len(Path(root).relative_to(home).parts) >= 6:
            dirs[:] = []
        for name in sorted(files):
            scanned += 1
            path = Path(root) / name
            if name.startswith('.') or path.suffix.lower() not in DOC_SUFFIXES:
                continue
            if needle and needle not in name.lower():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            found.append(f'- {path.relative_to(home)} ({size // 1024 or 1} Ko)')
            if len(found) >= 25:
                break
        if len(found) >= 25 or scanned > 20000:
            break
    if not found:
        return (f'Aucun document contenant « {mot} » dans le dossier personnel.' if needle
                else 'Aucun document dans le dossier personnel.')
    return 'Documents trouvés (chemins relatifs au dossier personnel) :\n' + '\n'.join(found)


def lire_document(env: Env, chemin: str, debut: int = 0) -> str:
    home = env.home.resolve()
    candidate = Path(chemin.strip()).expanduser()
    path = (candidate if candidate.is_absolute() else home / candidate).resolve()
    library_root = getattr(env.library, 'root', None)
    if not candidate.is_absolute() and not path.is_file() and library_root is not None:
        # rechercher_documents et depenses citent des chemins relatifs à la bibliothèque.
        path = (Path(library_root) / candidate).resolve()
    if home not in path.parents or not _visible(path, home):
        raise ToolError('seuls les documents non cachés du dossier personnel sont lisibles')
    if not path.is_file():
        raise ToolError(f'document introuvable : {chemin} (utiliser chercher_documents)')
    if path.suffix.lower() not in DOC_SUFFIXES:
        raise ToolError('type de fichier non pris en charge (texte, PDF ou image)')
    if path.stat().st_size > 30 * 2**20:
        raise ToolError('document trop gros (plus de 30 Mo)')
    recognised = False
    if path.suffix.lower() == '.pdf':
        code, text = env.command(['pdftotext', '-layout', '-l', '40', '-enc', 'UTF-8', str(path), '-'])
        if code != 0:
            raise ToolError('PDF illisible (protégé ou endommagé)')
    elif path.suffix.lower() in IMAGE_SUFFIXES:
        text = ''
    else:
        text = _read(path, 2_000_000) or ''
    text = '\n'.join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(text) < 20 and path.suffix.lower() in IMAGE_SUFFIXES | {'.pdf'}:
        # Scan ou photo : le texte a été reconnu à l'indexation, on le prend dans la bibliothèque.
        indexed = env.library.text_of(path) if env.library is not None else None
        if indexed is None or not indexed[0]:
            raise ToolError('ce document est une image sans texte lisible pour l\'instant '
                            '(l\'indexer avec la reconnaissance de texte : nexus docs indexer)')
        text, recognised = indexed
    window = MAX_RESULT_CHARS - 200
    part = text[debut:debut + window]
    rest = len(text) - debut - len(part)
    header = f'Contenu de {path.relative_to(home)} (caractères {debut}–{debut + len(part)} sur {len(text)}) :\n'
    if recognised:
        header = f'[{OCR_NOTE}]\n' + header
    footer = f'\n[… {rest} caractères restants : relire avec debut={debut + len(part)}]' if rest > 0 else ''
    return header + part + footer


# --------------------------------------------------------------------------- bibliothèque
def rechercher_documents(env: Env, question: str) -> str:
    library = env.library
    if library is None or not library.stats()['documents']:
        return ('Bibliothèque de documents non indexée (commande : nexus docs indexer). '
                'Recherche par nom de fichier à la place :\n' + chercher_documents(env, ''))
    results = library.search(question, k=4)
    if not results:
        return 'Aucun passage pertinent dans les documents.'
    lines = ['Passages trouvés dans les documents (citer la source [fichier, page]) :']
    for r in results:
        source = f"[{r['path']}, page {r['page']}]"
        note = f' [{OCR_NOTE}]' if r.get('ocr') else ''
        lines.append(f"{source}{' (' + r['title'] + ')' if r['title'] else ''}{note}\n{r['text'][:520]}")
    return '\n\n'.join(lines)


def depenses(env: Env, categorie: str = 'toutes', annee: int = 0, emetteur: str = '') -> str:
    from .library import CATEGORIES, money
    library = env.library
    if library is None:
        return 'Bibliothèque de documents non configurée.'
    rows = library.expenses(None if categorie == 'toutes' else categorie, annee or None, emetteur or None)
    scope = ', '.join(x for x in (CATEGORIES.get(categorie, ''), str(annee) if annee else '',
                                  emetteur) if x) or 'tous les documents'
    if not rows:
        return f'Aucun document avec un montant pour : {scope}.'
    total = sum(r['amount_cents'] for r in rows)
    lines = [f'Montants relevés dans les documents ({scope}) : {len(rows)} document(s), '
             f'total {money(total)}']
    lines += [f"- {r['date'] or 'sans date'} · {r['issuer'] or '?'} · {r['title'] or ''} : "
              f"{money(r['amount_cents'])} [{r['path']}]" + (' (scan : montant à vérifier)' if r.get('ocr') else '')
              for r in rows[:20]]
    if len(rows) > 20:
        lines.append(f'… et {len(rows) - 20} autres')
    return '\n'.join(lines)


def agenda(env: Env, jours: int = 7) -> str:
    if env.agenda is None:
        return 'Agenda non configuré.'
    from datetime import datetime
    return ('Agenda de l\'utilisateur (consultation seule ; pour y écrire, il doit le demander : '
            '« rappelle-moi… », « ajoute… à ma liste ») :\n' + env.agenda.overview(datetime.now(), max(1, min(jours, 60))))


def courrier(env: Env, jours: int = 7) -> str:
    if env.mailbox is None:
        return 'Courrier non configuré.'
    return env.mailbox.overview(days=max(1, min(jours, 30)))


# --------------------------------------------------------------------------- registre
@dataclass(frozen=True)
class Tool:
    name: str
    label: str            # libellé affiché à l'utilisateur
    description: str
    params: dict[str, dict] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    func: Callable[..., str] = lambda env: ''

    def schema(self) -> dict[str, Any]:
        props = {k: {kk: vv for kk, vv in v.items() if kk != 'default'} for k, v in self.params.items()}
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description,
            'parameters': {'type': 'object', 'properties': props, 'required': list(self.required)}}}


TOOLS = [
    Tool('infos_systeme', 'Infos système',
         "Système, processeur, mémoire, mode de démarrage, Secure Boot, batterie, session Live.",
         func=infos_systeme),
    Tool('processus_gourmands', 'Programmes gourmands',
         'Programmes qui utilisent le plus de mémoire ou de processeur en ce moment.',
         {'critere': {'type': 'string', 'enum': ['memoire', 'processeur'], 'default': 'memoire'},
          'nombre': {'type': 'integer', 'minimum': 1, 'maximum': 15, 'default': 8}},
         func=processus_gourmands),
    Tool('etat_reseau', 'État du réseau',
         'Interfaces réseau, Wi-Fi bloqué ou non, connexion, passerelle, DNS, réseaux Wi-Fi visibles.',
         func=etat_reseau),
    Tool('materiel', 'Matériel et pilotes',
         'Matériel détecté (carte graphique, réseau, son, stockage, USB) et pilote utilisé par chacun.',
         {'categorie': {'type': 'string', 'enum': ['tout', 'graphique', 'reseau', 'audio', 'stockage', 'usb'],
                        'default': 'tout'}},
         func=materiel),
    Tool('erreurs_recentes', 'Erreurs récentes',
         'Erreurs récentes du journal système, pour diagnostiquer un problème.',
         {'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440, 'default': 60}},
         func=erreurs_recentes),
    Tool('firmwares_manquants', 'Firmwares manquants',
         'Firmwares que le noyau n\'a pas trouvés au démarrage (cause fréquente de Wi-Fi ou son absents).',
         func=firmwares_manquants),
    Tool('stockage', 'Disques', 'Disques, clés USB et espace libre de chaque système de fichiers.',
         func=stockage),
    Tool('chercher_documents', 'Recherche de documents',
         "Cherche des documents (texte, PDF…) dans le dossier personnel de l'utilisateur, par nom.",
         {'mot': {'type': 'string', 'maxLength': 80, 'default': ''}},
         func=chercher_documents),
    Tool('lire_document', 'Lecture d\'un document',
         "Lit un document texte ou PDF du dossier personnel (chemin donné par chercher_documents).",
         {'chemin': {'type': 'string', 'maxLength': 400},
          'debut': {'type': 'integer', 'minimum': 0, 'maximum': 10_000_000, 'default': 0}},
         ('chemin',), func=lire_document),
    Tool('rechercher_documents', 'Recherche dans les documents',
         "Cherche la réponse à une question DANS le contenu des documents de l'utilisateur "
         "(factures, contrats, impôts, santé…) et renvoie les passages avec leur source.",
         {'question': {'type': 'string', 'maxLength': 300}}, ('question',), func=rechercher_documents),
    Tool('depenses', 'Dépenses',
         "Additionne les montants des factures et documents classés, par catégorie, année ou émetteur.",
         {'categorie': {'type': 'string', 'enum': ['toutes', 'energie', 'logement', 'impots', 'banque', 'sante',
                                                    'vehicule', 'travail', 'telecom', 'achats', 'autre'],
                        'default': 'toutes'},
          'annee': {'type': 'integer', 'minimum': 0, 'maximum': 2100, 'default': 0},
          'emetteur': {'type': 'string', 'maxLength': 60, 'default': ''}},
         func=depenses),
    Tool('agenda', 'Agenda',
         "Rendez-vous, rappels et liste de choses à faire de l'utilisateur pour les jours qui viennent "
         "(agenda, calendrier, emploi du temps, ce qui est prévu).",
         {'jours': {'type': 'integer', 'minimum': 1, 'maximum': 60, 'default': 7}}, func=agenda),
    Tool('courrier', 'Courrier',
         "Courriels importants reçus ces derniers jours dans les boîtes mail de l'utilisateur (expéditeur, objet, résumé). "
         "Lecture seule : impossible d'envoyer ou de répondre.",
         {'jours': {'type': 'integer', 'minimum': 1, 'maximum': 30, 'default': 7}}, func=courrier),
]


class Toolbox:
    def __init__(self, env: Env | None = None, tools: list[Tool] | None = None):
        self.env = env or Env()
        self.tools = {t.name: t for t in (tools or TOOLS)}

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def label(self, name: str) -> str:
        tool = self.tools.get(name)
        return tool.label if tool else name

    def validate(self, name: Any, args: Any) -> tuple[Tool, dict[str, Any]]:
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolError(f'outil inconnu : {name!r}. Outils disponibles : {", ".join(self.tools)}')
        if isinstance(args, str):  # certains modèles envoient les arguments en texte JSON
            try:
                args = json.loads(args or '{}')
            except json.JSONDecodeError as exc:
                raise ToolError('arguments illisibles') from exc
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError('les arguments doivent être un objet')
        clean = {}
        for key, value in args.items():
            spec = tool.params.get(key)
            if spec is None:
                continue  # argument inventé : ignoré plutôt que bloquant
            if spec['type'] == 'integer':
                if isinstance(value, str) and value.strip().isdigit():
                    value = int(value.strip())
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ToolError(f'{key} doit être un entier')
                value = min(max(value, spec['minimum']), spec['maximum'])
            else:
                if not isinstance(value, str):
                    raise ToolError(f'{key} doit être du texte')
                if 'enum' in spec:
                    value = value.strip().lower().replace('é', 'e')
                    if value not in spec['enum']:
                        raise ToolError(f'{key} doit valoir {", ".join(spec["enum"])}')
                elif len(value) > spec.get('maxLength', 200) or '\x00' in value:
                    raise ToolError(f'{key} trop long ou invalide')
            clean[key] = value
        for key in tool.required:
            if key not in clean:
                raise ToolError(f'argument manquant : {key}')
        for key, spec in tool.params.items():
            clean.setdefault(key, spec.get('default'))
        return tool, clean

    def call(self, name: Any, args: Any) -> str:
        """Exécute un outil et renvoie TOUJOURS du texte (erreur comprise), borné en taille."""
        try:
            tool, clean = self.validate(name, args)
            result = tool.func(self.env, **clean)
        except ToolError as exc:
            result = f'ERREUR : {exc}'
        except Exception as exc:  # un outil défaillant ne doit pas faire tomber le dialogue
            result = f'ERREUR : l\'outil a échoué ({type(exc).__name__})'
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + '\n[… tronqué]'
        return result
