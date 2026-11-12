"""Accès aux offres : interfaces publiques officielles des plateformes de recrutement, et règles d'accès.

Règles (voir le dossier de projet) :
- on n'utilise que les interfaces publiques DOCUMENTÉES (Greenhouse, Lever, SmartRecruiters, Recruitee) ;
- Workday n'a pas d'interface documentée : sa page publique est lue comme un visiteur (filtres pays / contrat
  posés dans l'adresse par l'utilisateur) ;
- on respecte le fichier robots.txt des sites dont on lit les pages.
"""
import html
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from urllib.robotparser import RobotFileParser

import requests

AGENT = "AgentVeilleCDI/1.0 (projet etudiant, 12 passages par jour)"
MAX_SMARTRECRUITERS = 1000      # offres lues au plus par entreprise (10 appels de 100)


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
    """Version CDI : l'adresse est utilisée telle quelle (plus de recherche « alternance » ajoutée).
    Les filtres utiles (pays, type de contrat) sont posés dans l'adresse du fichier pages_a_surveiller."""
    return url


# ---------- Interfaces officielles ----------
# Lieu en France (ou non précisé) : les autres offres ne sont même pas proposées à l'IA
FRANCE = re.compile(r"france|paris|lyon|marseille|toulouse|nantes|bordeaux|lille|rennes|nice|montpellier|grenoble|"
                    r"strasbourg|sophia|la d[ée]fense|nanterre|boulogne|levallois|issy|saint-denis|courbevoie|"
                    r"[îi]le-de-france|\bfr\b", re.I)


def en_france(lieu):
    return not (lieu or "").strip() or bool(FRANCE.search(lieu))



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
            for o in donnees.get("jobs", []) if en_france((o.get("location") or {}).get("name"))}


def _lever(societe):
    donnees = _get(f"https://api.lever.co/v0/postings/{societe}", mode="json")
    offres = {}
    for o in donnees:
        cat = o.get("categories") or {}
        if not en_france(cat.get("location")):
            continue
        listes = " ".join(f"{l.get('text', '')} {_texte(l.get('content', ''))}" for l in o.get("lists", []))
        offres[o["hostedUrl"]] = (o["text"], _texte(f"Contrat : {cat.get('commitment', '?')} — Lieu : {cat.get('location', '?')} — "
                                                     f"{o.get('descriptionPlain', '')} {listes}"))
    return offres


def _smartrecruiters(societe, deja_vues=frozenset()):
    """Toutes les offres situées en France (liste paginée, sans description).
    La description n'est pas demandée ici : le veilleur ouvre la page publique de l'offre
    seulement si son titre passe l'étape 1 (une seule lecture par offre)."""
    offres = {}
    for debut in range(0, MAX_SMARTRECRUITERS, 100):
        page = _get(f"https://api.smartrecruiters.com/v1/companies/{societe}/postings", limit=100, offset=debut)
        contenu = page.get("content", [])
        for o in contenu:
            lieu = o.get("location") or {}
            if (lieu.get("country") or "").lower() != "fr":
                continue
            offres[f"https://jobs.smartrecruiters.com/{societe}/{o['id']}"] = (o["name"], None)
        if len(contenu) < 100:
            break
    return offres


def _recruitee(societe):
    donnees = _get(f"https://{societe}.recruitee.com/api/offers/")
    return {o["careers_url"]: (o["title"], _texte(f"Contrat : {o.get('employment_type_code', '?')} — "
                                                  f"Lieu : {o.get('location', '?')} — {o.get('description', '')} "
                                                  f"{o.get('requirements', '')}"))
            for o in donnees.get("offers", []) if en_france(o.get("location"))}


def description_smartrecruiters(url):
    """Description et lieu d'une offre SmartRecruiters, par l'interface officielle (un appel par offre)."""
    societe, ident = url.rstrip("/").split("/")[-2:]
    try:
        detail = _get(f"https://api.smartrecruiters.com/v1/companies/{societe}/postings/{ident}")
    except AccesImpossible:
        return ""
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    texte = " ".join((s or {}).get("text", "") for s in sections.values())
    lieu = detail.get("location") or {}
    ville = " ".join(x for x in (lieu.get("city"), lieu.get("postalCode"), lieu.get("region")) if x)
    contrat = (detail.get("typeOfEmployment") or {}).get("label", "?")
    return _texte(f"Contrat : {contrat} — Lieu : {ville or '?'} — {texte}")


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