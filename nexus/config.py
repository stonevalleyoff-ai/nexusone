"""Réglages, lus dans l'environnement puis dans /etc/nexus (posés à la construction de l'ISO)."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .assistant import DEFAULT_NUM_CTX
from .ollama import DEFAULT_URL, MODEL_RE, is_cloud

SYSTEM_DIR = Path('/etc/nexus')


def _home(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else Path.home() / fallback


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, '')
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if low <= value <= high else default


def preferred_model(system_dir: Path = SYSTEM_DIR) -> str | None:
    raw = os.environ.get('NEXUS_MODEL')
    if raw is None:
        try:
            raw = (system_dir / 'model').read_text(encoding='utf-8').strip()
        except OSError:
            raw = None
    if raw and MODEL_RE.fullmatch(raw) and not is_cloud(raw):
        return raw
    return None


def _model_env(name: str, default: str) -> str:
    raw = os.environ.get(name, '').strip()
    return raw if raw and MODEL_RE.fullmatch(raw) and not is_cloud(raw) else default


def _engine(base: Path, idle: int):
    from .engine import Engine
    code, model = os.environ.get('NEXUS_COLIBRI_DIR'), os.environ.get('NEXUS_COLIBRI_MODELE')
    return Engine(base, idle_minutes=idle,
                  colibri_dir=Path(code).expanduser() if code and model else None,
                  model_dir=Path(model).expanduser() if code and model else None)


@dataclass(frozen=True)
class Settings:
    ollama_url: str
    model: str | None
    db_path: Path
    rules_path: Path
    num_ctx: int
    tools: bool = True  # outils de consultation en lecture seule
    library_root: Path | None = None   # dossier des documents à indexer
    library_db: Path | None = None
    embed_model: str = 'bge-m3'
    souvenirs_db: Path | None = None
    memory: bool = True  # mémoire à long terme
    skills_dir: Path | None = None  # savoir-faire de l'utilisateur (un JSON chacun)
    planner_db: Path | None = None   # tâches planifiées et notifications
    agenda_db: Path | None = None    # rendez-vous, rappels et choses à faire
    autonomy_db: Path | None = None  # journal de ce que NEXUS fait de lui-même
    autonomy_levels: Path | None = None   # niveau d'autonomie par geste (JSON)
    mail_db: Path | None = None      # courriels vus : expéditeur, objet, avis, résumé (pas le texte)
    mail_accounts: Path | None = None   # boîtes renseignées (fichier lisible par son propriétaire seul)
    backend: str = 'ollama'          # 'auto' (tous les moteurs, NEXUS choisit), 'ollama' ou 'colibri'
    measures_path: Path | None = None   # mesures des modèles sur cette machine
    colibri_url: str | None = None   # None : moteur colibrì tenu par NEXUS ; sinon un colibrì déjà lancé
    engine_dir: Path | None = None   # dossier du moteur (code, modèle, réglages, journal)
    engine_idle: int = 30            # minutes sans question avant l'arrêt du moteur (0 : jamais)

    @classmethod
    def load(cls) -> 'Settings':
        data = _home('XDG_DATA_HOME', '.local/share') / 'nexus'
        conf = _home('XDG_CONFIG_HOME', '.config') / 'nexus'
        rules = os.environ.get('NEXUS_RULES')
        engine_dir = Path(os.environ.get('NEXUS_MOTEUR', str(data / 'moteur'))).expanduser()
        idle = _int('NEXUS_MOTEUR_VEILLE', 30, 0, 100000)
        url = os.environ.get('NEXUS_COLIBRI_URL', '').strip() or None
        wanted = os.environ.get('NEXUS_BACKEND', '').strip().lower()
        if wanted in ('colibri', 'colibrì', 'openai'):
            backend = 'colibri'
        elif wanted == 'ollama':
            backend = 'ollama'
        else:   # rien de demandé : tous les moteurs installés, et NEXUS choisit le modèle
            backend = 'colibri' if url else 'auto'
        return cls(
            ollama_url=os.environ.get('NEXUS_OLLAMA_URL', DEFAULT_URL),
            model=preferred_model(),
            db_path=Path(os.environ.get('NEXUS_DB', str(data / 'nexus.sqlite3'))),
            rules_path=Path(rules) if rules else conf / 'rules.json',
            num_ctx=_int('NEXUS_NUM_CTX', DEFAULT_NUM_CTX, 1024, 131072),
            tools=os.environ.get('NEXUS_TOOLS', '1').strip().lower() not in ('0', 'non', 'no', 'false'),
            library_root=Path(os.environ.get('NEXUS_BIBLIOTHEQUE', str(Path.home() / 'Documents'))).expanduser(),
            library_db=data / 'bibliotheque.sqlite3',
            souvenirs_db=data / 'souvenirs.sqlite3',
            skills_dir=conf / 'savoir-faire',
            planner_db=data / 'taches.sqlite3',
            agenda_db=data / 'agenda.sqlite3',
            autonomy_db=data / 'activite.sqlite3',
            autonomy_levels=conf / 'autonomie.json',
            mail_db=data / 'courrier.sqlite3',
            mail_accounts=conf / 'courrier.json',
            backend=backend,
            measures_path=data / 'modeles.json',
            colibri_url=url,
            engine_dir=engine_dir,
            engine_idle=idle,
            memory=os.environ.get('NEXUS_MEMOIRE', '1').strip().lower() not in ('0', 'non', 'no', 'false'),
            embed_model=_model_env('NEXUS_EMBED_MODEL', 'bge-m3'),
        )

    def open_client(self, timeout: float = 300.0):
        """Le client du moteur de dialogue choisi. Les embeddings passent toujours par Ollama."""
        from .ollama import OllamaClient
        if self.backend == 'colibri':
            from .openai_compat import ManagedColibriClient, OpenAICompatClient
            embedder = OllamaClient(self.ollama_url, timeout)
            if self.colibri_url:   # un colibrì lancé par quelqu'un d'autre : on s'y branche tel quel
                return OpenAICompatClient(self.colibri_url, timeout, embedder=embedder,
                                          api_key=os.environ.get('NEXUS_COLIBRI_KEY'))
            return ManagedColibriClient(self.open_engine(), timeout, embedder=embedder)
        if self.backend == 'auto':
            from .fleet import Fleet
            from .openai_compat import ManagedColibriClient
            ollama = OllamaClient(self.ollama_url, timeout)
            return Fleet(ollama, ManagedColibriClient(self.open_engine(), timeout, embedder=ollama))
        return OllamaClient(self.ollama_url, timeout)

    def open_chooser(self, client, ram_mib: int | None = None, cores: int | None = None):
        """Le choix du modèle (nexus/models.py). Un modèle imposé (NEXUS_MODEL) court-circuite le choix."""
        from .models import Chooser, Measures, machine_signature
        if ram_mib is None:
            from .telemetry import Telemetry
            sample = Telemetry().sample()
            ram_mib, cores = sample.mem_total_mib, sample.cores
        return Chooser(client, Measures(self.measures_path, machine_signature(cores, ram_mib)),
                       ram_mib=ram_mib, forced=self.model)

    def open_engine(self):
        """Le moteur colibrì tenu par NEXUS (installé ou non)."""
        data = _home('XDG_DATA_HOME', '.local/share') / 'nexus'
        return _engine(self.engine_dir or data / 'moteur', self.engine_idle)

    def open_autonomy(self, library, agenda, planner):
        """Ce que NEXUS fait seul (nexus/autonomy.py), ou None si rien n'est configuré pour."""
        from .autonomy import Autonomy
        if self.autonomy_db is None or library is None:
            return None
        return Autonomy(self.autonomy_db, self.autonomy_levels, library=library, agenda=agenda, planner=planner)

    def open_mailbox(self, client=None, chooser=None):
        """Le courrier en lecture seule (nexus/mail.py). Le tri est fait par le modèle « de fond »."""
        from .mail import Mailbox
        if self.mail_db is None:
            return None
        model = (lambda: chooser.pick('fond').name) if chooser is not None else None
        return Mailbox(self.mail_db, self.mail_accounts, client=client, model=model)

    def open_relay(self, planner):
        """Le relais d'alertes pour téléphone verrouillé (nexus/alerts.py) ; None sans planificateur."""
        from .alerts import Relay
        return Relay(self.db_path.parent / 'alertes.json', planner) if planner is not None else None

    def databases(self) -> list:
        """Toutes les bases de données de NEXUS (pour la sauvegarde et les mises à jour)."""
        data = self.db_path.parent
        return [p for p in (self.db_path, self.library_db, self.souvenirs_db, self.planner_db, self.agenda_db,
                            self.autonomy_db, self.mail_db, data / 'appareils.sqlite3') if p is not None]

    def open_updater(self):
        """Les mises à jour (nexus/update.py) : versions posées côte à côte, hors des données."""
        from .update import SOURCE, Updater
        data = self.db_path.parent
        return Updater(data.parent / 'nexus-programme', data, source=os.environ.get('NEXUS_SOURCE', '').strip() or SOURCE,
                       databases=self.databases())

    def open_backup(self):
        """La sauvegarde chiffrée (nexus/backup.py) : documents, réglages et données de NEXUS."""
        from .backup import Backup
        data = self.db_path.parent
        conf = self.autonomy_levels.parent if self.autonomy_levels is not None else None
        databases = self.databases()
        # Ni les moteurs ni leurs modèles (ils se retéléchargent), ni le fichier de service.
        excluded = [data / 'voix', data / 'service'] + ([self.engine_dir] if self.engine_dir is not None else [])
        return Backup(data / 'sauvegarde', documents=self.library_root, data=data, config=conf,
                      databases=databases, excluded=excluded)

    @staticmethod
    def engine_probe(client, chooser):
        """« Le moteur répond-il, avec un modèle pour dialoguer ? » : une vraie question posée au moteur,
        pas un réglage relu (un modèle imposé ne prouve pas que le moteur tourne)."""
        from .ollama import OllamaError

        def probe() -> str:
            if not client.models():
                raise OllamaError('Aucun modèle n\'est installé (ollama pull …)')
            return chooser.pick('dialogue').name
        return probe

    def open_health(self, *, engine=None, planner=None, mailbox=None, backup=None, updater=None):
        """La santé de NEXUS (nexus/health.py) : ce qu'il vérifie sur lui-même."""
        from .health import Health
        data = self.db_path.parent
        folders = [data] + ([self.library_root] if self.library_root is not None else [])
        return Health(engine=engine, planner=planner, mailbox=mailbox, backup=backup, folders=folders,
                      certificate=data / 'serveur' / 'certificat.pem', service=self.open_service(),
                      updater=updater if updater is not None else self.open_updater(), system_root=Path('/'))

    def open_service(self):
        """NEXUS en service du PC (nexus/service.py)."""
        from .service import Service
        return Service(self)

    def open_vpn(self):
        """Le tunnel privé de la maison (nexus/vpn.py)."""
        from .vpn import Vpn
        return Vpn(self.db_path.parent / 'vpn')

    def open_voice(self):
        """La voix (nexus/voice.py) : transcription locale des questions dictées au téléphone."""
        from .voice import Voice
        return Voice(self.db_path.parent / 'voix')

    def open_press(self):
        """Le kiosque du journal du matin (nexus/journal.py)."""
        from .journal import Press
        data = self.db_path.parent
        conf = self.autonomy_levels.parent if self.autonomy_levels is not None else None
        return Press(data / 'journal', conf / 'journal.json' if conf is not None else None)

    def open_agenda(self):
        from .agenda import Agenda
        return Agenda(self.agenda_db) if self.agenda_db is not None else None

    def open_planner(self):
        from .planner import Planner
        return Planner(self.planner_db) if self.planner_db is not None else None

    def open_skills(self, toolbox):
        from .skills import SkillBook
        return SkillBook(self.skills_dir, set(toolbox.tools))

    def open_souvenirs(self, client):
        """La mémoire à long terme, ou None si elle est désactivée (NEXUS_MEMOIRE=0)."""
        from .souvenirs import Souvenirs
        if not self.memory or self.souvenirs_db is None:
            return None
        return Souvenirs(self.souvenirs_db, client, embed_model=self.embed_model)

    def open_library(self, client, llm_model: str | None):
        """La bibliothèque si son dossier existe, sinon None (aucun dossier n'est créé d'office)."""
        from .library import Library
        if self.library_root is None or self.library_db is None or not self.library_root.is_dir():
            return None
        return Library(self.library_db, self.library_root, client, embed_model=self.embed_model,
                       llm_model=llm_model)
