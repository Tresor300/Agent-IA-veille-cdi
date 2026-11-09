import os, re, math, time, sqlite3, html
from datetime import datetime
from collections import Counter
import pandas as pd
import requests
from dotenv import load_dotenv
from config import envoyer, noter_passage, DATA, RAPIDE
from dedoublon import Registre, offres_deja_envoyees
from filtre_ia import juger, juger_offres, GeminiIndisponible, TAILLE_PAQUET, TAILLE_PAQUET_OFFRES

load_dotenv()

# Mode test (MODE_TEST=1) : les messages sont affichés à l'écran au lieu d'être envoyés sur Telegram
if os.environ.get("MODE_TEST"):
    def envoyer(texte):
        print("\n----- [MODE TEST] message non envoyé -----\n" + texte)
        return True
    print("🧪 MODE TEST : aucun message ne sera envoyé sur Telegram.")
URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
CLES = {"Authorization": f"Bearer {os.environ['LBA_API_KEY']}"}
BASE = f"{DATA}/veilleur.db"
NANTES = (47.218, -1.553)
ROMES_INFO = ",".join(f"M18{n:02d}" for n in range(100)) + ",I1401"
DOM = [(16.25, -61.58, 60), (14.64, -61.02, 50), (4.94, -52.33, 150), (-21.11, 55.53, 60), (-12.78, 45.15, 40)]
MAX_A_JUGER = 15000            # plafond d'offres jugées par passage

stats = {"requetes": 0, "piles": 0}
toutes = {}


def lire(d, chemin):
    for cle in chemin.split("."):
        d = d.get(cle) if isinstance(d, dict) else None
    return d


# ---------- 1. Collecte (inchangée) ----------
def chercher(params):
    for essai in range(3):
        try:
            r = requests.get(URL, params=params, headers=CLES, timeout=30)
            stats["requetes"] += 1
            time.sleep(0.2)
            if r.ok:
                return r.json().get("jobs", [])
        except requests.RequestException:
            pass
        time.sleep(3)
    return []


def ranger(jobs):
    for j in jobs:
        ident = lire(j, "identifier.id")
        if ident:
            toutes[ident] = j


def description(j):
    """Texte brut de la description (balises HTML retirées), coupé à 1500 caractères."""
    texte = re.sub(r"<[^>]+>", " ", html.unescape(lire(j, "offer.description") or ""))
    return " ".join(texte.split())[:1500]


def plafonne(jobs):
    return any(n >= 150 for n in Counter(lire(j, "identifier.partner_label") for j in jobs).values())


def balayer(lat, lon, rayon):
    zone = {"latitude": round(lat, 4), "longitude": round(lon, 4), "radius": max(1, round(rayon))}
    jobs = chercher(zone)
    ranger(jobs)
    if not plafonne(jobs):
        return
    if rayon > 5:
        d_lat = (rayon / 2) / 111
        d_lon = d_lat / math.cos(math.radians(lat))
        for a in (-1, 1):
            for b in (-1, 1):
                balayer(lat + a * d_lat, lon + b * d_lon, rayon * 0.71)
    else:
        stats["piles"] += 1
        filet = chercher({**zone, "romes": ROMES_INFO})
        ranger(filet)
        if plafonne(filet):
            print("⚠️ Pile encore plafonnée malgré le filet :", zone)


def grille_metropole(rayon=30):
    pas_lat = rayon * 1.4 / 111
    lat = 41.3
    while lat <= 51.2:
        pas_lon = pas_lat / math.cos(math.radians(lat))
        lon = -5.2
        while lon <= 9.7:
            yield lat, lon, rayon
            lon += pas_lon
        lat += pas_lat


# ---------- 2. Mise en forme ----------
def distance_nantes(j):
    coords = lire(j, "workplace.location.geopoint.coordinates")
    if not coords:
        return None
    lon, lat = coords
    la1, lo1, la2, lo2 = map(math.radians, (NANTES[0], NANTES[1], lat, lon))
    a = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(a)))


CIBLES = [str(n).lower().strip() for n in pd.read_excel(f"{DATA}/pages_a_surveiller.xlsx")["Entreprise"].dropna()]


def est_cible(entreprise):
    """Vrai si l'entreprise est dans la liste cible, dans les deux sens :
    « Capgemini Engineering » (liste) reconnaît « CAPGEMINI » (offre), et inversement."""
    e = (entreprise or "").lower().strip()
    if len(e) < 3:
        return False
    return any(len(n) >= 3 and (re.search(rf"\b{re.escape(n)}\b", e) or (len(e) >= 4 and re.search(rf"\b{re.escape(e)}\b", n)))
               for n in CIBLES)


def bloc(ligne):
    _, titre, entreprise, lieu, url, km, verdict, raison = ligne
    entreprise = entreprise or "Entreprise non précisée"
    marque = "⭐ " if est_cible(entreprise) else ""
    return (f"{marque}{titre}\n"
            f"🏢 {entreprise} · 📍 {lieu or '?'}{f' ({km} km)' if km is not None else ''}\n"
            f"💡 {raison or '?'}\n"
            f"{url or 'lien indisponible'}\n")


def decouper(entete, blocs, limite=3800):
    messages, courant = [], entete
    for b in blocs:
        if len(courant) + len(b) + 1 > limite:
            messages.append(courant)
            courant = ""
        courant += "\n" + b
    messages.append(courant)
    return messages


# ---------- 3. Programme principal ----------

def lancer():
    debut = time.time()

    # 3.1 Collecte
    cercles = list(grille_metropole()) + DOM
    if RAPIDE:                                 # test rapide : un seul cercle autour de Nantes
        cercles = [(NANTES[0], NANTES[1], 30)]
    for i, (lat, lon, rayon) in enumerate(cercles, 1):
        balayer(lat, lon, rayon)
        if i % 100 == 0:
            print(f"{i}/{len(cercles)} cercles | {len(toutes)} offres", flush=True)

    # 3.2 Mémoire : toutes les offres, avec leur verdict
    con = sqlite3.connect(BASE)
    con.execute("CREATE TABLE IF NOT EXISTS offres_capteur (id TEXT PRIMARY KEY, titre TEXT, entreprise TEXT, "
                "lieu TEXT, url TEXT, km INTEGER, verdict TEXT, raison TEXT, envoyee INTEGER DEFAULT 0, vue_le TEXT)")
    colonnes = {c[1] for c in con.execute("PRAGMA table_info(offres_capteur)")}
    for col in ("description", "verdict2", "raison2"):          # colonnes de l'étape 2
        if col not in colonnes:
            con.execute(f"ALTER TABLE offres_capteur ADD COLUMN {col} TEXT")
    connues = {r[0] for r in con.execute("SELECT id FROM offres_capteur")}
    try:
        deja_envoyees = {r[0] for r in con.execute("SELECT id FROM offres_api")}
    except sqlite3.OperationalError:
        deja_envoyees = set()
    maintenant = datetime.now().isoformat(timespec="seconds")
    nouvelles = [(k, j) for k, j in toutes.items() if k not in connues]
    # Sécurité : base vide (mémoire perdue) → on mémorise tout sans juger ni envoyer
    premier_passage = not connues
    if premier_passage:
        print("⚠️ Base vide : offres mémorisées sans jugement ni envoi (pas d'avalanche d'alertes).")
    con.executemany(
        "INSERT INTO offres_capteur (id, titre, entreprise, lieu, url, km, verdict, envoyee, vue_le, description) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(k, html.unescape(lire(j, "offer.title") or ""), lire(j, "workplace.name"),
          lire(j, "workplace.location.address"), lire(j, "apply.url"), distance_nantes(j),
          "initial" if premier_passage else None,
          1 if (premier_passage or k in deja_envoyees) else 0, maintenant,
          None if premier_passage else description(j)) for k, j in nouvelles])
    # Description manquante d'une offre qui attend encore l'étape 2 : on la complète
    attente = {r[0] for r in con.execute(
        "SELECT id FROM offres_capteur WHERE description IS NULL AND verdict IN ('oui', 'doute') "
        "AND verdict2 IS NULL AND envoyee = 0")}
    con.executemany("UPDATE offres_capteur SET description = ? WHERE id = ?",
                    [(description(toutes[k]), k) for k in attente if k in toutes])
    con.commit()

    # 3.3 Étape 1 : jugement du titre, sauvegardé paquet par paquet
    a_juger = con.execute("SELECT id, titre FROM offres_capteur WHERE verdict IS NULL LIMIT ?", (MAX_A_JUGER,)).fetchall()
    print(f"\nÉtape 1 : {len(a_juger)} titres à juger…")
    jugees = 0
    try:
        for d in range(0, len(a_juger), TAILLE_PAQUET):
            paquet = a_juger[d:d + TAILLE_PAQUET]
            verdicts = juger([t for _, t in paquet])
            con.executemany("UPDATE offres_capteur SET verdict = ?, raison = ? WHERE id = ?",
                            [(v, r, i) for (i, _), (v, r) in zip(paquet, verdicts)])
            con.commit()
            jugees += len(paquet)
            print(f"   {jugees}/{len(a_juger)} titres jugés", flush=True)
    except GeminiIndisponible as e:
        print(f"⚠️ {e} — le reste sera jugé au prochain passage.")

    # 3.4 Étape 2 : lecture de l'offre complète pour les « oui » et « doute » pas encore envoyés
    a_lire = con.execute("SELECT id, titre, entreprise, description FROM offres_capteur "
                         "WHERE verdict IN ('oui', 'doute') AND verdict2 IS NULL "
                         "AND description IS NOT NULL AND envoyee = 0").fetchall()
    print(f"\nÉtape 2 : {len(a_lire)} offres à lire…")
    lues = 0
    try:
        for d in range(0, len(a_lire), TAILLE_PAQUET_OFFRES):
            paquet = a_lire[d:d + TAILLE_PAQUET_OFFRES]
            verdicts = juger_offres([(t, e, desc) for _, t, e, desc in paquet])
            con.executemany("UPDATE offres_capteur SET verdict2 = ?, raison2 = ? WHERE id = ?",
                            [(v, r, i) for (i, *_), (v, r) in zip(paquet, verdicts) if v])
            con.commit()
            lues += len(paquet)
            print(f"   {lues}/{len(a_lire)} offres lues", flush=True)
    except GeminiIndisponible as e:
        print(f"⚠️ {e} — le reste sera lu au prochain passage.")

    # 3.5 Envoi : les « oui » de l'étape 2, puis une section « à vérifier »
    lignes = con.execute("SELECT id, titre, entreprise, lieu, url, km, verdict2, raison2 FROM offres_capteur "
                         "WHERE verdict2 IN ('oui', 'à vérifier') AND envoyee = 0").fetchall()
    # Doublons : même offre déjà envoyée (par le capteur ou le veilleur) ou déjà dans ce message
    registre = Registre(offres_deja_envoyees(con))
    uniques = []
    for l in lignes:
        if not registre.est_doublon(l[1], l[2], l[3]):
            registre.ajouter(l[1], l[2], l[3])
            uniques.append(l)
    ordre = lambda l: (not est_cible(l[2]), l[5] if l[5] is not None else 99999)
    oui = sorted([l for l in uniques if l[6] == "oui"], key=ordre)
    a_verifier = sorted([l for l in uniques if l[6] == "à vérifier"], key=ordre)
    if uniques:
        blocs = [bloc(l) for l in oui]
        if a_verifier:
            blocs.append(f"🔎 À VÉRIFIER ({len(a_verifier)}) — description vide ou poste mixte\n")
            blocs += [bloc(l) for l in a_verifier]
        entete = f"🛰️ Capteur — {len(oui)} alternance(s) informatique(s)\n"
        if all(envoyer(m) for m in decouper(entete, blocs)):
            con.executemany("UPDATE offres_capteur SET envoyee = 1 WHERE id = ?", [(l[0],) for l in lignes])
            con.commit()
        else:
            print("❌ Échec d'envoi Telegram : ces offres seront renvoyées au prochain passage.")
    elif lignes:                       # uniquement des doublons : rien à envoyer, mais on les marque vus
        con.executemany("UPDATE offres_capteur SET envoyee = 1 WHERE id = ?", [(l[0],) for l in lignes])
        con.commit()

    # Ménage : la description ne sert qu'aux offres qui attendent l'étape 2 (base légère pour GitHub)
    con.execute("UPDATE offres_capteur SET description = NULL "
                "WHERE verdict = 'non' OR (verdict2 IS NULL AND envoyee = 1)")
    con.commit()
    con.execute("VACUUM")

    comptes = dict(con.execute("SELECT COALESCE(verdict, 'en attente'), COUNT(*) FROM offres_capteur GROUP BY 1").fetchall())
    comptes2 = dict(con.execute("SELECT verdict2, COUNT(*) FROM offres_capteur WHERE verdict2 IS NOT NULL GROUP BY 1").fetchall())
    noter_passage(con, "capteur", debut, len(nouvelles), len(oui) + len(a_verifier), "")
    con.close()
    print(f"\n=== BILAN CAPTEUR ({round((time.time() - debut) / 60, 1)} min) ===")
    print(f"Requêtes : {stats['requetes']} | piles traitées : {stats['piles']}")
    print(f"Offres collectées : {len(toutes)} | nouvelles : {len(nouvelles)} | "
          f"envoyées : {len(oui)} oui + {len(a_verifier)} à vérifier")
    print(f"Étape 1 (titre) : {comptes}")
    print(f"Étape 2 (description) : {comptes2}")


if __name__ == "__main__":
    lancer()