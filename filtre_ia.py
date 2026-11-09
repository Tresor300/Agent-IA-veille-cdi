import os, json, time
import requests
from dotenv import load_dotenv

load_dotenv()
CLE = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
BASE = "https://generativelanguage.googleapis.com/v1beta"
MODELES = ["models/gemini-flash-latest", "models/gemini-flash-lite-latest"]
TAILLE_PAQUET = 50            # étape 1 : titres par appel
TAILLE_PAQUET_OFFRES = 10     # étape 2 : offres complètes par appel
dernier = [None]              # modèle qui a répondu en dernier : on commence par lui


class GeminiIndisponible(Exception):
    """Gemini n'a pas pu juger : les offres doivent être rejugées plus tard."""


def _appeler(texte, schema, timeout):
    """Envoie une demande à Gemini et renvoie la réponse JSON décodée."""
    ordre = sorted(MODELES, key=lambda m: m != dernier[0])
    for modele in ordre:
        for essai in range(2):
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
    "Tu tries des offres pour un élève ingénieur qui cherche une ALTERNANCE dans un MÉTIER TECHNIQUE "
    "de l'informatique au sens large : développement logiciel ou web, DevOps, cloud, data (data analyst, "
    "data engineer, BI), IA, cybersécurité, administration systèmes et réseaux INFORMATIQUES, support et "
    "technicien informatique, ERP, test/QA, chef de projet / product owner / business analyst en SI.\n"
    "IMPORTANT : toutes ces offres proviennent d'un service public dédié à l'alternance. "
    "L'absence du mot « alternance » dans le titre n'est donc PAS une raison de douter. "
    "Un CDI n'est une alternance QUE si le titre mentionne aussi alternance, apprentissage ou apprenti "
    "(ex. « Alternance CDI » = oui ; « Technicien réseau CDI » = non).\n"
    "Pour chaque titre, réponds :\n"
    "- \"oui\" : métier technique informatique de la liste ci-dessus ;\n"
    "- \"non\" : autre métier (BTP, réseaux électriques, d'eau ou de chaleur, voirie, électricité, industrie, "
    "mécanique, aéronautique matérielle, RH, recrutement, vente, commercial même dans l'IT, marketing digital, "
    "community management, finance, comptabilité…) OU titre qui indique EXPLICITEMENT un CDI classique, "
    "un CDD, un stage ou de l'intérim ;\n"
    "- \"doute\" : titre trop vague pour connaître le domaine (ex. « Chargé de projets » seul), "
    "ou métier voisin (informatique industrielle, automatisme, webmaster).\n"
    "Donne un objet par titre, avec son numéro.\n\nTitres :\n"
)

CONSIGNE_CARRIERE = (
    "Tu tries des liens trouvés sur la page carrière d'une entreprise, pour un élève ingénieur qui cherche une "
    "ALTERNANCE dans un MÉTIER TECHNIQUE de l'informatique : développement logiciel ou web, DevOps, cloud, data, "
    "BI, IA, cybersécurité, administration systèmes et réseaux INFORMATIQUES, support informatique, ERP, QA.\n"
    "IMPORTANT : la page mélange CDI, stages et alternances, et la plupart des titres n'indiquent pas le contrat. "
    "Un titre sans contrat indiqué n'est donc PAS une raison de dire non (le contrat sera vérifié ensuite).\n"
    "Pour chaque texte, réponds :\n"
    "- \"oui\" : métier technique informatique de la liste, contrat non indiqué ou en alternance "
    "(alternance, apprentissage, apprenti, contrat pro, work-study, apprenticeship) ;\n"
    "- \"non\" : métier hors informatique ; OU contrat explicitement CDI, CDD, stage, internship, intérim ou VIE "
    "sans alternance ; OU ce n'est pas une offre (lien de navigation, « Voir toutes les offres », rubrique "
    "comme « Alternance et stage » ou « Alternance (4) », nom d'équipe ou de département seul comme "
    "« Business Analytics », nom de produit, page entreprise, « Postuler » ou « Candidature spontanée ») ;\n"
    "Exception : « Voir l'offre » ou « En savoir plus » seul = \"doute\" (c'est une offre dont le titre n'est pas lisible).\n"
    "- \"doute\" : texte trop vague pour connaître le métier.\n"
    "Donne un objet par texte, avec son numéro.\n\nTextes :\n"
)

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "verdict": {"type": "STRING", "enum": ["oui", "non", "doute"]},
            "raison": {"type": "STRING"},
        },
        "required": ["i", "verdict", "raison"],
    },
}


def juger(titres, source="lba"):
    """Étape 1. Renvoie une liste de (verdict, raison) alignée sur `titres`.
    source = "lba" (capteur, offres toutes en alternance) ou "carriere" (veilleur, contrats mélangés)."""
    consigne = CONSIGNE if source == "lba" else CONSIGNE_CARRIERE
    resultats = []
    for debut in range(0, len(titres), TAILLE_PAQUET):
        paquet = titres[debut:debut + TAILLE_PAQUET]
        texte = consigne + "\n".join(f"{i}. {t}" for i, t in enumerate(paquet))
        par_numero = {int(v["i"]): (v["verdict"], v.get("raison", "")) for v in _appeler(texte, SCHEMA, 120)}
        for i in range(len(paquet)):
            verdict, raison = par_numero.get(i, ("doute", "titre oublié par le modèle"))
            resultats.append((verdict if verdict in ("oui", "non", "doute") else "doute", raison))
        time.sleep(4)          # respect du quota gratuit
    return resultats


# ---------- Étape 2 : jugement sur l'offre complète ----------
FRONTIERE = (
    "Tu es recruteur. Un élève ingénieur cherche une ALTERNANCE dans un MÉTIER TECHNIQUE de l'informatique.\n"
    "OUI : développement, DevOps, cloud, data et BI (y compris data orientée métier : Power BI, SQL, Python, "
    "tableaux de bord pour le marketing ou la finance), IA, cybersécurité, administration systèmes et réseaux "
    "informatiques, support informatique, ERP (paramétrage, développement), QA/tests.\n"
    "NON : gestion de projet ou PMO (même informatique), chef de projet digital, Product Owner, Product Manager, "
    "AMOA/MOA, informatique RSE ou numérique responsable, vente, management, industrie, maintenance matérielle, "
    "câblage, qualité, marketing, RH, finance, même s'ils utilisent des outils informatiques.\n"
)
CONTRAT = {
    "lba": (
        "Toutes ces offres viennent de La bonne alternance, un service public dédié à l'alternance : "
        "l'absence du mot « alternance » n'est PAS une raison de dire non. Dis non pour le contrat seulement "
        "si l'offre indique clairement un CDI, CDD, stage ou intérim sans alternance.\n"
    ),
    "carriere": (
        "Ces offres viennent des sites carrière d'entreprises, qui mélangent CDI, CDD, stages et alternances. "
        "Vérifie DEUX critères : 1) le contrat est une alternance (alternance, apprentissage, apprenti, contrat "
        "de professionnalisation, work-study, apprenticeship) ; CDI, CDD, stage, internship, intérim ou VIE = \"non\". "
        "Sur un site carrière, une offre d'alternance le dit TOUJOURS : si la description est lisible et ne "
        "mentionne nulle part l'alternance, c'est un emploi classique = \"non\". \"à vérifier\" seulement si la "
        "description est vide, illisible (menu, cookies) ou trop courte pour le savoir ; "
        "2) le métier respecte la frontière ci-dessus. \"oui\" seulement si les deux critères sont remplis. "
        "Si la page n'est pas UNE offre précise (liste d'offres, rubrique, présentation d'équipe ou de produit, "
        "formulaire de candidature spontanée) = \"non\". Un poste situé hors de France = \"non\". Une phrase générale du site, sans lien avec "
        "ce poste (« nous recrutons aussi des alternants », menu « Alternance et stages ») ne prouve PAS que "
        "cette offre est une alternance.\n"
    ),
}
FIN_OFFRES = (
    "Si la description est vide ou trop courte, juge sur le titre avec la même frontière : \"non\" si le titre "
    "correspond à un métier NON (ex. contrôleur de gestion, chef de projet, maîtrise d'ouvrage, technicien de "
    "maintenance sans mention de l'informatique), sinon \"à vérifier\". "
    "\"à vérifier\" aussi si, après lecture, tu hésites vraiment entre oui et non.\n"
    "Pour \"oui\", la raison cite la mission et l'outil trouvés dans l'offre (ex. « automatiser les "
    "déploiements avec Docker et GitLab CI »). Raison courte, une phrase.\n"
    "Donne un objet par offre, avec son numéro.\n\nOffres :\n"
)

SCHEMA_OFFRES = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "verdict": {"type": "STRING", "enum": ["oui", "non", "à vérifier"]},
            "raison": {"type": "STRING"},
        },
        "required": ["i", "verdict", "raison"],
    },
}


def juger_offres(offres, source="lba"):
    """Étape 2. `offres` : liste de (titre, entreprise, description), au plus TAILLE_PAQUET_OFFRES.
    Renvoie une liste alignée de (verdict, raison) ; (None, None) si le modèle a oublié une offre
    (elle sera rejugée au passage suivant). source = "lba" (capteur) ou "carriere" (veilleur)."""
    texte = FRONTIERE + CONTRAT[source] + FIN_OFFRES + "\n\n".join(
        f"--- Offre {i} ---\nTitre : {t}\nEntreprise : {e or '?'}\nDescription : {d or '(vide)'}"
        for i, (t, e, d) in enumerate(offres))
    par_numero = {int(v["i"]): (v["verdict"], v.get("raison", ""))
                  for v in _appeler(texte, SCHEMA_OFFRES, 60)}
    time.sleep(4)              # respect du quota gratuit
    return [par_numero.get(i, (None, None)) for i in range(len(offres))]


if __name__ == "__main__":
    # Petit test de connexion, sans base ni Telegram
    exemples = [
        ("Alternance DevOps", "Exemple SA", "Vous automatiserez les déploiements avec Docker, Kubernetes et GitLab CI."),
        ("Assistant manager", "Fast-food X", "Encadrement de l'équipe en restaurant, gestion des plannings."),
        ("Technicien informatique", "Exemple SAS", ""),
    ]
    for (t, _, _), (v, r) in zip(exemples, juger_offres(exemples)):
        print(f"{t:28} → {v:10} | {r}  [{dernier[0]}]")