"""Les mises à jour de NEXUS : annoncées, approuvées par toi, essayées avant, et réversibles.

Celui qui peut pousser du code vers le PC de la maison contrôle tes documents et ton courrier.
Donc pas de mise à jour silencieuse :

- NEXUS regarde chaque nuit si une version plus récente existe dans SON dépôt (lecture seule)
  et l'annonce ; il n'installe rien de lui-même ;
- tu approuves sur le PC (`nexus mise-a-jour installer`), en voyant le numéro de version et
  l'empreinte exacte du code ; c'est cette empreinte-là qui est installée, pas « la dernière » ;
- une étiquette de version qui change de cible après avoir été annoncée est refusée : une
  version publiée ne se réécrit pas ;
- la nouvelle version est posée À CÔTÉ de l'ancienne, ses tests passent avant qu'elle prenne
  la main, les bases de données sont photographiées, puis la bascule tient en un lien ;
- le service redémarre de lui-même sur la nouvelle version ; s'il ne tient pas debout, NEXUS
  revient seul à la précédente. `nexus mise-a-jour annuler` y revient aussi à la main.

Le service enfermé ne peut pas écrire son propre code : c'est cette commande, lancée par toi
hors de la cage, qui fait la mise à jour. Aucun droit d'administrateur n'est nécessaire.
"""
from __future__ import annotations

from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable

from .ollama import OllamaError

SOURCE = 'https://github.com/stonevalleyoff-ai/nexusone'
TAG_RE = re.compile(r'v(\d{1,4})\.(\d{1,4})\.(\d{1,4})')
COMMIT_RE = re.compile(r'[0-9a-f]{40}')
VERSION_RE = re.compile(r"^__version__ = '(\d{1,4}\.\d{1,4}\.\d{1,4})'$", re.M)
URL_RE = re.compile(r'(https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+|/[^\s:]+|file:///[^\s]+)')
TOOLS_PATH = '/usr/bin:/bin:/usr/local/bin'
HANDOVER = 40                # secondes laissées à l'ancien serveur pour laisser la main de lui-même
START_TIMEOUT = 90           # secondes laissées à la nouvelle version pour tenir debout
STEADY = 15                  # secondes pendant lesquelles elle doit rester en marche
MAX_FILES, MAX_BYTES = 5000, 200 * 2**20

Version = tuple[int, int, int]


def parse_version(text: str) -> Version | None:
    found = TAG_RE.fullmatch(text if text.startswith('v') else 'v' + text)
    return tuple(int(part) for part in found.groups()) if found else None      # type: ignore[return-value]


def dotted(version: Version) -> str:
    return '.'.join(map(str, version))


def clean(text: str, limit: int = 1500) -> str:
    """Un texte venu du dépôt (notes de version), ramené à du texte simple."""
    kept = ''.join(ch for ch in text if ch == '\n' or ch.isprintable())
    return '\n'.join(line.rstrip() for line in kept.strip().splitlines())[:limit]


class Updater:
    def __init__(self, app: Path, data: Path, *, source: str = SOURCE, code: Path | None = None,
                 python: str | None = None, databases: list[Path] | None = None):
        self.app, self.data = Path(app), Path(data)
        if not URL_RE.fullmatch(source):
            raise ValueError('adresse du dépôt refusée')
        self.source = source
        self.code = Path(code) if code is not None else Path(__file__).resolve().parent.parent      # le code qui tourne
        self.python = python or sys.executable or '/usr/bin/python3'
        self.databases = [Path(p) for p in (databases or [])]
        self.depot, self.versions = self.app / 'depot.git', self.app / 'versions'
        self.current, self.previous = self.app / 'actuelle', self.app / 'precedente'
        self.state_path = self.data / 'mise-a-jour.json'
        self.running_path = self.data / 'service' / 'en-marche.json'

    # -- où en est-on ---------------------------------------------------------------------
    def managed(self) -> bool:
        """Vrai si le code qui tourne est une version posée par ce mécanisme (et non un dossier de travail)."""
        try:
            return self.code.resolve().parent == self.versions.resolve()
        except OSError:
            return False

    @staticmethod
    def version_of(folder: Path) -> str | None:
        """Le numéro de version écrit dans un dossier de code, lu sans l'exécuter."""
        try:
            found = VERSION_RE.search((folder / 'nexus' / '__init__.py').read_text(encoding='utf-8'))
        except OSError:
            return None
        return found.group(1) if found else None

    def installed(self) -> dict[str, Any]:
        """La version en place : {'version', 'commit', 'dossier'} ; commit None hors du mécanisme."""
        folder = self.current.resolve() if self.current.exists() else self.code
        try:
            mark = json.loads((folder / '.nexus-version').read_text(encoding='utf-8'))
            commit = mark['commit'] if COMMIT_RE.fullmatch(str(mark.get('commit'))) else None
        except (OSError, ValueError, KeyError, TypeError):
            commit = None
        return {'version': self.version_of(folder), 'commit': commit, 'dossier': folder}

    def state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, **changes: Any) -> dict[str, Any]:
        state = {**self.state(), **changes}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temporary, self.state_path)
        return state

    # -- git, arguments figés, jamais de shell --------------------------------------------------
    def _git(self, *argv: str, timeout: int = 300, binary: bool = False) -> tuple[int, Any, str]:
        program = shutil.which('git', path=TOOLS_PATH)
        if program is None:
            raise OllamaError('Il manque « git » pour les mises à jour : sudo apt install git')
        env = {'PATH': TOOLS_PATH, 'HOME': str(self.app), 'LANG': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0',
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_ASKPASS': '/bin/false'}
        for name in ('HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy', 'SSL_CERT_FILE', 'GIT_SSL_CAINFO', 'CURL_CA_BUNDLE'):
            if os.environ.get(name):                    # passer par le mandataire de la maison, s'il y en a un
                env[name] = os.environ[name]
        try:
            done = subprocess.run([program, '-c', 'core.hooksPath=/dev/null', '-c', 'protocol.ext.allow=never', *argv],
                                  env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return 124, b'' if binary else '', 'délai dépassé'
        except OSError as exc:
            return 127, b'' if binary else '', type(exc).__name__
        return (done.returncode, done.stdout if binary else done.stdout.decode('utf-8', 'replace'),
                done.stderr.decode('utf-8', 'replace'))

    def remote_versions(self) -> dict[Version, dict[str, str]]:
        """Les versions publiées dans le dépôt : {(0, 28, 1): {'etiquette': 'v0.28.1', 'commit': '…'}}. Lecture seule."""
        code, out, err = self._git('ls-remote', '--tags', self.source, 'v*', timeout=60)
        if code != 0:
            reason = next((line for line in err.splitlines() if line.startswith(('fatal:', 'error:'))), err.strip()[-160:])
            raise OllamaError('Le dépôt des versions est injoignable : ' + (reason.strip() or 'erreur')[:160])
        found: dict[Version, dict[str, str]] = {}
        for line in out.splitlines():
            commit, _, ref = line.partition('\t')
            peeled = ref.endswith('^{}')                 # étiquette annotée : la ligne « ^{} » donne le vrai commit
            tag = ref.removeprefix('refs/tags/').removesuffix('^{}')
            version = parse_version(tag) if TAG_RE.fullmatch(tag) else None
            if version is None or not COMMIT_RE.fullmatch(commit):
                continue
            if peeled or version not in found:
                found[version] = {'etiquette': tag, 'commit': commit}
        return found

    # -- l'annonce de la nuit -----------------------------------------------------------------
    def check(self, now: datetime | None = None) -> dict[str, Any]:
        """Regarde si une version plus récente existe et le retient. N'installe rien.
        Une étiquette déjà vue qui désigne maintenant un autre code est signalée, et refusée."""
        now = now or datetime.now()
        stamp = now.isoformat(timespec='minutes')
        try:
            remote = self.remote_versions()
        except OllamaError as exc:
            self._remember(verifie_le=stamp, erreur=str(exc))
            return {'ok': False, 'message': str(exc)}
        seen = dict(self.state().get('vues') or {})
        moved = sorted(v['etiquette'] for v in remote.values()
                       if seen.get(v['etiquette']) not in (None, v['commit']))
        for value in remote.values():
            seen.setdefault(value['etiquette'], value['commit'])      # la première cible vue fait foi
        current = parse_version(self.installed()['version'] or '') or (0, 0, 0)
        refused = (self.state().get('refusee') or {}).get('commit')        # déjà essayée ici, et elle n'a pas tenu
        newer = sorted(v for v in remote if v > current and remote[v]['commit'] != refused)
        latest = newer[-1] if newer else None
        self._remember(verifie_le=stamp, erreur=None, vues=seen, deplacees=moved,
                       disponible=dotted(latest) if latest else None,
                       commit=seen[remote[latest]['etiquette']] if latest else None)
        return {'ok': True, 'actuelle': dotted(current), 'disponible': dotted(latest) if latest else None,
                'deplacees': moved}

    # -- installation -----------------------------------------------------------------------
    def fetch(self, tag: str, commit: str) -> str:
        """Récupère le code d'une version dans le dépôt local et vérifie que c'est bien l'empreinte attendue.
        Renvoie les notes de la version (texte de l'étiquette)."""
        self.app.mkdir(parents=True, exist_ok=True)
        if not (self.depot / 'HEAD').is_file():
            code, _out, err = self._git('init', '-q', '--bare', str(self.depot))
            if code != 0:
                raise OllamaError('Préparation du dépôt local impossible : ' + err.strip()[-160:])
        code, _out, err = self._git('--git-dir', str(self.depot), 'fetch', '-q', '--no-tags', '--force', self.source,
                                    f'refs/tags/{tag}:refs/tags/{tag}', timeout=1800)
        if code != 0:
            raise OllamaError('Téléchargement de la version impossible : ' + (err.strip().splitlines() or ['erreur'])[-1][:160])
        code, out, _err = self._git('--git-dir', str(self.depot), 'rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}')
        if code != 0 or out.strip() != commit:
            raise OllamaError(f'Le code reçu pour {tag} n\'a pas l\'empreinte annoncée : installation refusée.')
        code, out, _err = self._git('--git-dir', str(self.depot), 'tag', '-l', '--format=%(contents)', tag)
        return clean(out) if code == 0 else ''

    def export(self, version: str, commit: str) -> Path:
        """Pose le code de cette empreinte dans son propre dossier, à côté des autres versions."""
        target = self.versions / f'{version}-{commit[:12]}'
        if (target / '.nexus-version').is_file():
            return target                               # déjà posée (mise à jour reprise)
        code, archive, err = self._git('--git-dir', str(self.depot), 'archive', '--format=tar', commit, binary=True,
                                       timeout=600)
        if code != 0:
            raise OllamaError('Extraction de la version impossible : ' + err.strip()[-160:])
        self.versions.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.pose-', dir=self.versions))
        try:
            total = count = 0
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                for member in tar:
                    name = Path(member.name)
                    if member.name == 'pax_global_header' or member.type == tarfile.XGLTYPE:
                        continue
                    # Rien que des fichiers et des dossiers, à l'intérieur du dossier : ni lien, ni chemin qui s'échappe.
                    if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                        raise OllamaError(f'La version contient une entrée inattendue ({member.name[:80]}) : refusée.')
                    count += 1
                    total += member.size
                    if count > MAX_FILES or total > MAX_BYTES:
                        raise OllamaError('La version est anormalement grosse : refusée.')
                    destination = staging / name
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    with destination.open('wb') as out:
                        shutil.copyfileobj(source, out)
                    destination.chmod(0o755 if member.mode & 0o100 else 0o644)
            if self.version_of(staging) != version:
                raise OllamaError(f'Le code reçu ne se dit pas en version {version} : refusé.')
            (staging / '.nexus-version').write_text(json.dumps({'version': version, 'commit': commit}), encoding='utf-8')
            staging.chmod(0o755)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return target

    def test(self, folder: Path, timeout: int = 2400) -> tuple[bool, str]:
        """Fait passer à la nouvelle version ses propres tests, dans un dossier personnel vide (jamais sur tes données)."""
        with tempfile.TemporaryDirectory(prefix='nexus-essai-') as home:
            env = {'PATH': os.environ.get('PATH', TOOLS_PATH), 'HOME': home, 'LANG': os.environ.get('LANG', 'C.UTF-8'),
                   'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': home}
            try:
                done = subprocess.run([self.python, '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.'], cwd=folder,
                                      env=env, capture_output=True, text=True, errors='replace',
                                      stdin=subprocess.DEVNULL, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                return False, 'les tests n\'ont pas fini à temps'
            except OSError as exc:
                return False, f'les tests n\'ont pas pu être lancés ({type(exc).__name__})'
        tail = [line for line in done.stderr.strip().splitlines() if line.strip()]
        counted = next((line for line in reversed(tail) if line.startswith('Ran ')), '')
        summary = ' · '.join(filter(None, [counted, tail[-1] if tail else 'aucune sortie']))[-300:]
        failed = [line.split(' ', 1)[1][:120] for line in tail if line.startswith(('FAIL: ', 'ERROR: '))][:5]
        return done.returncode == 0, summary + (' · ' + ' ; '.join(failed) if failed else '')

    def snapshot(self, label: str) -> Path | None:
        """Photographie les bases de données avant la bascule : de quoi revenir en arrière même si la
        nouvelle version les a transformées."""
        from .backup import copy_databases
        target = self.data / 'avant-mise-a-jour' / label
        if target.exists():
            return target
        target.mkdir(parents=True)
        return target if copy_databases(self.databases, target) else None

    def _point(self, link: Path, folder: Path) -> None:
        """Fait désigner `folder` par le lien, d'un seul geste (jamais d'instant sans version)."""
        temporary = link.with_name(link.name + '.nouveau')
        temporary.unlink(missing_ok=True)
        os.symlink(Path('versions') / folder.name, temporary)       # lien relatif : il reste juste vu de la cage
        os.replace(temporary, link)

    def switch(self, folder: Path) -> None:
        if self.current.is_symlink() and self.current.resolve() != folder.resolve():
            self._point(self.previous, self.current.resolve())
        self._point(self.current, folder)

    def rollback(self) -> dict[str, Any]:
        """Revient à la version précédente (elle est restée sur le disque)."""
        if not self.previous.is_symlink() or not self.previous.resolve().is_dir():
            raise OllamaError('Aucune version précédente à reprendre.')
        back, leaving = self.previous.resolve(), self.current.resolve()
        self._point(self.current, back)
        self._point(self.previous, leaving)
        return {'version': self.version_of(back), 'dossier': back}

    def tidy(self) -> None:
        """Ne garde que la version en place et la précédente."""
        keep = {link.resolve() for link in (self.current, self.previous) if link.is_symlink()}
        for folder in self.versions.iterdir() if self.versions.is_dir() else []:
            if folder.is_dir() and folder.resolve() not in keep:
                shutil.rmtree(folder, ignore_errors=True)

    # -- le service qui tourne --------------------------------------------------------------
    def running(self) -> dict[str, Any] | None:
        """Le serveur en marche, d'après ce qu'il a noté en démarrant : {'version', 'pid', 'code'} ou None."""
        try:
            mark = json.loads(self.running_path.read_text(encoding='utf-8'))
            os.kill(int(mark['pid']), 0)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return mark

    def announce_running(self) -> None:
        """Appelé par le serveur à son démarrage."""
        self.running_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.running_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': self.version_of(self.code), 'pid': os.getpid(),
                                         'code': str(self.code.resolve())}), encoding='utf-8')
        os.replace(temporary, self.running_path)

    def outdated(self) -> bool:
        """Vrai si le serveur qui tourne n'est plus la version en place : il doit laisser la main."""
        try:
            return self.managed() and self.current.is_symlink() and self.current.resolve() != self.code.resolve()
        except OSError:
            return False

    def _wait_running(self, folder: Path, timeout: float, sleep: Callable[[float], None] = time.sleep) -> bool:
        """Attend que le serveur tourne depuis ce dossier, et qu'il y reste un moment."""
        end, since = time.monotonic() + timeout, None
        while time.monotonic() < end:
            mark = self.running()
            if mark is not None and mark.get('code') == str(folder.resolve()):
                since = since or time.monotonic()
                if time.monotonic() - since >= STEADY:
                    return True
            else:
                since = None
            sleep(1)
        return False

    def _nudge(self, folder: Path) -> None:
        """Si l'ancien serveur ne laisse pas la main de lui-même, on le lui demande (même compte, pas de sudo)."""
        mark = self.running()
        if mark is not None and mark.get('code') != str(folder.resolve()):
            try:
                os.kill(int(mark['pid']), signal.SIGINT)
            except (OSError, ValueError):
                pass

    # -- la mise à jour, de bout en bout -----------------------------------------------------
    def plan(self, wanted: str | None = None) -> dict[str, Any]:
        """Ce qui serait installé : {'version', 'etiquette', 'commit'}. Refuse une étiquette déplacée."""
        remote = self.remote_versions()
        if not remote:
            raise OllamaError('Aucune version publiée dans le dépôt.')
        if wanted is not None:
            version = parse_version(wanted)
            if version is None or version not in remote:
                raise ValueError('version inconnue ; publiées : ' + ', '.join(dotted(v) for v in sorted(remote)[-8:]))
        else:       # la plus récente, sauf celle qui a déjà échoué ici (la demander par son numéro reste possible)
            refused = (self.state().get('refusee') or {}).get('commit')
            usable = [v for v in remote if remote[v]['commit'] != refused]
            if not usable:
                raise OllamaError('La seule version publiée est celle qui n\'a pas tenu en marche ici.')
            version = max(usable)
        entry = remote[version]
        seen = (self.state().get('vues') or {}).get(entry['etiquette'])
        if seen is not None and seen != entry['commit']:
            raise OllamaError(f"L'étiquette {entry['etiquette']} ne désigne plus le code annoncé ({seen[:12]} → "
                              f"{entry['commit'][:12]}). Une version publiée ne doit jamais changer : n'installe rien "
                              'et vérifie qui a accès à ton dépôt.')
        return {'version': dotted(version), 'etiquette': entry['etiquette'], 'commit': entry['commit']}

    def install(self, plan: dict[str, Any], *, say: Callable[[str], None] = print, skip_tests: bool = False,
                backup: Any = None, wait: bool = True) -> dict[str, Any]:
        """Installe exactement la version du plan approuvé. Renvoie {'ok', 'message', …}."""
        version, tag, commit = plan['version'], plan['etiquette'], plan['commit']
        before = self.installed()
        say(f'Téléchargement de NEXUS {version}…')
        notes = self.fetch(tag, commit)
        folder = self.export(version, commit)
        if not skip_tests:
            say('La nouvelle version passe ses tests (quelques minutes)…')
            passed, summary = self.test(folder)
            if not passed:
                return {'ok': False, 'etape': 'tests', 'message': 'La nouvelle version échoue à ses propres tests ; '
                        'rien n\'a été changé. ' + summary}
            say('  tests réussis : ' + summary.split(' · ')[0])
        if backup is not None and backup.configured():
            say('Sauvegarde avant la mise à jour…')
            saved = backup.run()
            if not saved['ok']:
                return {'ok': False, 'etape': 'sauvegarde', 'message': 'La sauvegarde a échoué ; rien n\'a été changé. '
                        + saved['message']}
        self.snapshot(f"{before['version'] or 'ancienne'}-vers-{version}")
        was_running = self.running()
        old_previous = self.previous.resolve() if self.previous.is_symlink() else None
        self.switch(folder)
        self._remember(vues={**(self.state().get('vues') or {}), tag: commit}, disponible=None, commit=None,
                       installee_le=datetime.now().isoformat(timespec='minutes'))
        result = {'ok': True, 'version': version, 'commit': commit, 'notes': notes, 'dossier': folder,
                  'redemarre': False, 'message': f'NEXUS {version} est en place.'}
        if was_running is None or not wait:
            self.tidy()
            return result                               # pas de serveur en marche : rien à surveiller
        say('Le service passe à la nouvelle version…')
        if not self._wait_running(folder, HANDOVER):
            self._nudge(folder)
        if self._wait_running(folder, START_TIMEOUT):
            self.tidy()
            return {**result, 'redemarre': True}
        # La nouvelle version ne tient pas debout : on revient à celle d'avant, toute seule. Et la version
        # fautive ne devient pas « la précédente » : `annuler` ne doit jamais y ramener.
        back = self.rollback()
        if old_previous is not None and old_previous.is_dir():
            self._point(self.previous, old_previous)
        else:
            self.previous.unlink(missing_ok=True)
        self._remember(refusee={'version': version, 'commit': commit, 'raison': 'ne tient pas en marche'})
        self._nudge(back['dossier'])
        recovered = self._wait_running(back['dossier'], START_TIMEOUT)
        return {'ok': False, 'etape': 'demarrage', 'version': version,
                'message': f"NEXUS {version} n'a pas tenu en marche : retour à la version {back['version']}"
                           + (', qui tourne de nouveau.' if recovered else
                              ', mais le service n\'est pas reparti : voir nexus service journal.')}

    def describe(self) -> list[str]:
        installed, state = self.installed(), self.state()
        where = (f"empreinte {installed['commit'][:12]}" if installed['commit'] else 'dossier de travail, hors mises à jour')
        lines = [f"NEXUS {installed['version'] or '?'} ({where}). Versions officielles : {self.source}"]
        if not self.managed() and not self.current.is_symlink():
            lines.append('  Pour passer aux versions officielles, avec retour arrière : nexus mise-a-jour installer')
        if state.get('deplacees'):
            lines.append('  ALERTE : des étiquettes de version ont changé de cible (' + ', '.join(state['deplacees'])
                         + ') : n\'installe rien et vérifie qui a accès à ton dépôt.')
        if state.get('disponible'):
            lines.append(f"  Version {state['disponible']} disponible : nexus mise-a-jour installer")
        elif state.get('verifie_le') and not state.get('erreur'):
            lines.append(f"  À jour (vérifié le {str(state['verifie_le']).replace('T', ' à ')}).")
        if state.get('erreur'):
            lines.append(f"  Dernière vérification impossible : {state['erreur']}")
        if state.get('refusee'):
            lines.append(f"  Version {state['refusee'].get('version')} essayée et refusée : elle {state['refusee'].get('raison')}. "
                         'Elle ne sera plus proposée ; attendre la suivante.')
        if self.previous.is_symlink():
            lines.append(f"  Version précédente gardée : {self.version_of(self.previous.resolve())} (nexus mise-a-jour annuler)")
        return lines
