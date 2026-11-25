"""Banc d'essai d'un modèle pour NEXUS, quel que soit le moteur (Ollama ou colibrì).

    python3 -m tests.bench_modele                    # modèle par défaut du moteur configuré
    python3 -m tests.bench_modele qwen3.5:2b         # un modèle Ollama précis
    NEXUS_BACKEND=colibri python3 -m tests.bench_modele

Trois mesures, les mêmes pour tous les modèles :
1. choix des outils SANS l'aiguillage (16 questions, 2 passes) ;
2. vitesse : lecture de la question et écriture de la réponse ;
3. fidélité aux documents : 6 questions sur le jeu de faux documents, réponse vérifiée
   par la présence du bon chiffre (la recherche par le sens demande Ollama + bge-m3).
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time

from nexus.assistant import TOOLS_SYSTEM_PROMPT, Assistant, Turn
from nexus.config import Settings
from nexus.library import Library
from nexus.memory import Store
from nexus.ollama import OllamaError, Stats, ToolCalls, pick_model
from nexus.tools import Env, Toolbox
from tests.corpus import build

TOOL_QUESTIONS = [
    ('Quels programmes prennent le plus de mémoire en ce moment ?', {'processus_gourmands'}),
    ('Mon Wi-Fi ne marche pas, pourquoi ?', {'etat_reseau', 'firmwares_manquants', 'materiel'}),
    ('Quelle carte graphique a cet ordinateur ?', {'materiel'}),
    ('Il reste combien de place sur le disque ?', {'stockage'}),
    ('Résume mon rapport annuel.', {'chercher_documents', 'lire_document', 'rechercher_documents'}),
    ('Y a-t-il eu des erreurs récemment ?', {'erreurs_recentes'}),
    ('Le Secure Boot est-il activé ?', {'infos_systeme'}),
    ('Mon PC est lent, tu peux regarder ?', {'processus_gourmands', 'infos_systeme', 'erreurs_recentes'}),
    ('Est-ce que le son a un pilote ?', {'materiel'}),
    ('Combien de RAM a cette machine ?', {'infos_systeme'}),
    ('Ma batterie est-elle usée ?', {'infos_systeme'}),
    ('Bonjour, ça va ?', set()),
    ('Écris un petit poème sur la mer.', set()),
    ("C'est quoi la différence entre la RAM et le disque ?", set()),
    ('Donne-moi une recette de crêpes.', set()),
    ('Traduis « bonne nuit » en anglais.', set()),
]
DOC_QUESTIONS = [
    ('Quel est le montant du dépôt de garantie de mon appartement ?', ['890']),
    ('Quand se termine mon assurance habitation ?', ['30/06/2026', '30 juin 2026']),
    ('Combien de jours rouges par an dans mon contrat Tempo ?', ['22']),
    ("Jusqu'à quand mon ordinateur est-il garanti ?", ['18/11/2027', '18 novembre 2027']),
    ('Quel est mon revenu fiscal de référence ?', ['38 420', '38420', '38 420']),
    ('Quel est le taux de mon crédit pour la voiture ?', ['4,20', '4,2 %', '4.20', '4.2']),
]


def run(model_arg: str | None) -> int:
    sys.stdout.reconfigure(line_buffering=True)   # résultats visibles au fil de l'eau, même redirigés
    settings = Settings.load()
    client = settings.open_client(timeout=1800)
    try:
        names = [m['name'] for m in client.models()]
        model = pick_model(names, model_arg or settings.model)
        if model is None or (model_arg and model != model_arg and model != model_arg + ':latest'):
            print(f'Modèle introuvable sur {client.name}. Disponibles : {", ".join(names) or "aucun"}')
            return 2
        info = client.show(model)
    except OllamaError as exc:
        print(f'{client.name} : {exc}')
        return 2
    print(f'Modèle : {model} · moteur : {client.name} ({client.base_url})')
    schemas = Toolbox().schemas()

    # 1. vitesse
    question = [{'role': 'system', 'content': TOOLS_SYSTEM_PROMPT},
                {'role': 'user', 'content': "Explique en cinq phrases ce qu'est un système d'exploitation."}]
    started = time.monotonic()
    first, stats = None, None
    for item in client.chat_stream(info, question, num_ctx=6144, num_predict=160, tools=schemas):
        if isinstance(item, str) and first is None:
            first = time.monotonic()
        if isinstance(item, Stats):
            stats = item
    wait = (first or time.monotonic()) - started
    speed = stats.tokens_per_second if stats else 0.0
    print(f'Vitesse : premier mot après {wait:.1f} s (outils décrits dans la requête), puis {speed:.1f} jetons/s')

    # 2. choix des outils, sans aiguillage
    good, misses = 0, []
    for _ in range(2):
        for text, expected in TOOL_QUESTIONS:
            called: list[str] = []
            messages = [{'role': 'system', 'content': TOOLS_SYSTEM_PROMPT}, {'role': 'user', 'content': text}]
            try:
                for item in client.chat_stream(info, messages, num_ctx=6144, num_predict=120, tools=schemas):
                    if isinstance(item, ToolCalls):
                        called += [str(c.get('function', {}).get('name')) for c in item.calls]
            except OllamaError as exc:
                called = [f'ERREUR : {exc}']
            ok = (not called) if not expected else bool(set(called) & expected)
            good += ok
            if not ok:
                misses.append(f'{text[:44]} -> {called}')
    total = 2 * len(TOOL_QUESTIONS)
    print(f'Choix des outils (sans aiguillage) : {good}/{total} ({100 * good // total} %)')
    for miss in sorted(set(misses)):
        print(f'    raté : {miss}')

    # 3. fidélité aux documents
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'Documents'
        build(root)
        (root / 'manifest.json').unlink()
        library = Library(Path(tmp) / 'bib.sqlite3', root, client, embed_model=settings.embed_model)
        library.scan(classify=False)
        if library.degraded:
            print('  (modèle de sens indisponible : recherche par mots-clés, résultats moins bons)')
        store = Store(':memory:')
        assistant = Assistant(client, store, toolbox=Toolbox(Env(home=root, library=library)))
        right = 0
        for text, accepted in DOC_QUESTIONS:
            turn = Turn()
            try:
                for _item in assistant.reply(store.new_conversation(), model, text, '', turn=turn):
                    pass
            except OllamaError as exc:
                turn.text = f'ERREUR : {exc}'
            ok = any(value in turn.text for value in accepted)
            right += ok
            if not ok:
                print(f'    raté : {text} -> {turn.text[:110]!r}')
        print(f'Fidélité aux documents : {right}/{len(DOC_QUESTIONS)}')
        library.close()
    return 0


if __name__ == '__main__':
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
