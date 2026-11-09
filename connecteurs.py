"""Accès aux offres : interfaces publiques officielles des plateformes de recrutement, et règles d'accès.

Règles (voir le dossier de projet) :
- on n'utilise que les interfaces publiques DOCUMENTÉES (Greenhouse, Lever, SmartRecruiters, Recruitee) ;
- Workday n'a pas d'interface documentée : sa page publique est lue comme un visiteur, filtrée sur « alternance » ;
- on respecte le fichier robots.txt des sites dont on lit les pages.
"""
import html
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from urllib.robotparser import RobotFileParser

import requests

AGENT = "AgentVeilleAlternance/1.0 (projet etudiant, 12 passages par jour)"
RECHERCHES = ["alternance", "alternant", "apprentissage", "apprenti", "apprenticeship", "work-study"]


class AccesImpossible(Exception):
    """L'interface ou la page n'a pas pu être lue : le site est compté en erreur pour ce passage."""


# ---------- Reconnaître la plateforme à partir de l'adresse ----------
def detecter_acces(url):
    """Renvoie (plateforme, identifiant) ; ("page", None) si l'on doit lire la page elle-même."""
    u = urlparse(url)
    hote, morceaux = u.netloc.lower(), [m for m in u.path.split("/") if m]
    if hote in ("job-boards.greenhouse.io", "boards.greenhouse.io") and morceaux:
        return "greenhouse", morceaux[0]
    if hote == "jobs.lever.co" and morceaux:
        return "lever", morceaux[0]
    if hote in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com") and morceaux:
        return "smartrecruiters", morceaux[0]
    if hote.endswith(".recruitee.com"):
        return "recruitee", hote.split(".")[0]
    return "page", None


def adresse_a_visiter(url):
    """Workday : si l'adresse n'a aucun filtre, on ajoute la recherche « alternance ».
    Si elle a déjà des filtres (pays, type de contrat), on la garde telle quelle : ajouter une
    recherche par mot-clé exclurait des offres titrées « Apprenti » ou « Apprenticeship »."""
    u = urlparse(url)
    if ("myworkdayjobs.com" in u.netloc or "myworkdaysite.com" in u.netloc) and not u.query:
        return urlunparse(u._replace(query=urlencode([("q", "alternance")])))
    return url


# ---------- Interfaces officielles ----------
def _texte(contenu_html):
    sans_balises = re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(contenu_html or "")))
    return " ".join(sans_balises.split())[:3000]


def _get(url, **params):
    try:
        r = requests.get(url, params=params, timeout=30, headers={"User-Agent": AGENT, "Accept": "application/json"})
    except requests.RequestException as e:
        raise AccesImpossible(type(e).__name__)
    if not r.ok:
        raise AccesImpossible(f"code HTTP {r.status_code}")
    return r.json()


def _greenhouse(jeton):
    donnees = _get(f"https://boards-api.greenhouse.io/v1/boards/{jeton}/jobs", content="true")
    return {o["absolute_url"]: (o["title"], _texte(f"{(o.get('location') or {}).get('name', '')} — {o.get('content', '')}"))
            for o in donnees.get("jobs", [])}


def _lever(societe):
    donnees = _get(f"https://api.lever.co/v0/postings/{societe}", mode="json")
    offres = {}
    for o in donnees:
        cat = o.get("categories") or {}
        listes = " ".join(f"{l.get('text', '')} {_texte(l.get('content', ''))}" for l in o.get("lists", []))
        offres[o["hostedUrl"]] = (o["text"], _texte(f"Contrat : {cat.get('commitment', '?')} — Lieu : {cat.get('location', '?')} — "
                                                     f"{o.get('descriptionPlain', '')} {listes}"))
    return offres


def _smartrecruiters(societe, deja_vues=frozenset()):
    """Seulement les offres trouvées par la recherche du site sur les mots de l'alternance.
    La description (un appel par offre) n'est demandée que pour les offres jamais vues."""
    trouvees = {}
    for mot in RECHERCHES:
        for o in _get(f"https://api.smartrecruiters.com/v1/companies/{societe}/postings", q=mot, limit=100).get("content", []):
            trouvees[o["id"]] = o
    offres = {}
    for ident, o in trouvees.items():
        adresse = f"https://jobs.smartrecruiters.com/{societe}/{ident}"
        if adresse in deja_vues:
            offres[adresse] = (o["name"], "")
            continue
        try:
            detail = _get(f"https://api.smartrecruiters.com/v1/companies/{societe}/postings/{ident}")
            sections = (detail.get("jobAd") or {}).get("sections") or {}
            description = " ".join((s or {}).get("text", "") for s in sections.values())
        except AccesImpossible:
            description = ""
        lieu = (o.get("location") or {}).get("city", "?")
        contrat = (o.get("typeOfEmployment") or {}).get("label", "?")
        offres[adresse] = (
            o["name"], _texte(f"Contrat : {contrat} — Lieu : {lieu} — {description}"))
    return offres


def _recruitee(societe):
    donnees = _get(f"https://{societe}.recruitee.com/api/offers/")
    return {o["careers_url"]: (o["title"], _texte(f"Contrat : {o.get('employment_type_code', '?')} — "
                                                  f"Lieu : {o.get('location', '?')} — {o.get('description', '')} "
                                                  f"{o.get('requirements', '')}"))
            for o in donnees.get("offers", [])}


LECTEURS = {"greenhouse": _greenhouse, "lever": _lever, "smartrecruiters": _smartrecruiters, "recruitee": _recruitee}


def lire_interface(plateforme, identifiant, deja_vues=frozenset()):
    """Renvoie {adresse de l'offre: (titre, description)} ; lève AccesImpossible en cas d'échec.
    deja_vues : adresses déjà en base, pour éviter de redemander leur description."""
    try:
        if plateforme == "smartrecruiters":
            return _smartrecruiters(identifiant, deja_vues)
        return LECTEURS[plateforme](identifiant)
    except AccesImpossible:
        raise
    except Exception as e:                       # réponse inattendue : on la traite comme une erreur du site
        raise AccesImpossible(f"réponse inattendue ({type(e).__name__})")


# ---------- robots.txt ----------
_robots = {}


def autorise(url):
    """Vrai si le robots.txt du site autorise la lecture de cette page (vrai aussi s'il est illisible)."""
    u = urlparse(url)
    racine = f"{u.scheme}://{u.netloc}"
    if racine not in _robots:
        regles = RobotFileParser()
        try:
            r = requests.get(f"{racine}/robots.txt", timeout=10, headers={"User-Agent": AGENT})
            regles.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            regles.parse([])
        _robots[racine] = regles
    return _robots[racine].can_fetch(AGENT, url)