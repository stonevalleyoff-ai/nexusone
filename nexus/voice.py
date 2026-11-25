"""La voix : parler à NEXUS depuis le téléphone, sans qu'un mot quitte la maison.

Le téléphone enregistre la question et l'envoie au PC. Le PC la transcrit avec whisper.cpp,
un moteur de reconnaissance vocale local, puis NEXUS répond comme à une question écrite.
La réponse est lue par la synthèse vocale du téléphone lui-même.

Le moteur est un programme séparé, tenu par NEXUS comme colibrì : `nexus voix installer` le
télécharge à une version précise, le compile et récupère son modèle (empreinte vérifiée).
L'enregistrement n'est pas gardé : il est transcrit puis effacé.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Callable
from urllib import parse, request

from .ollama import OllamaError

SOURCE = 'https://github.com/ggml-org/whisper.cpp'
PINNED_COMMIT = '96ec4d9823e9d0211944172da35f9f29134af990'      # version 1.9.6
MODEL_URL = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/'
MODELS = {      # nom : (fichier, empreinte SHA-256, taille)
    'small': ('ggml-small.bin', '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b', 487_601_967),
    'base': ('ggml-base.bin', '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe', 147_951_465),
}
TOOLS_PATH = '/usr/bin:/bin:/usr/local/bin'
MAX_AUDIO = 8 * 2**20           # octets : largement une minute de voix compressée
MAX_SECONDS = 90
# Ce que le moteur « entend » parfois dans un silence : des phrases de sous-titres apprises par cœur.
GHOSTS = re.compile(r"sous[- ]titr|amara\.org|merci d'avoir regard|abonnez[- ]vous|\bsoustitreur", re.I)
NOISE = re.compile(r'[\[(][^\])]{0,40}[\])]')      # [Musique], (silence), [BLANK_AUDIO]…

Run = Callable[[list[str], int], tuple[int, bytes]]


def _exec(argv: list[str], timeout: int) -> tuple[int, bytes]:
    try:
        done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, b''


def clean_transcript(text: str) -> str:
    """Le texte reconnu, sans les bruits entre crochets ni les phrases fantômes des silences."""
    text = ' '.join(NOISE.sub(' ', text).split()).strip(' -–—')
    return '' if not text or GHOSTS.search(text) else text[:1000]


class Voice:
    def __init__(self, base: Path, run: Run | None = None):
        self.base, self._run = Path(base), run
        self._lock = threading.Lock()         # une transcription à la fois : elle prend tous les cœurs

    # -- installation ---------------------------------------------------------------------
    @property
    def binary(self) -> Path:
        return self.base / 'whisper.cpp' / 'build' / 'bin' / 'whisper-cli'

    def model(self) -> Path | None:
        for _name, (filename, _sha, _size) in MODELS.items():
            if (self.base / filename).is_file():
                return self.base / filename
        return None

    def missing(self) -> list[str]:
        """Ce qu'il manque pour que la voix marche (liste vide : tout est là)."""
        if self._run is not None:
            return []
        missing = []
        if not self.binary.is_file():
            missing.append('le moteur de transcription (nexus voix installer)')
        if self.model() is None:
            missing.append('son modèle (nexus voix installer)')
        if shutil.which('ffmpeg', path=TOOLS_PATH) is None:
            missing.append('ffmpeg (sudo apt install ffmpeg)')
        return missing

    def installed(self) -> bool:
        return not self.missing()

    def install(self, model: str = 'small', *, source: str = SOURCE, say: Callable[[str], None] = print) -> None:
        """Télécharge whisper.cpp à la version épinglée, le compile, récupère le modèle et vérifie son empreinte."""
        if model not in MODELS:
            raise OllamaError(f'Modèle de voix inconnu. Choix : {", ".join(MODELS)}')
        for tool, package in (('git', 'git'), ('cmake', 'cmake'), ('c++', 'build-essential'), ('ffmpeg', 'ffmpeg')):
            if shutil.which(tool) is None:
                raise OllamaError(f'Il manque « {tool} » pour installer la voix : sudo apt install {package}')
        self.base.mkdir(parents=True, exist_ok=True)
        code = self.base / 'whisper.cpp'

        def git(*args: str) -> str:
            done = subprocess.run(['git', '-C', str(code), *args], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=1800)
            if done.returncode != 0:
                raise OllamaError('Téléchargement du moteur de voix impossible (réseau ?) : '
                                  + (done.stderr.strip().splitlines() or ['erreur git'])[-1][:200])
            return done.stdout.strip()

        if not (code / '.git').is_dir():
            code.mkdir(parents=True, exist_ok=True)
            git('init', '-q')
        if not (code / 'CMakeLists.txt').is_file() or git('rev-parse', 'HEAD') != PINNED_COMMIT:
            say(f'Téléchargement de whisper.cpp (version {PINNED_COMMIT[:7]})…')
            git('fetch', '-q', '--depth', '1', source, PINNED_COMMIT)
            git('checkout', '-q', '--detach', 'FETCH_HEAD')
            if git('rev-parse', 'HEAD') != PINNED_COMMIT:
                raise OllamaError('La version téléchargée du moteur de voix n\'est pas celle attendue')
        if not self.binary.is_file():
            say('Compilation du moteur de voix (quelques minutes)…')
            for argv in (['cmake', '-S', str(code), '-B', str(code / 'build'), '-DCMAKE_BUILD_TYPE=Release',
                          '-DWHISPER_BUILD_TESTS=OFF', '-DWHISPER_BUILD_SERVER=OFF'],
                         ['cmake', '--build', str(code / 'build'), '--config', 'Release', '--target', 'whisper-cli',
                          '-j', str(os.cpu_count() or 2)]):
                done = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=3600)
                if done.returncode != 0:
                    raise OllamaError('Compilation du moteur de voix impossible : '
                                      + (done.stderr.decode('utf-8', 'replace').strip().splitlines() or ['erreur'])[-1][:200])
        filename, digest, size = MODELS[model]
        target = self.base / filename
        if not target.is_file():
            say(f'Téléchargement du modèle de voix « {model} » ({size // 2**20} Mo)…')
            self._download(MODEL_URL + filename, target, digest)
        for other, (name, _sha, _size) in MODELS.items():      # un seul modèle : le dernier demandé
            if other != model:
                (self.base / name).unlink(missing_ok=True)

    @staticmethod
    def _download(url: str, target: Path, digest: str) -> None:
        partial = target.with_suffix('.partiel')
        sha = hashlib.sha256()
        try:
            with request.urlopen(request.Request(url, headers={'User-Agent': 'telechargement-modele'}), timeout=60) as response:
                host = parse.urlsplit(response.geturl()).hostname or ''
                if not (host == 'huggingface.co' or host.endswith('.hf.co') or host.endswith('.huggingface.co')):
                    raise OllamaError('Le modèle de voix est proposé depuis une adresse inattendue : téléchargement refusé')
                with partial.open('wb') as out:
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        sha.update(block)
                        out.write(block)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise OllamaError(f'Téléchargement du modèle de voix impossible ({type(exc).__name__})') from exc
        if sha.hexdigest() != digest:
            partial.unlink(missing_ok=True)
            raise OllamaError('Le modèle de voix téléchargé n\'a pas l\'empreinte attendue : il a été refusé')
        os.replace(partial, target)

    # -- transcription --------------------------------------------------------------------
    def transcribe(self, audio: bytes) -> str:
        """Le texte dit dans l'enregistrement ('' si rien d'intelligible). L'enregistrement n'est pas gardé."""
        if not audio or len(audio) > MAX_AUDIO:
            raise OllamaError('Enregistrement vide ou trop long (une minute et demie au plus)')
        if self.missing():
            raise OllamaError('La voix n\'est pas installée sur le PC : ' + ' ; '.join(self.missing()))
        run = self._run or _exec
        with self._lock, tempfile.TemporaryDirectory(prefix='nexus-voix-') as tmp:
            source, wav = Path(tmp) / 'question', Path(tmp) / 'question.wav'
            source.write_bytes(audio)
            # Conversion en son simple (16 kHz, une voie) : arguments figés, jamais de shell.
            code, _out = run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                              '-t', str(MAX_SECONDS), '-vn', '-ac', '1', '-ar', '16000', '-f', 'wav', str(wav)], 60)
            if code != 0 or not wav.is_file() or wav.stat().st_size < 8000:        # moins d'un quart de seconde
                raise OllamaError('Enregistrement illisible ou trop court : réessaie en parlant un peu plus longtemps')
            model = self.model() or Path('modele')
            code, out = run([str(self.binary), '-m', str(model), '-f', str(wav), '-l', 'fr', '-nt', '-np',
                             '-t', str(min(8, os.cpu_count() or 2))], 180)
            if code != 0:
                raise OllamaError('La transcription a échoué (moteur de voix)')
        return clean_transcript(out.decode('utf-8', errors='replace'))

    def describe(self) -> list[str]:
        missing = self.missing()
        if missing:
            return ['Voix : pas prête. Il manque ' + ' ; '.join(missing) + '.']
        model = self.model()
        name = next((n for n, (f, _s, _z) in MODELS.items() if model is not None and f == model.name), '?')
        return [f'Voix : prête (whisper.cpp, modèle « {name} »). Bouton micro dans l\'appli du téléphone.']

