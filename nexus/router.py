"""Aiguillage déterministe : choisit d'avance les outils évidents pour une question.

Un petit modèle (1 à 2 milliards de paramètres) oublie souvent d'appeler ses outils,
et cela varie d'une fois à l'autre. Pour les questions dont le sujet est clair
(« mon PC est lent », « le Wi-Fi ne marche pas »…), NEXUS consulte donc lui-même
les bons outils avant de laisser le modèle répondre ; le modèle peut toujours en
demander d'autres. Les questions sans mot-clé reconnu restent entièrement à son choix.
"""
from __future__ import annotations

from datetime import date
import re
import unicodedata

MAX_PLANNED = 3
QUESTION = object()  # remplacé par la question de l'utilisateur


def normalize(text: str) -> str:
    """minuscules, sans accents, tirets et apostrophes remplacés par des espaces."""
    text = unicodedata.normalize('NFKD', text.lower())
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[-'’]", ' ', text)


def _words(*items: str) -> re.Pattern:
    return re.compile(r'\b(' + '|'.join(items) + r')\b')


PROBLEM = _words(r'marche pas', r'fonctionne pas', r'plus de', r'pas de', r'probleme\w*', r'pourquoi',
                 r'panne', r'coupe\w*', r'aucun\w*', r'detecte pas')
ROUTES: list[tuple[re.Pattern, list[tuple[str, dict]]]] = [
    (_words(r'lent\w*', r'rame\w*', r'ralenti\w*', r'lag\w*', r'freeze', r'fige\w*', r'memoire', r'ram',
            r'gourmand\w*', r'programmes?', r'processus', r'applications?'),
     [('processus_gourmands', {'critere': 'memoire'})]),
    (_words(r'processeur', r'cpu', r'chauffe\w*', r'ventilateur\w*', r'surchauffe'),
     [('processus_gourmands', {'critere': 'processeur'})]),
    (_words(r'wifi', r'wi fi', r'reseau\w*', r'internet', r'connecte\w*', r'connexion\w*', r'ethernet',
            r'cable', r'dns', r'adresse ip', r'box', r'routeur'),
     [('etat_reseau', {})]),
    (_words(r'disques?', r'place', r'espace', r'stockage', r'ssd', r'cle usb', r'partition\w*'),
     [('stockage', {})]),
    (_words(r'carte graphique', r'gpu', r'graphique', r'ecran', r'affichage', r'resolution'),
     [('materiel', {'categorie': 'graphique'})]),
    (_words(r'son', r'audio', r'haut parleurs?', r'enceintes?', r'casque', r'micro\w*'),
     [('materiel', {'categorie': 'audio'})]),
    (_words(r'materiel', r'pilotes?', r'drivers?', r'peripheriques?', r'webcam', r'bluetooth', r'usb'),
     [('materiel', {'categorie': 'tout'})]),
    (_words(r'erreurs?', r'plante\w*', r'crash\w*', r'bugs?', r'beugue\w*', r'journal'),
     [('erreurs_recentes', {'minutes': 60})]),
    (_words(r'firmwares?', r'micrologiciels?'),
     [('firmwares_manquants', {})]),
    (_words(r'secure boot', r'uefi', r'bios', r'batterie', r'systeme', r'noyau', r'kernel', r'version',
            r'demarrage', r'ordinateur', r'machine', r'config\w*', r'caracteristiques'),
     [('infos_systeme', {})]),
    # Questions sur le CONTENU des papiers : recherche par le sens dans la bibliothèque.
    (_words(r'documents?', r'fichiers?', r'rapports?', r'pdf', r'cv', r'notes?', r'lettres?',
            r'textes?', r'compte rendu', r'proces verbal', r'memo', r'courriers?', r'contrats?',
            r'factures?', r'devis', r'loyer', r'bail', r'quittances?', r'assurances?', r'mutuelle',
            r'ordonnances?', r'medicaments?', r'impots?', r'revenu fiscal', r'salaires?', r'bulletins?',
            r'fiches? de paie', r'credits?', r'prets?', r'banque', r'releves?', r'solde', r'garanties?',
            r'garanti', r'abonnements?', r'electricite', r'tempo', r'jours rouges', r'compteur',
            r'consommation', r'eau', r'garage', r'attestations?', r'caution', r'resilier', r'resiliation',
            r'rendez vous', r'rdv', r'medecin', r'docteur', r'dentiste', r'pharmacie', r'vaccin\w*', r'analyses',
            r'remboursement\w*', r'voiture', r'vehicule', r'controle technique', r'carte grise', r'permis',
            r'echeances?', r'expire\w*', r'renouvel\w*', r'date limite', r'proprietaire', r'employeur',
            r'appartement', r'syndic', r'copropriete', r'taxes?', r'amendes?', r'tickets? de caisse'),
     [('rechercher_documents', {'question': QUESTION})]),
]
# « Quand est mon prochain… », « quel est mon numéro de… » : une question précise sur la vie de
# la personne se cherche dans ses papiers, même sans mot-clé reconnu.
PERSONAL = re.compile(r'\b(quand|combien|quel\w*|jusqu a quand|a quelle)\b.*\b(mon|ma|mes)\b|'
                      r'\b(mon|ma|mes)\b.*\b(quand|combien|quel\w*)\b')
AGENDA_INTENT = _words(r'agenda', r'calendrier', r'planning', r'emploi du temps', r'rendez vous', r'rdv',
                       r'rappels?', r'ma liste', r'choses a faire', r'a faire', r'todo', r'prevus?', r'prevues?',
                       r'programme (?:de|du|d) (?:la|ma|cette)? ?(?:journee|semaine|demain)')
MAIL_INTENT = _words(r'e ?mails?', r'mails?', r'courriels?', r'boite mail', r'boite de reception', r'messagerie', r'gmail')
EXPENSE_INTENT = re.compile(r'\bcombien\b.*\b(paye\w*|depense\w*|coute\w*|regle\w*)\b|\bdepenses?\b|'
                            r'\btotal (de|des) (mes )?(factures|depenses)\b|\bbudget\b')
EXPENSE_CATEGORIES = [
    (_words(r'electricite', r'edf', r'tempo', r'energie', r'chauffage', r'gaz'), 'energie'),
    (_words(r'loyer', r'eau', r'logement', r'appartement', r'maison', r'travaux'), 'logement'),
    (_words(r'internet', r'box', r'fibre', r'mobile', r'telephone', r'forfait'), 'telecom'),
    (_words(r'voiture', r'garage', r'vehicule', r'auto'), 'vehicule'),
    (_words(r'impots?', r'taxes?'), 'impots'),
    (_words(r'sante', r'medecin', r'mutuelle', r'pharmacie'), 'sante'),
    (_words(r'achats?', r'electromenager', r'ordinateur'), 'achats'),
]
DOC_INTENT = _words(r'resume\w*', r'lis', r'lire', r'relis', r'que dit', r'contenu d\w*', r'de quoi parle')
# Un périphérique qui « ne marche pas » : les firmwares manquants sont une cause fréquente.
HARDWARE_PROBLEM = _words(r'wifi', r'wi fi', r'son', r'audio', r'bluetooth', r'webcam', r'reseau')


OWN_FIGURES = re.compile(r'\b\d+(?:[.,]\d+)?\s*(?:go|gb|mo|mb|ko|to|kwh|kva|watts?|w|%|°c|degres)\b')
MEASURING_TOOLS = {'processus_gourmands', 'infos_systeme', 'stockage', 'materiel'}


def expense_filters(text: str) -> dict:
    """Catégorie et année déduites d'une question d'argent (texte déjà normalisé)."""
    filters: dict = {'categorie': 'toutes', 'annee': 0}
    for pattern, category in EXPENSE_CATEGORIES:
        if pattern.search(text):
            filters['categorie'] = category
            break
    year = re.search(r'\b(19[9]\d|20\d\d)\b', text)
    if year:
        filters['annee'] = int(year.group(1))
    elif re.search(r'\b(l an dernier|l annee derniere|l an passe|l annee passee)\b', text):
        filters['annee'] = date.today().year - 1
    elif re.search(r'\b(cette annee|depuis janvier)\b', text):
        filters['annee'] = date.today().year
    return filters


def plan(question: str) -> list[tuple[str, dict]]:
    """Outils à consulter d'office, dans l'ordre, sans doublon (au plus MAX_PLANNED)."""
    text = normalize(question)
    planned: list[tuple[str, dict]] = []
    if EXPENSE_INTENT.search(text):
        planned.append(('depenses', expense_filters(text)))
    # L'utilisateur donne ses propres chiffres (« j'ai 8 Go, le système en prend 2,5… ») :
    # c'est un calcul ou un cas hypothétique. Mesurer la vraie machine mélangerait deux
    # séries de chiffres dans la réponse (constaté sur 9 modèles).
    own_figures = len(OWN_FIGURES.findall(text)) >= 2
    matched = False
    for pattern, calls in ROUTES:
        if pattern.search(text):
            matched = True
            for name, args in calls:
                if own_figures and name in MEASURING_TOOLS:
                    continue
                args = {k: (question[:300] if v is QUESTION else v) for k, v in args.items()}
                if (name, args) not in planned:
                    planned.append((name, args))
    if MAIL_INTENT.search(text):
        planned.insert(0, ('courrier', {'jours': 7}))
    if AGENDA_INTENT.search(text):
        days = 31 if re.search(r'\bmois\b', text) else 14 if re.search(r'semaine prochaine|quinze|15 jours', text) else 7
        planned.insert(0, ('agenda', {'jours': days}))
    if not matched and not own_figures and PERSONAL.search(text):
        planned.append(('rechercher_documents', {'question': question[:300]}))
    # Une demande explicite de lecture (« que dit… », « résume… ») vise forcément un document.
    if DOC_INTENT.search(text) and ('chercher_documents', {'mot': ''}) not in planned:
        planned.insert(0, ('chercher_documents', {'mot': ''}))
    if PROBLEM.search(text) and HARDWARE_PROBLEM.search(text):
        planned.insert(1, ('firmwares_manquants', {}))
    # Une question générale (« c'est quoi la RAM ? ») ne doit pas déclencher 3 consultations.
    unique, seen = [], set()
    for name, args in planned:
        if name not in seen:
            seen.add(name)
            unique.append((name, args))
    return unique[:MAX_PLANNED]


READ_INTENT = _words(r'resume\w*', r'lis', r'lire', r'relis', r'explique\w*', r'que dit', r'contenu',
                     r'analyse\w*', r'corrige\w*', r'tradui\w*', r'parle', r'de quoi')
STOPWORDS = {'mon', 'ma', 'mes', 'le', 'la', 'les', 'un', 'une', 'des', 'du', 'de', 'et', 'est', 'dans',
             'qui', 'que', 'quoi', 'sur', 'pour', 'avec', 'moi', 'stp', 'svp', 'fichier', 'fichiers',
             'document', 'documents', 'texte', 'resume', 'resumer', 'lis', 'lire', 'explique', 'peux',
             'tu', 'me', 'ce', 'cet', 'cette', 'dit', 'contenu'}


def wants_reading(question: str) -> bool:
    return bool(READ_INTENT.search(normalize(question)))


def pick_document(question: str, search_result: str) -> str | None:
    """Parmi les documents listés par chercher_documents, celui que la question désigne.

    Renvoie le chemin relatif, ou None si rien ne correspond clairement (le modèle choisira).
    """
    paths = [line[2:line.rfind(' (')] for line in search_result.splitlines()
             if line.startswith('- ') and line.endswith(')') and ' (' in line]
    if not paths:
        return None
    wanted = {w for w in re.findall(r'[a-z0-9]{3,}', normalize(question)) if w not in STOPWORDS}
    best, best_score = None, 0
    for path in paths:
        tokens = set(re.findall(r'[a-z0-9]{3,}', normalize(path.rsplit('/', 1)[-1].rsplit('.', 1)[0])))
        # « rapport » correspond à « rapports », « annuel » à « annuel2025 »…
        score = sum(1 for w in wanted if any(t.startswith(w[:5]) or w.startswith(t[:5]) for t in tokens))
        if score > best_score:
            best, best_score = path, score
    if best is None and len(paths) == 1:
        return paths[0]
    return best
