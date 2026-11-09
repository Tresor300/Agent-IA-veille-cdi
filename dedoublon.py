"""Reconnaît une offre déjà envoyée, même venue d'une autre source ou de l'autre moteur.

Deux offres sont des doublons si leurs titres simplifiés sont identiques ET si elles partagent
un mot du nom de l'entreprise (« Michelin » / « Manufacture française des pneumatiques Michelin »)
ou le même code postal (« Château de Versailles » / « Ministère de la Culture », 78000).
Un titre seul ne suffit pas : « Technicien informatique » existe chez des centaines d'entreprises.
"""
import re
import unicodedata

# Mots retirés du titre : contrat, genre, mots outils
MOTS_TITRE_IGNORES = {
    "alternance", "alternant", "alternante", "apprentissage", "apprenti", "apprentie", "contrat", "pro",
    "professionnalisation", "work", "study", "apprenticeship", "en", "de", "du", "des", "la", "le", "les",
    "et", "a", "au", "aux", "un", "une", "h", "f", "x", "m", "w", "d", "nb", "hf", "fh",
}
# Mots trop courants dans les noms d'entreprise pour prouver qu'il s'agit de la même
MOTS_ENTREPRISE_IGNORES = {
    "france", "francaise", "francais", "groupe", "group", "societe", "entreprise", "services", "service",
    "precisee", "manufacture", "international", "solutions", "consulting", "conseil", "holding",
    "etablissement", "public", "direction", "generale", "ministere", "sarl", "sas",
}


def _mots(texte):
    sans_accents = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", sans_accents)


def cle_titre(titre):
    return " ".join(m for m in _mots(titre) if m not in MOTS_TITRE_IGNORES)


def mots_entreprise(entreprise):
    return {m for m in _mots(entreprise) if len(m) >= 4 and m not in MOTS_ENTREPRISE_IGNORES}


def code_postal(lieu):
    trouve = re.search(r"\b\d{5}\b", lieu or "")
    return trouve.group(0) if trouve else None


class Registre:
    """Mémoire des offres envoyées : on y ajoute chaque offre envoyée, on l'interroge avant d'envoyer."""

    def __init__(self, deja_envoyees=()):
        self.offres = []
        for titre, entreprise, lieu in deja_envoyees:
            self.ajouter(titre, entreprise, lieu)

    def ajouter(self, titre, entreprise, lieu=None):
        self.offres.append((cle_titre(titre), mots_entreprise(entreprise), code_postal(lieu)))

    def est_doublon(self, titre, entreprise, lieu=None):
        cle = cle_titre(titre)
        if not cle:                       # titre vide une fois simplifié : on ne prend pas de risque
            return False
        mots, cp = mots_entreprise(entreprise), code_postal(lieu)
        return any(cle == c and ((mots & m) or (cp and cp == p)) for c, m, p in self.offres)


def offres_deja_envoyees(con):
    """Offres déjà envoyées par les deux moteurs (titre, entreprise, lieu)."""
    deja = []
    requetes = [
        "SELECT titre, entreprise, lieu FROM offres_capteur WHERE envoyee = 1 AND verdict IN ('oui', 'doute')",
        "SELECT titre, entreprise, NULL FROM offres WHERE envoyee = 1 AND verdict2 IN ('oui', 'à vérifier')",
    ]
    for requete in requetes:
        try:
            deja += con.execute(requete).fetchall()
        except Exception:                  # table ou colonne absente : l'autre moteur n'a pas encore tourné
            pass
    return deja