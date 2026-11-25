"""La sauvegarde : une copie chiffrée de tes documents et de la mémoire de NEXUS, sur un autre disque.

Le mini-PC devient ton cloud personnel : une panne de disque ne doit pas tout emporter. NEXUS
ne réinvente pas la sauvegarde, il pilote restic, l'outil libre de référence :

- tout est chiffré avant d'être écrit : un disque de sauvegarde perdu ou volé ne livre rien ;
- seules les nouveautés sont copiées, et plusieurs dates sont gardées (7 jours, 5 semaines,
  12 mois) : un fichier abîmé ou effacé par erreur se retrouve tel qu'il était avant ;
- chaque nuit, NEXUS prouve que la sauvegarde se restaure : il en ressort quelques fichiers
  pris au hasard, les compare aux originaux, rouvre une de ses bases de données restaurée,
  et fait relire à restic une partie du dépôt (tout y passe en un mois).

Ce qui est sauvegardé : tes documents, les réglages de NEXUS (boîtes mail comprises) et ses
données (conversations, agenda, souvenirs, journal, clés du tunnel et du certificat). Pas les
moteurs ni leurs modèles, qui se retéléchargent.

La clé de la sauvegarde est créée par NEXUS et gardée sur le PC pour les copies de nuit. Elle
doit aussi exister AILLEURS, sur papier : sans elle, la sauvegarde est illisible, pour toi aussi.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from typing import Any, Callable

from .ollama import OllamaError

TOOLS_PATH = '/usr/bin:/bin:/usr/local/bin'
FOLDER = 'NEXUS-sauvegarde'          # le dépôt, dans le disque choisi
KEEP = ('--keep-daily', '7', '--keep-weekly', '5', '--keep-monthly', '12')
SAMPLE = 5                           # fichiers ressortis chaque nuit pour preuve
READ_CYCLE = 30                      # le dépôt entier est relu en autant de nuits
KEY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'      # sans 0/O ni 1/I : la clé se recopie à la main

Run = Callable[[list[str], dict[str, str], int], tuple[int, str, str]]


def _exec(argv: list[str], env: dict[str, str], timeout: int) -> tuple[int, str, str]:
    program = shutil.which(argv[0], path=TOOLS_PATH)
    if program is None:
        raise OllamaError('Il manque « restic » pour les sauvegardes : sudo apt install restic')
    try:
        done = subprocess.run([program, *argv[1:]], env=env, capture_output=True, text=True, errors='replace',
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, '', 'délai dépassé'
    except OSError as exc:
        return 127, '', type(exc).__name__
    return done.returncode, done.stdout, done.stderr


def new_key() -> str:
    """Une clé de 120 bits, en six groupes faciles à recopier : ABCD-EFGH-…"""
    letters = ''.join(secrets.choice(KEY_ALPHABET) for _ in range(24))
    return '-'.join(letters[i:i + 4] for i in range(0, 24, 4))


def tidy_key(text: str) -> str:
    """La clé telle que tapée (minuscules, espaces, tirets oubliés), remise en forme."""
    letters = ''.join(ch for ch in text.upper() if ch.isalnum())
    return '-'.join(letters[i:i + 4] for i in range(0, len(letters), 4))


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            sha.update(block)
    return sha.hexdigest()


def copy_databases(databases: list[Path], target: Path) -> dict[str, str]:
    """Copie cohérente de chaque base SQLite dans `target`, même si elle sert en ce moment.
    Renvoie {nom du fichier: chemin d'origine} pour celles qui existaient."""
    names = {}
    for database in databases:
        if not database.is_file():
            continue
        source = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=30)
        try:
            copy = sqlite3.connect(target / database.name)
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        names[database.name] = str(database)
    return names


def human(size: float) -> str:
    for unit in ('octets', 'Ko', 'Mo', 'Go'):
        if size < 1024 or unit == 'Go':
            return f'{size:.0f} {unit}' if unit == 'octets' else f'{size:.1f} {unit}'.replace('.', ',')
        size /= 1024
    return ''


class Backup:
    def __init__(self, base: Path, *, documents: Path | None, data: Path, config: Path | None,
                 databases: list[Path], excluded: list[Path] | None = None, run: Run | None = None):
        self.base = Path(base)                      # <données>/sauvegarde : clé, état, instantané des bases
        self.documents, self.data, self.config = documents, Path(data), config
        self.databases = [Path(p) for p in databases]
        self.excluded = [Path(p) for p in (excluded or [])]
        self._run = run or _exec
        self._lock = threading.Lock()
        self.key_path = self.base / 'cle'
        self.settings_path = self.base / 'reglages.json'
        self.state_path = self.base / 'etat.json'
        self.staging = self.base / 'instantane'

    # -- réglages -------------------------------------------------------------------------
    def destination(self) -> Path | None:
        try:
            value = json.loads(self.settings_path.read_text(encoding='utf-8')).get('destination')
        except (OSError, ValueError, AttributeError):
            return None
        return Path(value) if isinstance(value, str) and os.path.isabs(value) else None

    def configured(self) -> bool:
        return self.destination() is not None and self.key_path.is_file()

    def key(self) -> str | None:
        try:
            return self.key_path.read_text(encoding='ascii').strip() or None
        except (OSError, UnicodeDecodeError):
            return None

    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path: Path, text: str) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            self.base.chmod(0o700)
        except OSError:
            pass
        temporary = path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(text)
        os.replace(temporary, path)

    def _remember(self, **changes: Any) -> dict[str, Any]:
        state = {**self.state(), **changes}
        self._write(self.state_path, json.dumps(state, ensure_ascii=False, indent=1))
        return state

    def same_disk(self) -> bool:
        """Vrai si la sauvegarde est sur le même disque que les documents : elle ne protège alors pas d'une panne."""
        destination = self.destination()
        try:
            return (destination is not None and self.documents is not None
                    and os.stat(destination).st_dev == os.stat(self.documents).st_dev)
        except OSError:
            return False

    # -- restic ---------------------------------------------------------------------------
    def _restic(self, *argv: str, repository: Path | None = None, key_file: Path | None = None,
                timeout: int = 600) -> tuple[int, str, str]:
        repository = repository or (self.destination() or Path('/nonexistent')) / FOLDER
        env = {'PATH': TOOLS_PATH, 'HOME': str(self.base), 'LANG': 'C.UTF-8',
               'RESTIC_REPOSITORY': str(repository), 'RESTIC_PASSWORD_FILE': str(key_file or self.key_path),
               'RESTIC_CACHE_DIR': str(self.base / 'cache')}
        return self._run(['restic', *argv], env, timeout)

    def _problem(self, repository: Path | None = None, key_file: Path | None = None) -> str | None:
        """Pourquoi le dépôt n'est pas utilisable (None : il l'est)."""
        destination = repository.parent if repository is not None else self.destination()
        if destination is None:
            return 'La sauvegarde n\'est pas réglée : nexus sauvegarde installer /chemin/du/disque'
        if not destination.is_dir():
            return f'Le disque de sauvegarde est absent ({destination}).'
        code, _out, err = self._restic('cat', 'config', repository=repository, key_file=key_file, timeout=120)
        return None if code == 0 else self._explain(err, destination)

    @staticmethod
    def _explain(err: str, destination: Path) -> str:
        if 'wrong password' in err or 'no key found' in err:
            return 'La clé ne correspond pas à cette sauvegarde.'
        return f'Aucune sauvegarde NEXUS trouvée dans {destination}.'

    # -- installation ---------------------------------------------------------------------
    def setup(self, destination: Path, key: str | None = None) -> str:
        """Choisit le disque, crée la clé (une fois) et le dépôt chiffré. Renvoie la clé, à noter sur papier.
        `key` : la clé d'une sauvegarde existante à reprendre (PC neuf, après une restauration)."""
        destination = Path(destination).expanduser().absolute()
        if not destination.is_dir():
            raise ValueError(f'dossier introuvable : {destination} (le disque est-il branché ?)')
        for mine in (self.data, self.documents):
            if mine is not None and (destination == mine or mine in destination.parents):
                raise ValueError('la sauvegarde ne peut pas être rangée dans ce qu\'elle sauvegarde')
        if not os.access(destination, os.W_OK):
            raise ValueError(f'pas le droit d\'écrire dans {destination}')
        repository = destination / FOLDER
        exists = (repository / 'config').is_file()
        if key is not None:
            if not exists:
                raise ValueError('aucune sauvegarde à reprendre dans ce dossier')
            previous = self.key()
            self._write(self.key_path, tidy_key(key) + '\n')
            if self._problem(repository) is not None:       # mauvaise clé : on ne garde pas une clé qui n'ouvre rien
                if previous is None:
                    self.key_path.unlink(missing_ok=True)
                else:
                    self._write(self.key_path, previous + '\n')
                raise OllamaError('Cette clé n\'ouvre pas la sauvegarde de ce disque.')
        elif self.key() is None:
            self._write(self.key_path, new_key() + '\n')
        if exists:                                      # un dépôt existe déjà : il doit s'ouvrir avec notre clé
            if self._problem(repository) is not None:
                raise OllamaError('Ce disque contient déjà une sauvegarde NEXUS faite avec une autre clé. '
                                  'Pour la reprendre, donner sa clé : nexus sauvegarde installer DOSSIER --reprendre. '
                                  'Pour repartir de zéro, choisir un autre dossier.')
        else:
            code, _out, err = self._restic('init', repository=repository, timeout=120)
            if code != 0:
                raise OllamaError('Création de la sauvegarde impossible : ' + (err.strip().splitlines() or ['erreur'])[-1][:200])
        self._write(self.settings_path, json.dumps({'destination': str(destination)}, ensure_ascii=False))
        return self.key() or ''

    # -- l'instantané des bases ----------------------------------------------------------------
    def _stage(self) -> list[Path]:
        """Copie cohérente de chaque base de données (elles servent pendant la sauvegarde), et le plan de restauration."""
        shutil.rmtree(self.staging, ignore_errors=True)
        self.staging.mkdir(parents=True)
        names = copy_databases(self.databases, self.staging)
        staged = [self.staging / name for name in names]
        plan = {'fait_le': datetime.now().isoformat(timespec='seconds'),
                'documents': str(self.documents) if self.documents else None, 'donnees': str(self.data),
                'reglages': str(self.config) if self.config else None, 'bases': names}
        (self.staging / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
        return staged

    def _sources(self) -> list[Path]:
        # L'instantané des bases en fait toujours partie, où qu'il soit rangé.
        roots = [p for p in (self.documents, self.data, self.config, self.staging) if p is not None and p.is_dir()]
        return [p for p in roots if not any(other != p and other in p.parents for other in roots)]

    def _excludes(self) -> list[str]:
        """Ce qui n'est pas sauvegardé : les bases en service (leur instantané l'est), la clé, les moteurs."""
        excluded = [self.key_path, self.base / 'cache', self.state_path, self.base / 'nexus-preuve-*', *self.excluded]
        for database in self.databases:
            excluded += [database, Path(str(database) + '-wal'), Path(str(database) + '-shm')]
        return [argument for path in excluded for argument in ('--exclude', str(path))]

    # -- la sauvegarde de la nuit --------------------------------------------------------------
    def run(self, now: datetime | None = None, rng: random.Random | None = None) -> dict[str, Any]:
        """Sauvegarde, fait le ménage des anciennes dates, puis prouve que ça se restaure."""
        now = now or datetime.now()
        if not self._lock.acquire(blocking=False):
            return {'ok': False, 'message': 'Une sauvegarde est déjà en cours.'}
        try:
            result = self._run_locked(now, rng or random.Random())
        except (OSError, sqlite3.Error) as exc:
            result = {'ok': False, 'message': f'Sauvegarde interrompue ({type(exc).__name__}).'}
        finally:
            self._lock.release()
        stamp = now.isoformat(timespec='minutes')
        if result['ok']:
            self._remember(derniere_tentative=stamp, derniere_reussie=stamp, erreur=None, absent=False,
                           **{k: result[k] for k in ('fichiers', 'ajoute', 'dates', 'taille', 'preuve')})
        else:
            self._remember(derniere_tentative=stamp, erreur=result['message'], absent=result.get('absent', False))
        return result

    def _run_locked(self, now: datetime, rng: random.Random) -> dict[str, Any]:
        destination = self.destination()
        if destination is None or not destination.is_dir():
            return {'ok': False, 'message': self._problem() or '', 'absent': destination is not None}
        # Ouvre le dépôt (et le vérifie du même coup) en levant un verrou resté d'une coupure de courant.
        code, _out, err = self._restic('unlock', timeout=120)
        if code != 0:
            return {'ok': False, 'message': self._explain(err, destination)}
        staged = self._stage()
        sources = self._sources()
        # Les témoins : quelques fichiers dont on note l'empreinte avant, pour les comparer une fois ressortis.
        candidates = [p for p in (self.documents.rglob('*') if self.documents and self.documents.is_dir() else [])
                      if p.is_file() and not p.is_symlink()]
        witnesses = rng.sample(candidates, min(SAMPLE, len(candidates))) + staged      # et toutes les bases
        before = {path: digest(path) for path in witnesses}
        code, out, err = self._restic('backup', '--json', '--tag', 'nexus', '--host', 'nexus',
                                      '--time', now.strftime('%Y-%m-%d %H:%M:%S'), *self._excludes(),
                                      *map(str, sources), timeout=6 * 3600)
        summary = next((json.loads(line) for line in reversed(out.splitlines())
                        if line.startswith('{') and '"summary"' in line), None)
        if code not in (0, 3) or summary is None:       # 3 : sauvegarde faite, mais des fichiers illisibles
            return {'ok': False, 'message': 'La sauvegarde a échoué : ' + (err.strip().splitlines() or ['erreur'])[-1][:200]}
        unreadable = code == 3
        code, out, _err = self._restic('forget', '--tag', 'nexus', '--host', 'nexus', *KEEP, '--prune', '--json',
                                       timeout=3600)
        try:        # ce que le ménage a gardé : le nombre de dates qu'on peut encore ressortir
            dates = sum(len(group.get('keep') or []) for group in json.loads(out.strip().splitlines()[0])) if code == 0 else 0
        except (ValueError, IndexError, AttributeError):
            dates = 0
        proof = self._prove(summary['snapshot_id'], before, now)
        if proof is not None:
            return {'ok': False, 'message': 'Sauvegarde écrite, mais la preuve de restauration a échoué : ' + proof}
        size = sum(f.stat().st_size for f in (destination / FOLDER).rglob('*') if f.is_file())      # place prise sur le disque
        dates = dates or len(self.snapshots())
        return {'ok': True, 'fichiers': summary.get('total_files_processed', 0), 'ajoute': summary.get('data_added', 0),
                'dates': dates, 'taille': size, 'preuve': len(before),
                'message': (f"{summary.get('total_files_processed', 0)} fichiers, {human(summary.get('data_added', 0))} de nouveau, "
                            f"{len(before)} fichier(s) ressorti(s) et comparé(s)"
                            + (' ; attention, certains fichiers étaient illisibles' if unreadable else ''))}

    def _prove(self, snapshot: str, before: dict[Path, str], now: datetime) -> str | None:
        """Ressort les témoins de la sauvegarde et les compare. Renvoie le problème, ou None si tout est prouvé."""
        with tempfile.TemporaryDirectory(prefix='nexus-preuve-', dir=self.base) as tmp:
            includes = [argument for path in before for argument in ('--include', str(path))]
            if includes:
                code, _out, err = self._restic('restore', snapshot, '--target', tmp, *includes, timeout=1800)
                if code != 0:
                    return 'restauration refusée par restic (' + (err.strip().splitlines() or ['erreur'])[-1][:120] + ')'
            for path, fingerprint in before.items():
                restored = Path(tmp) / str(path).lstrip('/')
                if not restored.is_file():
                    return f'« {path.name} » manque dans la sauvegarde'
                if digest(restored) != fingerprint:
                    try:
                        changed = digest(path) != fingerprint      # modifié pendant la sauvegarde : pas une erreur
                    except OSError:
                        changed = True
                    if not changed:
                        return f'« {path.name} » ressort différent de l\'original'
                if restored.parent == Path(tmp) / str(self.staging).lstrip('/') and restored.suffix == '.sqlite3':
                    check = sqlite3.connect(f'file:{restored}?mode=ro', uri=True)
                    try:
                        if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            return f'la base « {path.name} » ressort abîmée'
                    except sqlite3.Error:
                        return f'la base « {path.name} » ressort illisible'
                    finally:
                        check.close()
        part = min(now.day, READ_CYCLE)
        code, _out, err = self._restic('check', f'--read-data-subset={part}/{READ_CYCLE}', timeout=3 * 3600)
        if code != 0:
            return 'restic a trouvé le dépôt abîmé (' + (err.strip().splitlines() or ['erreur'])[-1][:120] + ')'
        return None

    def snapshots(self, repository: Path | None = None, key_file: Path | None = None) -> list[dict[str, str]]:
        """Les dates gardées, de la plus ancienne à la plus récente : [{'id', 'quand'}]."""
        code, out, _err = self._restic('snapshots', '--json', '--tag', 'nexus', repository=repository,
                                       key_file=key_file, timeout=300)
        if code != 0 or not out.strip().startswith('['):
            return []
        found = [{'id': s['id'], 'quand': s['time'][:16]} for s in json.loads(out) if 'id' in s and 'time' in s]
        return sorted(found, key=lambda s: s['quand'])

    def verify_all(self) -> str | None:
        """Relit tout le dépôt (long). Renvoie le problème, ou None."""
        problem = self._problem()
        if problem is not None:
            return problem
        code, _out, err = self._restic('check', '--read-data', timeout=12 * 3600)
        return None if code == 0 else 'restic a trouvé le dépôt abîmé (' + (err.strip().splitlines() or ['erreur'])[-1][:160] + ')'

    # -- restauration ---------------------------------------------------------------------
    def restore(self, target: Path, *, source: Path | None = None, key: str | None = None,
                day: str | None = None) -> dict[str, Any]:
        """Ressort une sauvegarde dans un dossier vide, rangée en Documents / donnees / reglages : la
        dernière, ou celle d'un jour donné (`day`, AAAA-MM-JJ : la dernière faite ce jour-là ou avant).
        `source` et `key` : pour un PC neuf, qui ne connaît ni le disque ni la clé."""
        target = Path(target).expanduser().absolute()
        if target.exists() and any(target.iterdir()):
            raise ValueError('le dossier de restauration doit être vide : rien n\'est jamais écrasé')
        repository = (Path(source).expanduser().absolute() if source is not None else self.destination())
        if repository is None:
            raise ValueError('préciser où est la sauvegarde : --depuis /chemin/du/disque')
        repository = repository if repository.name == FOLDER else repository / FOLDER
        target.mkdir(parents=True, exist_ok=True)
        key_file = None
        try:
            if key is not None:
                descriptor, name = tempfile.mkstemp(prefix='cle-', dir=target.parent)
                with os.fdopen(descriptor, 'w', encoding='ascii') as handle:
                    handle.write(tidy_key(key) + '\n')
                key_file = Path(name)
            problem = self._problem(repository, key_file)
            if problem is not None:
                raise OllamaError(problem)
            snapshot = 'latest'
            if day is not None:
                older = [s for s in self.snapshots(repository, key_file) if s['quand'][:10] <= day]
                if not older:
                    raise ValueError(f'aucune sauvegarde du {day} ou d\'avant')
                snapshot = older[-1]['id']
            raw = target / '.brut'
            code, _out, err = self._restic('restore', snapshot, '--target', str(raw),
                                           repository=repository, key_file=key_file, timeout=12 * 3600)
            if code != 0:
                raise OllamaError('Restauration impossible : ' + (err.strip().splitlines() or ['erreur'])[-1][:200])
        finally:
            if key_file is not None:
                key_file.unlink(missing_ok=True)
        plans = sorted(raw.rglob('instantane/plan.json'))
        if not plans:
            raise OllamaError('Sauvegarde restaurée, mais son plan manque : les fichiers sont dans ' + str(raw))
        plan = json.loads(plans[0].read_text(encoding='utf-8'))
        placed = {}
        # Les données d'abord : si les documents sont rangés dedans, ils suivent.
        for label, key_name in (('donnees', 'donnees'), ('reglages', 'reglages'), ('Documents', 'documents')):
            original = plan.get(key_name)
            restored = raw / original.lstrip('/') if original else None
            if restored is not None and restored.is_dir():
                shutil.move(str(restored), str(target / label))
                placed[label] = original
        try:
            inside = self.staging.relative_to(self.data)
        except ValueError:
            inside = Path('sauvegarde') / 'instantane'
        staged = target / 'donnees' / inside if (target / 'donnees').is_dir() else None
        if staged is not None and staged.is_dir():       # les bases reprennent leur place et leur nom
            for name, original in plan.get('bases', {}).items():
                copy = staged / name
                if copy.is_file() and Path(original).parent == Path(plan['donnees']):
                    shutil.move(str(copy), str(target / 'donnees' / name))
            shutil.rmtree(staged.parent, ignore_errors=True)
        shutil.rmtree(raw, ignore_errors=True)
        return {'dossier': target, 'fait_le': plan.get('fait_le'), 'places': placed}

    def restore_in_place(self, *, source: Path | None = None, key: str | None = None,
                         day: str | None = None) -> dict[str, Any]:
        """PC neuf ou vidé : ressort la sauvegarde et remet chaque chose à sa place d'aujourd'hui (les
        dossiers de CE PC, même si le compte a changé de nom). Refuse s'il y a déjà des données :
        rien n'est jamais écrasé ; ce qui existe déjà est laissé et signalé."""
        present = [p.name for p in self.databases if p.is_file()]
        if present:
            raise ValueError('ce PC a déjà des données de NEXUS (' + ', '.join(present[:3]) + ') : restaurer plutôt dans '
                             'un dossier à part (nexus sauvegarde restaurer DOSSIER) et y prendre ce qu\'il faut')
        if self.documents is not None and self.documents.is_dir() and any(self.documents.iterdir()):
            raise ValueError(f'{self.documents} n\'est pas vide : restaurer plutôt dans un dossier à part '
                             '(nexus sauvegarde restaurer DOSSIER) et y prendre ce qu\'il faut')
        self.data.parent.mkdir(parents=True, exist_ok=True)
        holding = Path(tempfile.mkdtemp(prefix='nexus-restauration-', dir=self.data.parent))
        try:
            result = self.restore(holding / 'contenu', source=source, key=key, day=day)
            places = {'donnees': self.data, 'reglages': self.config, 'Documents': self.documents}
            moved, skipped = 0, []
            for label, destination in places.items():
                restored = holding / 'contenu' / label
                if destination is None or not restored.is_dir():
                    continue
                destination.mkdir(parents=True, exist_ok=True)
                for child in sorted(restored.iterdir()):
                    if (destination / child.name).exists():
                        skipped.append(str(destination / child.name))      # déjà là : on n'y touche pas
                    else:
                        shutil.move(str(child), str(destination / child.name))
                        moved += 1
        finally:
            shutil.rmtree(holding, ignore_errors=True)
        return {'fait_le': result['fait_le'], 'places': {k: v for k, v in places.items() if v is not None},
                'remis': moved, 'laisses': skipped}

    # -- affichage ------------------------------------------------------------------------
    def describe(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        destination = self.destination()
        if destination is None:
            return ['Sauvegarde : pas réglée (nexus sauvegarde installer /chemin/du/disque). '
                    'Une panne de disque emporterait tout.']
        state = self.state()
        lines = [f'Sauvegarde : sur {destination}, chiffrée, chaque nuit.']
        if self.same_disk():
            lines.append('  ATTENTION : c\'est le même disque que tes documents. Elle ne protège pas d\'une panne de ce disque.')
        last = state.get('derniere_reussie')
        if last:
            age = (now - datetime.fromisoformat(last)).days
            when = 'aujourd\'hui' if age <= 0 else 'hier' if age == 1 else f'il y a {age} jours'
            lines.append(f"  dernière réussie : {when} ({last.replace('T', ' à ')}), {state.get('fichiers', 0)} fichiers, "
                         f"{state.get('dates', 0)} copie(s) gardée(s), {human(state.get('taille', 0))} sur le disque ; "
                         f"restauration prouvée sur {state.get('preuve', 0)} fichier(s).")
        else:
            lines.append('  aucune sauvegarde réussie pour l\'instant (nexus sauvegarde maintenant).')
        if state.get('erreur'):
            lines.append(f"  dernier essai ({str(state.get('derniere_tentative', '')).replace('T', ' à ')}) : {state['erreur']}")
        return lines

    def describe_dates(self) -> list[str]:
        dates = self.snapshots()
        if not dates:
            return []
        days = sorted({s['quand'][:10] for s in dates})
        return ['  dates qu\'on peut ressortir (nexus sauvegarde restaurer DOSSIER --date AAAA-MM-JJ) :',
                '    ' + ', '.join(days[-40:])]
