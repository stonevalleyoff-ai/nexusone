"""Le moteur colibrì, tenu par NEXUS lui-même : rien à lancer à la main.

colibrì fait tourner un gros modèle sur un petit PC. C'est un programme séparé (du C, avec
sa passerelle) ; NEXUS s'en occupe entièrement :

- `nexus moteur installer` le télécharge (à une version précise, relue), le compile et
  récupère le modèle adapté à la machine ;
- à la première question, NEXUS le démarre, attend qu'il soit prêt, puis lui parle par une
  adresse locale et une clé tirées au hasard, connues de lui seul ;
- un petit surveillant (ce module, lancé en tâche de fond) l'arrête après un temps sans
  question, à la fermeture de NEXUS, ou sur `nexus moteur arreter`.

Tout vit dans un seul dossier (`~/.local/share/nexus/moteur`) : le code de colibrì, le
modèle, les réglages, le journal.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import fcntl
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable

from .ollama import OllamaError

SOURCE = 'https://github.com/JustVugg/colibri'
# Version de colibrì relue et essayée avec NEXUS. On n'installe jamais « la dernière ».
PINNED_COMMIT = 'e479104ae01bdc40ce81e675299b1d230891b321'
SYSTEM_BASE = Path('/opt/nexus/moteur')     # installation posée d'avance (image, mini-PC)
DEFAULT_IDLE_MINUTES = 30
START_TIMEOUT = 1800.0                      # un gros modèle peut mettre des minutes à charger
RETRY_DELAY = 60.0                          # après un démarrage raté, pas de relance en boucle


@dataclass(frozen=True)
class Install:
    launcher: Path          # …/colibri/c/coli
    model_dir: Path
    model_id: str
    settings_home: Path     # dossier des réglages de colibrì (COLI_SETUP_HOME)
    env: tuple[tuple[str, str], ...] = ()


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:    # un processus terminé mais pas encore « ramassé » n'est plus vivant
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except (OSError, IndexError):
        return True


def _is_supervisor(pid: object) -> bool:
    """Le numéro noté est-il encore celui de notre surveillant (et pas d'un autre programme) ?"""
    if not _pid_alive(pid):
        return False
    try:
        return b'nexus.engine' in Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return True    # pas de /proc lisible : on fait confiance au numéro


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _die_with_parent() -> None:
    """Dans le processus fils : mourir si le surveillant meurt (Linux)."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def _healthy(port: int, key: str, timeout: float = 2.0) -> bool:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request('GET', '/health', headers={'Authorization': 'Bearer ' + key})
        resp = conn.getresponse()
        resp.read(4096)
        return resp.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


class Engine:
    def __init__(self, base: Path, *, colibri_dir: Path | None = None, model_dir: Path | None = None,
                 idle_minutes: float = DEFAULT_IDLE_MINUTES, system_base: Path = SYSTEM_BASE):
        self.base = Path(base)
        self.colibri_dir = colibri_dir
        self.model_dir = model_dir
        self.idle_minutes = idle_minutes
        self.system_base = system_base
        self.state_path = self.base / 'etat.json'
        self.log_path = self.base / 'moteur.log'
        self.activity_path = self.base / 'activite'
        self.stop_path = self.base / 'arret'
        self._mutex = threading.Lock()
        self._failed: tuple[float, str] | None = None
        self.started_here = False    # ce processus a-t-il démarré le moteur ?

    # -- installation ----------------------------------------------------------------
    def installed(self) -> Install | None:
        """Ce qui est installé et utilisable, ou None."""
        if self.colibri_dir is not None and self.model_dir is not None:   # réglage explicite
            launcher = Path(self.colibri_dir) / 'c' / 'coli'
            if launcher.is_file() and Path(self.model_dir).is_dir():
                return Install(launcher, Path(self.model_dir), Path(self.model_dir).name,
                               self.base / 'reglages')
            return None
        for base in (self.base, self.system_base):
            home = base / 'reglages'
            try:
                data = json.loads((home / 'setup.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get('status') != 'ready':
                continue
            launcher, model_dir = Path(str(data.get('launcher', ''))), Path(str(data.get('model_dir', '')))
            if not (launcher.is_file() and model_dir.is_dir()):
                continue
            model = data.get('model') if isinstance(data.get('model'), dict) else {}
            env = data.get('env') if isinstance(data.get('env'), dict) else {}
            return Install(launcher, model_dir, str(model.get('id') or model_dir.name), home,
                           tuple((str(k), str(v)) for k, v in env.items()))
        return None

    def install(self, model: str | None = None, *, source: str = SOURCE,
                say: Callable[[str], None] = print) -> Install:
        """Télécharge colibrì à la version relue, le compile, récupère le modèle."""
        for tool, package in (('git', 'git'), ('make', 'build-essential'), ('cc', 'build-essential')):
            if shutil.which(tool) is None:
                raise OllamaError(f'Il manque « {tool} » pour installer le moteur : sudo apt install {package}')
        self.base.mkdir(parents=True, exist_ok=True)
        if self.running():
            raise OllamaError('Le moteur tourne : nexus moteur arreter, puis recommencer')
        code = self.base / 'colibri'

        def git(*args: str) -> str:
            done = subprocess.run(['git', '-C', str(code), *args], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=1800)
            if done.returncode != 0:
                raise OllamaError('Téléchargement de colibrì impossible (réseau ?) : '
                                  + (done.stderr.strip().splitlines() or ['erreur git'])[-1][:200])
            return done.stdout.strip()

        if not (code / '.git').is_dir():
            code.mkdir(parents=True, exist_ok=True)
            git('init', '-q')
        if not (code / 'c' / 'coli').is_file() or git('rev-parse', 'HEAD') != PINNED_COMMIT:
            say(f'Téléchargement de colibrì (version {PINNED_COMMIT[:7]})…')
            git('fetch', '-q', '--depth', '1', source, PINNED_COMMIT)
            git('checkout', '-q', '--detach', 'FETCH_HEAD')
            if git('rev-parse', 'HEAD') != PINNED_COMMIT:
                raise OllamaError('La version téléchargée de colibrì n\'est pas celle attendue')
        home = self.base / 'reglages'
        command = [sys.executable, str(code / 'c' / 'coli'), 'setup', '--yes', '--no-start', '--no-browser',
                   '--dir', str(self.base / 'modeles')]
        if model:
            command += ['--model', model]
        say('Préparation du moteur et du modèle (compilation, puis plusieurs Go à télécharger)…')
        env = dict(os.environ, COLI_SETUP_HOME=str(home))
        try:
            done = subprocess.run(command, cwd=code / 'c', env=env, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise OllamaError(f'Installation du moteur impossible : {exc}') from exc
        found = self.installed()
        if done.returncode != 0 or found is None:
            raise OllamaError('L\'installation du moteur n\'a pas abouti (voir les messages ci-dessus) ; '
                              'la relancer reprend où elle s\'est arrêtée')
        return found

    # -- état --------------------------------------------------------------------------
    def state(self) -> dict | None:
        """L'état écrit par le surveillant, s'il est toujours en vie."""
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if (isinstance(data, dict) and isinstance(data.get('port'), int) and isinstance(data.get('cle'), str)
                and _is_supervisor(data.get('superviseur'))):
            return data
        return None

    def running(self) -> bool:
        return self.state() is not None

    def describe(self) -> list[str]:
        found, state = self.installed(), self.state()
        if found is None:
            return ['Moteur colibrì : pas installé (nexus moteur installer)']
        lines = [f'Moteur colibrì : installé, modèle {found.model_id}']
        if state is None:
            lines.append('  arrêté : il démarre tout seul à la première question')
        elif _healthy(state['port'], state['cle']):
            minutes = int((time.time() - float(state.get('depuis', time.time()))) // 60)
            lines.append(f'  en marche depuis {minutes} min')
        else:
            lines.append('  en cours de démarrage (chargement du modèle)')
        if self.idle_minutes:
            lines.append(f'  s\'arrête après {self.idle_minutes:g} min sans question')
        return lines

    def log_tail(self, lines: int = 12) -> list[str]:
        try:
            with self.log_path.open('rb') as handle:
                handle.seek(max(0, self.log_path.stat().st_size - 8000))
                text = handle.read().decode('utf-8', errors='replace')
        except OSError:
            return []
        return [line[:300] for line in text.splitlines() if line.strip()][-lines:]

    def touch(self) -> None:
        """Signale au surveillant que le moteur sert encore."""
        try:
            self.activity_path.touch()
        except OSError:
            pass

    # -- démarrage et arrêt ------------------------------------------------------------
    def ensure(self, progress: Callable[[str], None] | None = None,
               timeout: float = START_TIMEOUT) -> tuple[str, str]:
        """Démarre le moteur s'il ne tourne pas ; renvoie (adresse, clé) une fois prêt."""
        progress = progress or (lambda _text: None)
        with self._mutex:
            state = self.state()
            if state is not None and _healthy(state['port'], state['cle']):
                self.touch()
                return f"http://127.0.0.1:{state['port']}", state['cle']
            if self._failed and time.monotonic() - self._failed[0] < RETRY_DELAY:
                raise OllamaError(self._failed[1])
            found = self.installed()
            if found is None:
                raise OllamaError('Le moteur colibrì n\'est pas installé : nexus moteur installer')
            self.base.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            with (self.base / 'verrou').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)    # un seul démarrage à la fois, tous programmes confondus
                if self.state() is None:
                    progress(f'Démarrage du moteur ({found.model_id})…')   # affiché tout de suite
                    self._spawn(found)
                    self.started_here = True
            while True:
                waited = time.monotonic() - started
                state = self.state()
                if state is not None and _healthy(state['port'], state['cle']):
                    self._failed = None
                    self.touch()
                    progress('')    # fin de l'attente : l'interface efface son message
                    return f"http://127.0.0.1:{state['port']}", state['cle']
                if state is None and waited > 5:
                    tail = self.log_tail(3)
                    message = ('Le moteur colibrì n\'a pas pu démarrer'
                               + (f' ({tail[-1][:160]})' if tail else '') + ' — détails : nexus moteur journal')
                    self._failed = (time.monotonic(), message)
                    raise OllamaError(message)
                if waited > timeout:
                    self.stop()
                    message = 'Le moteur colibrì met trop de temps à démarrer (nexus moteur journal)'
                    self._failed = (time.monotonic(), message)
                    raise OllamaError(message)
                progress(f'Démarrage du moteur ({found.model_id}), chargement du modèle… {int(waited)} s')
                time.sleep(1.0)

    def _spawn(self, found: Install) -> None:
        for stale in (self.state_path, self.stop_path):
            stale.unlink(missing_ok=True)
        self.touch()
        package_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        watcher = subprocess.Popen(
            [sys.executable, '-m', 'nexus.engine', str(self.base), str(found.launcher), str(found.model_dir),
             str(found.settings_home), str(self.idle_minutes), json.dumps(dict(found.env))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=env, cwd='/')
        # Le surveillant vit sa vie ; on le « ramasse » seulement quand il se termine.
        threading.Thread(name='nexus-moteur-fin', target=watcher.wait, daemon=True).start()
        # On garde le verrou jusqu'à ce qu'il se soit annoncé : sinon un autre programme NEXUS,
        # ne voyant encore rien, en démarrerait un second.
        deadline = time.monotonic() + 15
        while self.state() is None and watcher.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop(self, timeout: float = 30.0) -> bool:
        """Demande l'arrêt et attend ; vrai si un moteur tournait."""
        state = self.state()
        if state is None:
            return False
        try:
            self.stop_path.touch()
            os.kill(state['superviseur'], signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _pid_alive(state['superviseur']):
            time.sleep(0.1)
        self.started_here = False
        return True

    def release(self) -> None:
        """À la fermeture du programme : arrête le moteur si c'est lui qui l'a démarré."""
        if self.started_here:
            self.stop()


# -- le surveillant (processus de fond) ----------------------------------------------------
def supervise(base: Path, launcher: Path, model_dir: Path, settings_home: Path, idle_minutes: float,
              extra_env: dict[str, str], poll: float = 1.0) -> int:
    base.mkdir(parents=True, exist_ok=True)
    state_path, activity, stop_file = base / 'etat.json', base / 'activite', base / 'arret'
    port, key = _free_port(), secrets.token_urlsafe(32)
    env = {**os.environ, **extra_env, 'COLI_API_KEY': key, 'COLI_SETUP_HOME': str(settings_home),
           'PYTHONUNBUFFERED': '1'}
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: stopping.set())
    with (base / 'moteur.log').open('wb') as log:
        child = subprocess.Popen(
            [sys.executable, str(launcher), 'serve', '--model', str(model_dir),
             '--host', '127.0.0.1', '--port', str(port), '--auto-tier'],
            cwd=launcher.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, preexec_fn=_die_with_parent)
        temporary = state_path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # la clé reste privée
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump({'superviseur': os.getpid(), 'moteur': child.pid, 'port': port, 'cle': key,
                       'modele': model_dir.name, 'depuis': time.time()}, handle)
        os.replace(temporary, state_path)
        activity.touch()
        while child.poll() is None and not stopping.is_set() and not stop_file.exists():
            if idle_minutes > 0:
                try:
                    if time.time() - activity.stat().st_mtime > idle_minutes * 60:
                        break
                except OSError:
                    activity.touch()
            stopping.wait(poll)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)
        for path in (state_path, stop_file):
            path.unlink(missing_ok=True)
    try:    # le programme C est un petit-fils : on termine tout notre groupe, nous compris
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    _base, _launcher, _model, _home, _idle, _env = sys.argv[1:7]
    sys.exit(supervise(Path(_base), Path(_launcher), Path(_model), Path(_home), float(_idle),
                       {str(k): str(v) for k, v in json.loads(_env).items()}))
