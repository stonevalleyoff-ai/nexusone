"""Tous les moteurs locaux derrière une seule porte.

NEXUS peut disposer de plusieurs modèles sur deux moteurs : Ollama (petits modèles rapides)
et colibrì (un gros modèle, tenu par NEXUS). `Fleet` les présente comme un seul client :
la liste réunit les modèles des deux, et chaque demande part vers le moteur du modèle
choisi. Le choix lui-même est fait ailleurs (nexus/models.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator

from .ollama import ModelInfo, OllamaClient, OllamaError, Stats, ToolCalls


class Fleet:
    logs_hint = 'journalctl -u ollama ; nexus moteur journal'

    def __init__(self, ollama: OllamaClient, colibri: Any = None):
        self.ollama, self.colibri = ollama, colibri
        self._served: ModelInfo | None = None     # le modèle tel que le moteur colibrì le nomme
        self._sizes: dict[str, int] = {}

    # -- identité -----------------------------------------------------------------------
    @property
    def name(self) -> str:
        return 'Ollama + colibrì' if self.big_model() else 'Ollama'

    @property
    def base_url(self) -> str:
        return self.ollama.base_url

    @property
    def engine(self):
        """Le moteur colibrì tenu par NEXUS, s'il y en a un (démarrage, veille, arrêt)."""
        return self.colibri.engine if self.colibri is not None else None

    @property
    def on_progress(self) -> Callable[[str], None] | None:
        return self.colibri.on_progress if self.colibri is not None else None

    @on_progress.setter
    def on_progress(self, callback: Callable[[str], None] | None) -> None:
        if self.colibri is not None:
            self.colibri.on_progress = callback

    def big_model(self) -> str | None:
        """Nom du gros modèle installé pour colibrì, sans démarrer le moteur."""
        if self.colibri is None:
            return None
        found = self.colibri.engine.installed()
        return found.model_id if found is not None else None

    def engine_of(self, model: str) -> str:
        return 'colibri' if model == self.big_model() else 'ollama'

    def reads_ahead_for(self, model: str) -> bool:
        return self.engine_of(model) == 'colibri'

    # -- même contrat qu'OllamaClient ---------------------------------------------------
    def version(self, timeout: float = 5) -> str:
        try:
            return self.ollama.version(timeout)
        except OllamaError:
            if self.big_model():
                return 'Ollama absent'
            raise

    def _big_size(self) -> int:
        found = self.colibri.engine.installed()
        key = str(found.model_dir)
        if key not in self._sizes:
            try:
                self._sizes[key] = sum(f.stat().st_size for f in Path(found.model_dir).iterdir() if f.is_file())
            except OSError:
                self._sizes[key] = 0
        return self._sizes[key]

    def models(self, timeout: float = 10) -> list[dict[str, Any]]:
        """Modèles de tous les moteurs : [{'name', 'size', 'engine'}]. Ne démarre aucun moteur."""
        big = self.big_model()
        try:
            listed = [{**m, 'engine': 'ollama'} for m in self.ollama.models(timeout)]
        except OllamaError:
            if not big:
                raise
            listed = []        # Ollama absent : le gros modèle suffit pour dialoguer
        if big:
            listed.append({'name': big, 'size': self._big_size(), 'engine': 'colibri'})
        return listed

    def show(self, model: str) -> ModelInfo:
        if self.engine_of(model) == 'colibri':
            return ModelInfo(name=model, thinking=True, context_length=None, tools=True)
        return self.ollama.show(model)

    def load(self, model: str, keep_alive: str = '30m') -> None:
        if self.engine_of(model) == 'colibri':
            self._serving()
        else:
            self.ollama.load(model, keep_alive)

    def unload(self, model: str) -> None:
        (self.colibri if self.engine_of(model) == 'colibri' else self.ollama).unload(model)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        return self.ollama.embed(model, texts)

    def abort(self) -> None:
        self.ollama.abort()
        if self.colibri is not None:
            self.colibri.abort()

    def _serving(self) -> ModelInfo:
        """Démarre le moteur colibrì au besoin et renvoie son modèle, sous le nom qu'il lui donne."""
        if self._served is None or not self.colibri.engine.running():
            names = [m['name'] for m in self.colibri.models()]
            if not names:
                raise OllamaError('Le moteur colibrì ne sert aucun modèle de dialogue')
            self._served = self.colibri.show(names[0])
        return self._served

    def chat_stream(self, info: ModelInfo, messages: list[dict[str, Any]],
                    **options) -> Iterator[str | ToolCalls | Stats]:
        if self.engine_of(info.name) == 'colibri':
            yield from self.colibri.chat_stream(self._serving(), messages, **options)
        else:
            yield from self.ollama.chat_stream(info, messages, **options)
