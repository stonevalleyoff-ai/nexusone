"""Les actualités du journal, choisies d'après ce que NEXUS sait de la personne.

NEXUS ne lit pas « tout Internet » : il a un petit catalogue de flux publics, par rubrique.
Il choisit les rubriques puis les articles par proximité de sens avec les souvenirs validés
(« j'ai une voiture électrique », « contrat Tempo »…), le calcul se faisant sur le PC.

Ce qui sort de la machine : la simple demande des flux choisis, comme le ferait un lecteur
de flux. Les souvenirs, eux, ne partent nulle part. Ce qui entre est traité comme du texte :
nettoyé de son HTML, jamais exécuté, jamais pris pour une consigne.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import html
import math
import re
from typing import Any, Callable
from urllib import error, parse, request
import xml.etree.ElementTree as ET

from .ollama import OllamaError

MAX_FEED_BYTES = 2_000_000
MAX_AGE = timedelta(hours=48)
THEME_THRESHOLD = 0.52          # proximité minimale souvenir ↔ rubrique (étalonnée avec bge-m3 : les rubriques
                                # sans rapport tombent sous 0,46, les bonnes au-dessus de 0,55)
ARTICLE_THRESHOLD = 0.50        # au-dessus, un article de « À la une » entre aussi dans « Pour toi »
MAX_THEMES = 5
USER_AGENT = 'Mozilla/5.0 (compatible; lecteur de flux personnel)'

# Rubriques : un portrait en quelques mots (pour le rapprochement de sens) et des flux publics.
THEMES: dict[str, dict[str, Any]] = {
    'general': {'nom': 'À la une', 'profil': 'actualité générale du jour en France et dans le monde',
                'flux': [('Le Monde', 'https://www.lemonde.fr/rss/une.xml'),
                         ('franceinfo', 'https://www.franceinfo.fr/titres.rss')]},
    'energie': {'nom': 'Énergie', 'profil': 'électricité énergie chauffage électrique facture contrat Tempo heures creuses '
                                           'prix du kWh nucléaire renouvelable compteur Linky',
                'flux': [('Le Monde', 'https://www.lemonde.fr/energies/rss_full.xml'),
                         ('Connaissance des énergies', 'https://www.connaissancedesenergies.org/rss.xml')]},
    'voiture': {'nom': 'Voiture électrique', 'profil': 'voiture électrique recharge borne batterie autonomie automobile véhicule',
                'flux': [('Bevolt', 'https://www.bevolt.com/feed/rss/')]},
    'numerique': {'nom': 'Numérique', 'profil': 'informatique ordinateur logiciel intelligence artificielle internet smartphone '
                                                'jeux vidéo Linux programmation',
                  'flux': [('Numerama', 'https://www.numerama.com/feed/'), ('01net', 'https://www.01net.com/feed/'),
                           ('Le Monde', 'https://www.lemonde.fr/pixels/rss_full.xml')]},
    'sciences': {'nom': 'Sciences', 'profil': 'science recherche espace astronomie physique biologie découverte',
                 'flux': [('Le Monde', 'https://www.lemonde.fr/sciences/rss_full.xml'),
                          ('Futura', 'https://www.futura-sciences.com/rss/actualites.xml')]},
    'sante': {'nom': 'Santé', 'profil': 'santé médecine médecin maladie traitement médicament hôpital bien-être',
              'flux': [('Le Monde', 'https://www.lemonde.fr/sante/rss_full.xml'),
                       ('franceinfo', 'https://www.franceinfo.fr/sante.rss')]},
    'argent': {'nom': 'Argent', 'profil': 'budget impôts épargne banque crédit retraite assurance pouvoir d\'achat',
               'flux': [('Le Monde', 'https://www.lemonde.fr/argent/rss_full.xml')]},
    'logement': {'nom': 'Logement', 'profil': 'logement appartement maison loyer locataire propriétaire immobilier travaux',
                 'flux': [('Le Monde', 'https://www.lemonde.fr/immobilier/rss_full.xml')]},
    'economie': {'nom': 'Économie', 'profil': 'économie entreprises emploi inflation croissance marchés industrie',
                 'flux': [('Le Monde', 'https://www.lemonde.fr/economie/rss_full.xml'),
                          ('franceinfo', 'https://www.franceinfo.fr/economie.rss')]},
    'travail': {'nom': 'Travail', 'profil': 'travail emploi salaire télétravail carrière entreprise salarié',
                'flux': [('Le Monde', 'https://www.lemonde.fr/emploi/rss_full.xml')]},
    'planete': {'nom': 'Planète', 'profil': 'climat environnement écologie biodiversité météo réchauffement nature',
                'flux': [('Le Monde', 'https://www.lemonde.fr/planete/rss_full.xml')]},
    'monde': {'nom': 'Monde', 'profil': 'international géopolitique pays étranger diplomatie conflit',
              'flux': [('Le Monde', 'https://www.lemonde.fr/international/rss_full.xml'),
                       ('franceinfo', 'https://www.franceinfo.fr/monde.rss')]},
    'politique': {'nom': 'Politique', 'profil': 'politique gouvernement élections parlement lois partis',
                  'flux': [('Le Monde', 'https://www.lemonde.fr/politique/rss_full.xml')]},
    'sport': {'nom': 'Sport', 'profil': 'sport football rugby tennis cyclisme course match championnat',
              'flux': [('Le Monde', 'https://www.lemonde.fr/sport/rss_full.xml'),
                       ('franceinfo', 'https://www.franceinfo.fr/sports.rss')]},
    'culture': {'nom': 'Culture', 'profil': 'culture cinéma musique livres séries théâtre exposition',
                'flux': [('Le Monde', 'https://www.lemonde.fr/culture/rss_full.xml'),
                         ('franceinfo', 'https://www.franceinfo.fr/culture.rss')]},
    'education': {'nom': 'Éducation', 'profil': 'école enfants éducation études université enseignants parents',
                  'flux': [('Le Monde', 'https://www.lemonde.fr/education/rss_full.xml')]},
    'cuisine': {'nom': 'Cuisine', 'profil': 'cuisine recette gastronomie restaurant alimentation',
                'flux': [('Le Monde', 'https://www.lemonde.fr/cuisine/rss_full.xml')]},
    'voyage': {'nom': 'Voyage', 'profil': 'voyage vacances tourisme randonnée montagne destination',
               'flux': [('Le Monde', 'https://www.lemonde.fr/voyage/rss_full.xml')]},
}
ALLOWED_HOSTS = {parse.urlsplit(url).hostname for theme in THEMES.values() for _name, url in theme['flux']}


@dataclass
class Article:
    title: str
    summary: str
    link: str
    source: str
    theme: str
    published: datetime | None = None
    score: float = 0.0
    reason: str = ''              # le souvenir qui a fait retenir l'article
    vector: list[float] = field(default_factory=list, repr=False)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, timeout: float = 15) -> bytes:
    """Télécharge un flux du catalogue : HTTPS, hôte connu, pas de redirection, taille bornée."""
    parts = parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError('flux hors catalogue')
    opener = request.build_opener(_NoRedirect)
    req = request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/xml'})
    with opener.open(req, timeout=timeout) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError('flux trop volumineux')
    return data


def clean(text: Any, limit: int) -> str:
    """Texte simple : sans balises, sans entités, sans caractères de contrôle."""
    text = re.sub(r'<[^>]{0,2000}>', ' ', str(text or ''))
    text = html.unescape(text)
    text = ''.join(ch if ch.isprintable() else ' ' for ch in text)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0] + '…'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1].lower()


def parse_feed(raw: bytes, source: str, theme: str) -> list[Article]:
    """Articles d'un flux RSS ou Atom. Un flux piégé (entités XML) est refusé."""
    if b'<!DOCTYPE' in raw[:4096].upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('flux refusé (déclarations XML interdites)')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('flux illisible') from exc
    articles = []
    for item in root.iter():
        if _local(item.tag) not in ('item', 'entry'):
            continue
        fields: dict[str, str] = {}
        for child in item:
            name = _local(child.tag)
            if name == 'link' and child.get('href') and not fields.get('link'):
                fields['link'] = child.get('href', '')
            elif name not in fields and (child.text or '').strip():
                fields[name] = child.text or ''
        title = clean(fields.get('title'), 180)
        link = (fields.get('link') or '').strip()
        if not title or parse.urlsplit(link).scheme != 'https' or len(link) > 500:
            continue
        published = None
        stamp = fields.get('pubdate') or fields.get('published') or fields.get('updated') or fields.get('date')
        if stamp:
            try:
                published = parsedate_to_datetime(stamp)
            except (TypeError, ValueError):
                try:
                    published = datetime.fromisoformat(stamp.strip().replace('Z', '+00:00'))
                except ValueError:
                    published = None
            if published is not None and published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        articles.append(Article(title, clean(fields.get('description') or fields.get('summary') or '', 320),
                                link, source, theme, published))
    return articles


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class Newsroom:
    def __init__(self, client: Any, embed_model: str | None, interests: list[str],
                 fetcher: Callable[[str], bytes] = fetch):
        self.client, self.embed_model, self.fetcher = client, embed_model, fetcher
        self.interests = [text for text in interests if text.strip()][:60]
        self.errors: list[str] = []
        self._interest_vectors: list[list[float]] | None = None

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts or self.client is None or not self.embed_model:
            return None
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), 32):
                vectors += self.client.embed(self.embed_model, texts[start:start + 32])
            return vectors
        except OllamaError:
            return None

    def _interests(self) -> list[list[float]] | None:
        if self._interest_vectors is None and self.interests:
            self._interest_vectors = self._embed(self.interests)
        return self._interest_vectors

    def themes(self) -> list[tuple[str, float, str]]:
        """Rubriques retenues : (clé, proximité, souvenir qui la justifie). « À la une » toujours."""
        chosen = [('general', 1.0, '')]
        vectors = self._interests()
        keys = [k for k in THEMES if k != 'general']
        profiles = self._embed([THEMES[k]['profil'] for k in keys]) if vectors else None
        if not vectors or not profiles:
            return chosen
        scored = []
        for key, profile in zip(keys, profiles):
            best = max(range(len(vectors)), key=lambda i: cosine(profile, vectors[i]))
            score = cosine(profile, vectors[best])
            if score >= THEME_THRESHOLD:
                scored.append((key, round(score, 3), self.interests[best]))
        return chosen + sorted(scored, key=lambda t: -t[1])[:MAX_THEMES]

    def collect(self, now: datetime) -> list[Article]:
        """Articles récents des rubriques retenues (un flux en panne n'empêche pas les autres)."""
        if now.tzinfo is None:
            now = now.astimezone()
        seen, articles = set(), []
        for key, _score, _why in self.themes():
            for source, url in THEMES[key]['flux']:
                try:
                    found = parse_feed(self.fetcher(url), source, key)
                except (ValueError, OSError, error.URLError) as exc:
                    self.errors.append(f'{source} ({THEMES[key]["nom"]}) : {type(exc).__name__}')
                    continue
                for article in found[:25]:
                    if article.published is not None and now - article.published > MAX_AGE:
                        continue
                    if article.link not in seen and article.title.lower() not in seen:
                        seen.update((article.link, article.title.lower()))
                        articles.append(article)
        return articles

    def select(self, now: datetime, headlines: int = 3, personal: int = 5) -> dict[str, list[Article]]:
        """{'une': les titres du jour, 'pour_toi': les articles choisis d'après les souvenirs}."""
        articles = self.collect(now)
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        general = sorted((a for a in articles if a.theme == 'general'), key=lambda a: a.published or oldest, reverse=True)
        front, used = [], set()
        for article in general:       # un titre par source d'abord, pour varier…
            if article.source not in used and len(front) < headlines:
                front.append(article)
                used.add(article.source)
        for article in general:       # … puis les plus récents qui restent
            if article not in front and len(front) < headlines:
                front.append(article)
        others = [a for a in articles if a not in front]
        vectors = self._interests()
        embedded = self._embed([f'{a.title}. {a.summary}' for a in others]) if vectors and others else None
        chosen: list[Article] = []
        if vectors and embedded:
            for article, vector in zip(others, embedded):
                best = max(range(len(vectors)), key=lambda i: cosine(vector, vectors[i]))
                article.score, article.reason = cosine(vector, vectors[best]), self.interests[best]
            # Les rubriques retenues d'après les souvenirs se partagent les places, à tour de rôle,
            # chacune donnant ses articles les plus proches de ce que NEXUS sait de la personne.
            order = [key for key, _score, _why in self.themes() if key != 'general']
            ranked = {key: sorted((a for a in others if a.theme == key), key=lambda a: -a.score) for key in order}
            extra = [a for a in others if a.theme == 'general' and a.score >= ARTICLE_THRESHOLD]
            ranked['general'] = sorted(extra, key=lambda a: -a.score)
            turn = 0
            while len(chosen) < personal and any(ranked.values()):
                key = (order + ['general'])[turn % (len(order) + 1)]
                if ranked.get(key):
                    chosen.append(ranked[key].pop(0))
                turn += 1
        return {'une': front, 'pour_toi': chosen}
