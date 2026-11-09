import os
import re
from urllib.parse import urlparse
import pandas as pd
import sqlite3
from datetime import datetime
import time
from config import envoyer, noter_passage, DATA, RAPIDE
from connecteurs import detecter_acces, adresse_a_visiter, lire_interface, autorise, AccesImpossible
from dedoublon import Registre, offres_deja_envoyees
from filtre_ia import juger, juger_offres, GeminiIndisponible, TAILLE_PAQUET, TAILLE_PAQUET_OFFRES
from playwright.sync_api import sync_playwright

BASE = f"{DATA}/veilleur.db"


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Une offre se reconnaît à son adresse : job, offre, emploi... ou un numéro d'identifiant
MOTIF = re.compile(r"job|offre|offer|emploi|poste|position|vacanc|annonce|requisition|\d{4,}"
                   r"|taleez\.com/apply/(?!sp/)|breezy\.hr/p/", re.I)   # plateformes Taleez et Breezy
# Mots d'alternance, et contrats incompatibles annoncés dans le titre (règle fixe, sans IA)
ALTERNANCE = re.compile(r"altern|apprenti|apprentissage|contrat pro|work[- ]?study|apprenticeship", re.I)
# Pages de vérification anti-robot affichées à la place de l'offre
ANTI_ROBOT = re.compile(r"cloudflare|just a moment|checking your browser|v[ée]rification de s[ée]curit[ée]|"
                        r"attention required|enable javascript and cookies|captcha", re.I)
AUTRE_CONTRAT = re.compile(r"\b(stage|stagiaire|intern|internship|cdi|cdd|graduate program)\b", re.I)
# Lien vers la rubrique « Alternance » d'un site : le texte du lien ne contient QUE le nom de la rubrique,
# éventuellement suivi d'un nombre d'offres (« Alternance (4) », « Alternance et apprentissage (2) »).
# « Alternance DevOps » est une offre, pas une rubrique.
RUBRIQUE = re.compile(r"(nos\s+)?(offres?\s+(en|d'|de)\s*)?((stages?|alternances?|apprentissage)\s*(et|&|/)\s*)?"
                      r"(alternances?|apprentissage|stages?)\s*(\(\d+\))?", re.I)
DEBUT_RUBRIQUE = re.compile(r"nos\s+(stages?\s+et\s+alternances?|alternances?|offres?\s+(en|d')\s*alternance)\b", re.I)


def est_rubrique(texte):
    texte = " ".join((texte or "").split())
    return bool(RUBRIQUE.fullmatch(texte) and ALTERNANCE.search(texte)) or bool(DEBUT_RUBRIQUE.match(texte))


# ---------- 1. Collecte des liens d'offres (inchangée) ----------
def extraire_offres(page):
    """Renvoie {adresse: titre} pour les liens dont l'adresse ressemble à une offre :
    un mot comme job, offre, emploi… dans l'adresse, OU une sous-page de la page surveillée
    (ex. cloud-temple.com/en/join-us/ingenieur-cloud… sous cloud-temple.com/en/join-us)."""
    page_url = page.url.split("#")[0].rstrip("/")
    base = urlparse(page_url)
    liens = page.eval_on_selector_all(
        "a", "els => els.map(e => [e.innerText.trim(), e.href])")
    offres = {}
    for texte, href in liens:
        href = href.split("#")[0]
        if not href.startswith("http") or href.rstrip("/") == page_url:
            continue
        lien = urlparse(href)
        sous_page = (lien.netloc.removeprefix("www.") == base.netloc.removeprefix("www.") and len(base.path.strip("/")) > 0
                     and lien.path.rstrip("/").startswith(base.path.rstrip("/") + "/"))
        if MOTIF.search(href) or sous_page:
            texte = " ".join(texte.split())
            if len(texte) > len(offres.get(href, "")):
                offres[href] = texte
    return offres


def ouvrir_base():
    connexion = sqlite3.connect(BASE)
    connexion.execute("""CREATE TABLE IF NOT EXISTS offres (
        url TEXT PRIMARY KEY, entreprise TEXT, titre TEXT, vue_le TEXT)""")
    colonnes = {c[1] for c in connexion.execute("PRAGMA table_info(offres)")}
    for col in ("verdict", "raison", "description", "verdict2", "raison2"):
        if col not in colonnes:
            connexion.execute(f"ALTER TABLE offres ADD COLUMN {col} TEXT")
    if "envoyee" not in colonnes:
        # DEFAULT 1 : les offres déjà en base avant le filtre IA sont considérées comme traitées
        connexion.execute("ALTER TABLE offres ADD COLUMN envoyee INTEGER DEFAULT 1")
    connexion.commit()
    return connexion


def garder_nouvelles(connexion, offres, entreprise):
    """Enregistre les offres jamais vues. Renvoie (nouvelles, première_visite).
    Première visite d'une entreprise ajoutée à la liste : ses offres actuelles sont jugées et envoyées
    comme les autres (le filtre IA ne laisse passer que les alternances en informatique).
    Seule exception : base vide (mémoire perdue) → tout est mémorisé sans jugement ni envoi."""
    deja = connexion.execute(
        "SELECT COUNT(*) FROM offres WHERE entreprise = ?", (entreprise,)).fetchone()[0]
    premiere_visite = deja == 0 and BASE_VIDE
    nouvelles = {}
    for url, titre in offres.items():
        if not connexion.execute("SELECT 1 FROM offres WHERE url = ?", (url,)).fetchone():
            connexion.execute(
                "INSERT INTO offres (url, entreprise, titre, vue_le, envoyee) VALUES (?, ?, ?, ?, ?)",
                (url, entreprise, titre, datetime.now().isoformat(timespec="seconds"),
                 1 if premiere_visite else 0))
            nouvelles[url] = titre
    connexion.commit()
    return nouvelles, premiere_visite


# Mémoire perdue ? (aucune offre en base au démarrage)
with sqlite3.connect(BASE) as _c:
    try:
        BASE_VIDE = _c.execute("SELECT COUNT(*) FROM offres").fetchone()[0] == 0
    except sqlite3.OperationalError:
        BASE_VIDE = True

# Sites à surveiller : pages validées (✅ Fiable et 🔧 Corrigée)
df = pd.read_excel(f"{DATA}/pages_a_surveiller.xlsx")
df = df[df["Verdict"].isin(["✅ Fiable", "🔧 Corrigée"])]
SITES = [(ligne["Entreprise"], ligne["Page à surveiller"]) for _, ligne in df.iterrows()]
if RAPIDE:                                     # test rapide : 10 sites seulement
    SITES = SITES[:10]


def visiter(navigateur, url, essais=2):
    """Renvoie (offres, erreur). Ne plante jamais : en cas d'échec, offres est vide."""
    url = adresse_a_visiter(url)                 # Workday : page filtrée sur « alternance »
    if not autorise(url):
        return {}, "lecture interdite par le robots.txt du site"
    erreur = None
    for essai in range(essais):
        page = navigateur.new_page(user_agent=UA)
        try:
            reponse = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)   # 5 s pour laisser la liste s'afficher
            if reponse and reponse.status >= 400:
                raise Exception(f"code HTTP {reponse.status}")
            titre = page.title().lower()
            if "introuvable" in titre or "not found" in titre:
                raise Exception("page introuvable")
            offres = extraire_offres(page)
            # Pages suivantes (Workday affiche 20 offres par page) : bouton « suivant », 10 pages au plus
            for _ in range(10):
                suivant = page.query_selector("button[aria-label='next'], button[data-uxi-element-id='next']")
                if not suivant or not suivant.is_enabled():
                    break
                suivant.click()
                page.wait_for_timeout(3000)
                nouvelles_offres = {u: t for u, t in extraire_offres(page).items() if u not in offres}
                if not nouvelles_offres:
                    break
                offres.update(nouvelles_offres)
            # Suivre la rubrique « Alternance » du site : ses offres n'apparaissent pas sur la page principale
            for rubrique in [u for u, t in offres.items() if est_rubrique(t) and autorise(u)][:3]:
                try:
                    page.goto(rubrique, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)
                    for u, t in extraire_offres(page).items():
                        if len(t) > len(offres.get(u, "")):
                            offres[u] = t
                except Exception:
                    pass
            return offres, None
        except Exception as e:
            erreur = str(e).splitlines()[0]   # 1re ligne du message suffit
        finally:
            page.close()
    return {}, erreur


# ---------- 2. Lecture d'une offre (pour l'étape 2) ----------
def lire_offre(navigateur, url):
    """Texte de la page d'une offre (zone principale si elle existe), 3000 caractères au plus.
    Renvoie "" si la page ne s'ouvre pas : l'IA jugera alors sur le titre."""
    if not autorise(url):
        return ""
    page = navigateur.new_page(user_agent=UA)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        texte = ""
        for zone in ("main", "article", "[role=main]", "body"):
            element = page.query_selector(zone)
            if element:
                texte = " ".join(element.inner_text().split())
                if len(texte) > 200:
                    break
        texte = texte[:3000]
        if ANTI_ROBOT.search(texte[:600]):         # page de vérification (Cloudflare…) : rien à lire
            return ""
        # Mention d'alternance ailleurs dans la page (encadré, bas de page) : on transmet la phrase
        # elle-même, pour que l'IA distingue « Type de contrat : apprentissage » d'un slogan du site
        if not ALTERNANCE.search(texte):
            corps = " ".join(page.inner_text("body").split())
            trouve = ALTERNANCE.search(corps)
            if trouve:
                extrait = corps[max(0, trouve.start() - 120):trouve.end() + 120]
                texte = texte[:2700] + f" … [ailleurs sur la page : « {extrait} »]"
        return texte
    except Exception:
        return ""
    finally:
        page.close()


# ---------- 3. Message groupé ----------
def bloc(ligne):
    _, titre, entreprise, raison = ligne
    return f"{titre}\n🏢 {entreprise}\n💡 {raison or '?'}\n{ligne[0]}\n"


def decouper(entete, blocs, limite=3800):
    messages, courant = [], entete
    for b in blocs:
        if len(courant) + len(b) + 1 > limite:
            messages.append(courant)
            courant = ""
        courant += "\n" + b
    messages.append(courant)
    return messages


# ---------- 4. Programme principal ----------
connexion = ouvrir_base()
debut = time.time()
total_nouvelles = 0
bilan = {"avec offres": 0, "0 offre": [], "erreur": []}
with sync_playwright() as p:
    navigateur = p.chromium.launch()

    # 4.1 Collecte : visite des pages carrière
    for i, (entreprise, url) in enumerate(SITES, 1):
        acces, identifiant = detecter_acces(url)
        descriptions = {}
        if acces == "page":
            offres, erreur = visiter(navigateur, url)
        else:                                    # interface officielle de la plateforme
            try:
                deja_vues = {u for (u,) in connexion.execute("SELECT url FROM offres WHERE entreprise = ?", (entreprise,))}
                lues = lire_interface(acces, identifiant, deja_vues)
                offres = {u: t for u, (t, _) in lues.items()}
                descriptions = {u: d for u, (_, d) in lues.items()}
                erreur = None
            except AccesImpossible as e:
                offres, erreur = {}, f"{acces} : {e}"
        if erreur:
            print(f"[{i}/{len(SITES)}] ⚠️  {entreprise} : {erreur}")
            bilan["erreur"].append(entreprise)
            continue
        nouvelles, premiere_visite = garder_nouvelles(connexion, offres, entreprise)
        total_nouvelles += len(nouvelles)
        if descriptions:                         # description fournie par l'interface : pas besoin d'ouvrir l'offre
            connexion.executemany("UPDATE offres SET description = ? WHERE url = ?",
                                  [(descriptions[u], u) for u in nouvelles if u in descriptions])
            connexion.commit()
        source = "" if acces == "page" else f" [{acces}]"
        print(f"[{i}/{len(SITES)}] {entreprise}{source} : {len(offres)} offres, dont {len(nouvelles)} nouvelles")
        if premiere_visite:
            print("    (base vide : offres mémorisées, pas d'alerte)")
        if offres:
            bilan["avec offres"] += 1
        else:
            bilan["0 offre"].append(entreprise)

    # 4.2 Étape 1 : jugement du titre des nouvelles offres
    a_juger = connexion.execute(
        "SELECT url, titre FROM offres WHERE envoyee = 0 AND verdict IS NULL").fetchall()
    print(f"\nÉtape 1 : {len(a_juger)} titres à juger…")
    vides = [(u,) for u, t in a_juger                                       # liens sans texte ou « 375 »
             if not re.search(r"[^\W\d_]{3}", t or "")]                      # (pagination) : pas 3 lettres
    connexion.executemany("UPDATE offres SET verdict = 'non', raison = 'lien sans texte' WHERE url = ?", vides)
    contrat = [(u,) for u, t in a_juger if AUTRE_CONTRAT.search(t or "") and not ALTERNANCE.search(t or "")]
    connexion.executemany("UPDATE offres SET verdict = 'non', raison = 'stage, CDI ou CDD annoncé dans le titre' "
                          "WHERE url = ?", contrat)
    rubriques = [(u,) for u, t in a_juger if est_rubrique(t or "")]
    connexion.executemany("UPDATE offres SET verdict = 'non', raison = 'rubrique (ses offres sont suivies)' "
                          "WHERE url = ?", rubriques)
    exclues = {u for (u,) in vides + contrat + rubriques}
    a_juger = [(u, t) for u, t in a_juger if u not in exclues]
    try:
        for d in range(0, len(a_juger), TAILLE_PAQUET):
            paquet = a_juger[d:d + TAILLE_PAQUET]
            verdicts = juger([t for _, t in paquet], source="carriere")
            connexion.executemany("UPDATE offres SET verdict = ?, raison = ? WHERE url = ?",
                                  [(v, r, u) for (u, _), (v, r) in zip(paquet, verdicts)])
            connexion.commit()
    except GeminiIndisponible as e:
        print(f"⚠️ {e} — le reste sera jugé au prochain passage.")
    connexion.commit()

    # 4.3 Étape 2 : lecture de la page de l'offre pour les « oui » et « doute »
    a_lire = connexion.execute(
        "SELECT url, titre, entreprise, description FROM offres "
        "WHERE envoyee = 0 AND verdict IN ('oui', 'doute') AND verdict2 IS NULL").fetchall()
    print(f"Étape 2 : {len(a_lire)} offres à lire…")
    try:
        for d in range(0, len(a_lire), TAILLE_PAQUET_OFFRES):
            paquet = []
            for u, t, e, desc in a_lire[d:d + TAILLE_PAQUET_OFFRES]:
                if desc is None:                     # page jamais lue : on la lit et on la garde
                    desc = lire_offre(navigateur, u)
                    connexion.execute("UPDATE offres SET description = ? WHERE url = ?", (desc, u))
                paquet.append((u, t, e, desc))
            connexion.commit()
            verdicts = juger_offres([(t, e, desc) for _, t, e, desc in paquet], source="carriere")
            # Règle fixe : sur un site carrière, une alternance le dit. Si ni le titre ni l'offre
            # (y compris un encadré ailleurs sur la page) n'en parlent, ce n'en est pas une.
            # Une page illisible (anti-robot) ne suffit pas à rendre une offre « à vérifier ».
            verdicts = [("non", "aucune mention d'alternance (ni dans le titre ni dans l'offre)")
                        if v in ("oui", "à vérifier")
                        and not ALTERNANCE.search((t or "") + " " + (desc or "")) else (v, r)
                        for (_, t, _, desc), (v, r) in zip(paquet, verdicts)]
            connexion.executemany("UPDATE offres SET verdict2 = ?, raison2 = ? WHERE url = ?",
                                  [(v, r, u) for (u, *_), (v, r) in zip(paquet, verdicts) if v])
            connexion.commit()
    except GeminiIndisponible as e:
        print(f"⚠️ {e} — le reste sera lu au prochain passage.")
    navigateur.close()

# 4.4 Envoi groupé : les « oui », puis une section « à vérifier »
lignes = connexion.execute(
    "SELECT url, titre, entreprise, raison2, verdict2 FROM offres "
    "WHERE envoyee = 0 AND verdict2 IN ('oui', 'à vérifier') ORDER BY entreprise").fetchall()
# Doublons : même offre déjà envoyée (par le veilleur ou le capteur) ou déjà dans ce message
registre = Registre(offres_deja_envoyees(connexion))
oui, a_verifier = [], []
for u, t, e, r, v in lignes:
    if registre.est_doublon(t, e):
        continue
    registre.ajouter(t, e)
    (oui if v == "oui" else a_verifier).append((u, t, e, r))
if oui or a_verifier:
    blocs = [bloc(l) for l in oui]
    if a_verifier:
        blocs.append(f"🔎 À VÉRIFIER ({len(a_verifier)}) — contrat non précisé ou poste mixte\n")
        blocs += [bloc(l) for l in a_verifier]
    entete = (f"🌐 Veilleur — {len(oui)} alternance(s) informatique(s) sur les sites carrière\n" if oui
              else f"🌐 Veilleur — aucune offre sûre, {len(a_verifier)} offre(s) à vérifier\n")
    if all(envoyer(m) for m in decouper(entete, blocs)):
        connexion.executemany("UPDATE offres SET envoyee = 1 WHERE url = ?", [(l[0],) for l in lignes])
        connexion.commit()
    else:
        print("❌ Échec d'envoi Telegram : ces offres seront renvoyées au prochain passage.")
elif lignes:                           # uniquement des doublons : rien à envoyer, mais on les marque vus
    connexion.executemany("UPDATE offres SET envoyee = 1 WHERE url = ?", [(l[0],) for l in lignes])
    connexion.commit()

etape1 = dict(connexion.execute(
    "SELECT verdict, COUNT(*) FROM offres WHERE verdict IS NOT NULL GROUP BY 1").fetchall())
etape2 = dict(connexion.execute(
    "SELECT verdict2, COUNT(*) FROM offres WHERE verdict2 IS NOT NULL GROUP BY 1").fetchall())
noter_passage(connexion, "veilleur", debut, total_nouvelles, len(oui) + len(a_verifier), ", ".join(bilan["erreur"]))
connexion.close()

print(f"\nBILAN : {bilan['avec offres']} sites avec des offres, "
      f"{len(bilan['0 offre'])} sites à 0 offre, {len(bilan['erreur'])} erreurs")
print("0 offre :", ", ".join(bilan["0 offre"]))
print("Erreurs :", ", ".join(bilan["erreur"]))
print(f"Étape 1 (titre) : {etape1}")
print(f"Étape 2 (offre) : {etape2}")
print(f"Envoyées : {len(oui)} oui + {len(a_verifier)} à vérifier")