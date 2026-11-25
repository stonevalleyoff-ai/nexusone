"""Banc d'essai de la mémoire (nécessite Ollama et le modèle d'embedding) :

    python3 -m tests.bench_souvenirs [modele_embedding]

Mesure, pour plusieurs seuils, la part des questions qui rappellent le BON souvenir et
la part des questions sans rapport qui en rappellent un quand même (faux rappel).
"""
from __future__ import annotations

import sys

from nexus.ollama import OllamaClient
from nexus.souvenirs import Souvenirs

MEMORIES = [
    'Je pars au travail à 7h30 en semaine',
    'Je préfère les réponses courtes',
    'Tutoie-moi',
    'Ma fille s\'appelle Léa et elle a 8 ans',
    'La voiture est une Renault Mégane électrique',
    'On charge la voiture seulement la nuit en heures creuses',
    'Le chauffage est électrique, avec des radiateurs à fil pilote',
    'Mon abonnement est Tempo 9 kVA',
    'Je travaille de chez moi le mercredi',
    'Je suis allergique à la pénicilline',
    'On a décidé de garder l\'assurance habitation jusqu\'en 2027',
    'Ma mère habite à Lyon',
    'Je fais du vélo le dimanche matin',
    'Le chat s\'appelle Pixel',
    'Je n\'aime pas qu\'on me réveille avant 7h',
]
RELEVANT = [
    ('À quelle heure dois-je partir demain matin ?', 0),
    ('Rappelle-moi quand je pars le matin', 0),
    ('Combien d\'ans a ma fille ?', 3),
    ('Quel cadeau pour l\'anniversaire de Léa ?', 3),
    ('Quand vaut-il mieux recharger la voiture ?', 5),
    ('Quel modèle de voiture j\'ai ?', 4),
    ('Comment baisser le chauffage pendant les jours rouges ?', 6),
    ('Quelle puissance a mon abonnement électrique ?', 7),
    ('Je suis au bureau mercredi ?', 8),
    ('Le médecin me propose de l\'amoxicilline, ça va ?', 9),
    ('Faut-il changer d\'assurance habitation cette année ?', 10),
    ('Organise un week-end chez ma mère', 11),
    ('Qu\'est-ce que je peux faire dimanche ?', 12),
    ('Quelles croquettes pour Pixel ?', 13),
    ('Programme un réveil pour demain', 14),
]
UNRELATED = [
    'Mon PC est lent, tu peux regarder ?', 'Quelle carte graphique a cet ordinateur ?',
    'Il reste combien de place sur le disque ?', 'Écris un poème sur la mer', 'Donne-moi une recette de crêpes',
    'Traduis « bonne nuit » en anglais', 'C\'est quoi la différence entre la RAM et le disque ?',
    'Résume mon rapport annuel', 'Quelle est la capitale du Canada ?', 'Explique la photosynthèse',
    'Y a-t-il eu des erreurs système récemment ?', 'Combien font 17 fois 23 ?',
]


def run(model: str) -> None:
    store = Souvenirs(':memory:', OllamaClient(timeout=600), embed_model=model)
    ids = [store.add(text)[0] for text in MEMORIES]
    relevant = [(q, ids[i], store._scored(q)) for q, i in RELEVANT]
    unrelated = [(q, store._scored(q)) for q in UNRELATED]
    print(f'Modèle {model} — {len(MEMORIES)} souvenirs')
    for threshold in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        hits = sum(1 for _, wanted, scored in relevant
                   if any(row['id'] == wanted and score >= threshold for score, row in scored[:4]))
        false = sum(1 for _, scored in unrelated if scored and scored[0][0] >= threshold)
        print(f'  seuil {threshold:.2f} : bon souvenir rappelé {hits}/{len(RELEVANT)}   '
              f'faux rappels {false}/{len(UNRELATED)}')
    print('  plus hauts scores des questions sans rapport :',
          ', '.join(f'{scored[0][0]:.2f}' for _, scored in unrelated))
    print('  score du bon souvenir :', ', '.join(
        f'{next(s for s, r in scored if r["id"] == wanted):.2f}' for _, wanted, scored in relevant))


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else 'bge-m3')
