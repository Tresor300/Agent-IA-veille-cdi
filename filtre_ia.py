"""Filtre IA de la version CDI (Gemini) : étape 1 sur le titre, étape 2 sur l'offre complète.

Frontière fixée le 8 novembre 2026 (voir le dossier de projet) :
  OUI : développement, DevOps / Cloud / SRE / plateforme / DevSecOps, QA et test automatisé,
        cybersécurité (SOC, pentest, GRC), administration systèmes et réseaux ;
  NON : toute la data (analyst, BI, scientist, engineer), support / helpdesk / technicien,
        chef de projet / PO / PMO / AMOA ; tout contrat autre que le CDI.
L'étape 2 donne aussi le niveau (junior / confirmé) et le département, pour classer le message.
"""
import json
import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv()
CLE = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
BASE = "https://generativelanguage.googleapis.com/v1beta"
MODELES = ["models/gemini-flash-lite-latest", "models/gemini-flash-latest"]
TAILLE_PAQUET = 50            # étape 1 : titres par appel
TAILLE_PAQUET_OFFRES = 10     # étape 2 : offres complètes par appel
dernier = [None]              # modèle qui a répondu en dernier : on commence par lui

# ---------- Règles sûres, appliquées par le code (pas par l'IA) ----------
# Contrat autre que le CDI annoncé dans le titre
AUTRE_CONTRAT = re.compile(
    r"\b(stage|stagiaire|intern|internship|altern\w*|apprenti\w*|apprenticeship|contrat pro\w*|work[- ]?study|"
    r"cdd|freelance|free-lance|ind[ée]pendant|int[ée]rim\w*|v\.?i\.?e|mission temporaire)\b", re.I)
DEPARTEMENTS_IDF = {"75", "77", "78", "91", "92", "93", "94", "95"}


def zone(departement):
    """« Île-de-France » ou « Reste de la France » à partir du code de département donné par l'IA."""
    return "Île-de-France" if (departement or "").strip()[:2] in DEPARTEMENTS_IDF else "Reste de la France"


class GeminiIndisponible(Exception):
    """Gemini n'a pas pu juger : les offres doivent être rejugées plus tard."""


def _appeler(texte, schema, timeout):
    """Envoie une demande à Gemini et renvoie la réponse JSON décodée."""
    ordre = sorted(MODELES, key=lambda m: m != dernier[0])
    for modele in ordre:
        for _ in range(2):
            try:
                r = requests.post(f"{BASE}/{modele}:generateContent", headers=CLE, timeout=timeout, json={
                    "contents": [{"parts": [{"text": texte}]}],
                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                         "responseSchema": schema},
                })
            except requests.RequestException:
                print(f"   … {modele.split('/')[-1]} ne répond pas")
                continue
            if not r.ok:
                if r.status_code in (429, 500, 503):
                    time.sleep(10)               # surcharge temporaire : petite pause, on réessaie
                    continue
                break                            # autre erreur : modèle suivant
            try:
                reponse = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                dernier[0] = modele
                return reponse
            except (KeyError, IndexError, ValueError, TypeError):
                print(f"   réponse illisible de {modele.split('/')[-1]}, nouvel essai")
    raise GeminiIndisponible("Gemini n'a pas pu juger ce paquet")


# ---------- Étape 1 : tri rapide sur le titre ----------
CONSIGNE = (
    "Tu tries des liens trouvés sur la page carrière d'une entreprise, pour un jeune ingénieur qui cherche un CDI "
    "dans l'un de ces métiers : développement logiciel, web ou mobile (backend, frontend, fullstack, Java, Python, "
    "embarqué), DevOps, cloud, SRE, ingénieur plateforme, DevSecOps, QA et test automatisé, cybersécurité "
    "(SOC, pentest, GRC), administration systèmes et réseaux informatiques.\n"
    "IMPORTANT : la plupart des titres n'indiquent pas le contrat ; un titre sans contrat n'est PAS une raison "
    "de dire non (le contrat sera vérifié ensuite).\n"
    "Pour chaque texte, réponds :\n"
    "- \"oui\" : un des métiers de la liste ;\n"
    "- \"non\" : autre métier, Y COMPRIS toute la data (data analyst, data engineer, data scientist, BI, "
    "big data), le support ou helpdesk, technicien, chef de projet, Product Owner, Product Manager, PMO, "
    "AMOA, commercial, RH, finance, industrie ; OU ce n'est pas une offre (lien de navigation, « Voir toutes "
    "les offres », rubrique, nom d'équipe, page entreprise, « Postuler », « Candidature spontanée ») ;\n"
    "- \"doute\" : texte trop vague pour connaître le métier (« Ingénieur H/F », « Consultant IT », "
    "« Voir l'offre », « En savoir plus »).\n"
    "Donne un objet par texte, avec son numéro.\n\nTextes :\n"
)
SCHEMA = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"i": {"type": "INTEGER"},
                             "verdict": {"type": "STRING", "enum": ["oui", "non", "doute"]},
                             "raison": {"type": "STRING"}},
              "required": ["i", "verdict", "raison"]},
}


def juger(titres):
    """Étape 1. Renvoie une liste de (verdict, raison) alignée sur `titres`."""
    resultats = []
    for debut in range(0, len(titres), TAILLE_PAQUET):
        paquet = titres[debut:debut + TAILLE_PAQUET]
        texte = CONSIGNE + "\n".join(f"{i}. {t}" for i, t in enumerate(paquet))
        par_numero = {int(v["i"]): (v["verdict"], v.get("raison", "")) for v in _appeler(texte, SCHEMA, 120)}
        for i in range(len(paquet)):
            verdict, raison = par_numero.get(i, ("doute", "titre oublié par le modèle"))
            resultats.append((verdict if verdict in ("oui", "non", "doute") else "doute", raison))
        time.sleep(4)          # respect du quota gratuit
    return resultats


# ---------- Étape 2 : jugement sur l'offre complète ----------
CONSIGNE_OFFRES = (
    "Tu es recruteur. Un jeune ingénieur cherche un CDI en France dans un métier technique de l'informatique.\n"
    "MÉTIER — OUI : développement (backend, frontend, fullstack, mobile, embarqué, Java, Python…), DevOps, cloud, "
    "SRE, ingénieur plateforme, DevSecOps, QA et test automatisé, cybersécurité (SOC, pentest, GRC), "
    "administration systèmes et réseaux informatiques.\n"
    "MÉTIER — NON, sans exception : toute la data (data analyst, data engineer, data scientist, BI, big data, "
    "même avec du code), support, helpdesk, technicien, chef de projet ou PMO même informatique, Product Owner, "
    "Product Manager, AMOA/MOA, commercial, management, industrie, maintenance matérielle.\n"
    "CONTRAT : seul le CDI est retenu. CDD, stage, alternance, apprentissage, freelance, intérim, VIE = \"non\". "
    "Les sites carrière n'écrivent pas toujours « CDI » : un emploi permanent sans autre contrat indiqué est "
    "considéré comme un CDI. Un poste hors de France = \"non\". Si la page n'est pas UNE offre précise "
    "(liste d'offres, rubrique, présentation d'équipe, candidature spontanée) = \"non\".\n"
    "\"à vérifier\" seulement si la description est vide ou illisible (menu, cookies) et que le titre ne suffit "
    "pas, ou si tu hésites vraiment sur le métier.\n"
    "NIVEAU : \"junior\" si l'offre vise un débutant, un jeune diplômé ou 0 à 2 ans d'expérience ; \"confirmé\" "
    "si elle demande 3 ans ou plus, ou si le titre dit senior, confirmé, expérimenté, lead, expert, architecte, "
    "principal, manager ; \"non précisé\" sinon.\n"
    "LIEU : la ville du poste et son département en 2 chiffres (ex. Paris = 75, Nanterre = 92, Lyon = 69, "
    "Nantes = 44 ; « 2A »/« 2B » pour la Corse) ; département \"?\" si le lieu n'est pas indiqué ; "
    "\"étranger\" si le poste est hors de France.\n"
    "Pour \"oui\", la raison cite la mission et l'outil trouvés dans l'offre (ex. « développer des API "
    "Java Spring »). Raison courte, une phrase.\n"
    "Donne un objet par offre, avec son numéro.\n\nOffres :\n"
)
SCHEMA_OFFRES = {
    "type": "ARRAY",
    "items": {"type": "OBJECT",
              "properties": {"i": {"type": "INTEGER"},
                             "verdict": {"type": "STRING", "enum": ["oui", "non", "à vérifier"]},
                             "raison": {"type": "STRING"},
                             "niveau": {"type": "STRING", "enum": ["junior", "confirmé", "non précisé"]},
                             "ville": {"type": "STRING"},
                             "departement": {"type": "STRING"}},
              "required": ["i", "verdict", "raison", "niveau", "ville", "departement"]},
}


def juger_offres(offres):
    """Étape 2. `offres` : liste de (titre, entreprise, description), au plus TAILLE_PAQUET_OFFRES.
    Renvoie une liste alignée de dictionnaires {verdict, raison, niveau, ville, departement} ;
    None si le modèle a oublié une offre (elle sera rejugée au passage suivant)."""
    texte = CONSIGNE_OFFRES + "\n\n".join(
        f"--- Offre {i} ---\nTitre : {t}\nEntreprise : {e or '?'}\nDescription : {d or '(vide)'}"
        for i, (t, e, d) in enumerate(offres))
    par_numero = {int(v["i"]): v for v in _appeler(texte, SCHEMA_OFFRES, 60)}
    time.sleep(4)              # respect du quota gratuit
    resultats = []
    for i, (titre, _, _) in enumerate(offres):
        v = par_numero.get(i)
        if v and v["verdict"] != "non" and AUTRE_CONTRAT.search(titre or ""):
            v = {**v, "verdict": "non", "raison": "contrat autre que CDI annoncé dans le titre"}
        if v and v["verdict"] != "non" and v.get("departement", "").lower().startswith("étranger"):
            v = {**v, "verdict": "non", "raison": "poste hors de France"}
        resultats.append(v)
    return resultats


if __name__ == "__main__":
    # Test de la frontière sur des offres inventées : aucune base, aucun message
    exemples = [
        ("Développeur Java Spring H/F", "Exemple SA", "CDI à La Défense (92). Vous développerez des API REST en "
         "Java 17 / Spring Boot. Profil : jeune diplômé ou première expérience."),
        ("Ingénieur DevOps Senior", "Exemple SAS", "Poste en CDI basé à Lyon. 5 ans d'expérience minimum sur "
         "Kubernetes, Terraform et GitLab CI."),
        ("Data Engineer", "Exemple SA", "CDI à Paris. Pipelines Spark et Airflow, Python."),
        ("Analyste SOC N1", "Exemple Cyber", "CDI à Rennes (35). Surveillance des alertes SIEM, débutants acceptés."),
        ("Technicien support informatique", "Exemple SA", "CDI à Nantes. Assistance aux utilisateurs."),
        ("Stage - Développeur Python", "Exemple SA", "Stage de 6 mois à Paris."),
        ("Administrateur systèmes et réseaux", "Exemple SA", "CDD de 12 mois à Lille."),
        ("Ingénieur QA automatisation", "Exemple SA", ""),
        ("Chef de projet informatique", "Exemple SA", "CDI à Paris, pilotage de projets SI."),
        ("Software Engineer", "Exemple Inc", "Permanent position in Berlin, Germany. Go and Kubernetes."),
    ]
    print(f"{'Titre':38} {'verdict':11} {'niveau':12} zone (ville)")
    for (t, _, _), v in zip(exemples, juger_offres(exemples)):
        if v is None:
            print(f"{t:38} oublié par le modèle")
            continue
        print(f"{t:38} {v['verdict']:11} {v['niveau']:12} {zone(v['departement'])} ({v['ville']}, "
              f"{v['departement']})\n{'':38} └ {v['raison']}")
    print(f"[modèle : {dernier[0]}]")