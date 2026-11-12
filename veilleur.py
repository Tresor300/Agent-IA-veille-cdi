"""Veilleur — version CDI : visite les sites carrière, fait juger les nouvelles offres par l'IA
(métier, contrat CDI, niveau, lieu) et envoie un message Telegram classé par zone et par niveau."""
import re
from urllib.parse import urlparse
import pandas as pd
import sqlite3
from datetime import datetime
import time
from config import envoyer, noter_passage, DATA, RAPIDE
from connecteurs import (detecter_acces, adresse_a_visiter, lire_interface, autorise, AccesImpossible,
                         description_smartrecruiters)
from dedoublon import cle_titre
from filtre_ia import (juger, juger_offres, zone, AUTRE_CONTRAT, GeminiIndisponible,
                       TAILLE_PAQUET, TAILLE_PAQUET_OFFRES)
from playwright.sync_api import sync_playwright

BASE = f"{DATA}/veilleur.db"


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Une offre se reconnaît à son adresse : job, offre, emploi... ou un numéro d'identifiant
# Sites exclus par nos règles d'accès (conditions d'utilisation) : un lien vers eux n'est jamais lu
EXCLUS = re.compile(r"(^|\.)(indeed\.|linkedin\.com|welcometothejungle\.com|hellowork\.com|glassdoor\.)", re.I)
MOTIF = re.compile(r"job|offre|offer|emploi|poste|position|vacanc|annonce|requisition|\d{4,}"
                   r"|taleez\.com/apply/(?!sp/)|breezy\.hr/p/", re.I)   # plateformes Taleez et Breezy
# Pages de vérification anti-robot affichées à la place de l'offre
ANTI_ROBOT = re.compile(r"cloudflare|just a moment|checking your browser|v[ée]rification de s[ée]curit[ée]|"
                        r"attention required|enable javascript and cookies|captcha", re.I)
# Mention du contrat ailleurs sur la page (encadré, bas de page) : transmise à l'IA
# Bandeau de cookies : on le ferme pour lire l'offre (sinon le texte lu est celui du bandeau).
# On refuse si le site le permet, sinon on accepte (navigateur jetable, rien n'est conservé).
COOKIES = "text=/^(tout refuser|refuser tout|continuer sans accepter|reject all|decline all|tout accepter|accepter( et fermer)?|accepter tous les cookies|autoriser tous|allow all|accept all( cookies)?|j'accepte|ok)$/i"
# Texte de lien qui n'est pas un titre : on le remplace par le titre de la page de l'offre
TITRE_VIDE = re.compile(r"^(voir|d[ée]tails?|en savoir plus|lire la suite|postuler|apply|learn more|view|more)\b", re.I)
MOT_CONTRAT = re.compile(r"\b(cdi|cdd|contrat|stage|altern\w*|apprenti\w*|freelance|int[ée]rim|permanent)\b", re.I)


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
        if EXCLUS.search(lien.netloc):
            continue
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
    for col in ("verdict", "raison", "description", "verdict2", "raison2", "niveau", "ville", "departement"):
        if col not in colonnes:
            connexion.execute(f"ALTER TABLE offres ADD COLUMN {col} TEXT")
    if "envoyee" not in colonnes:
        connexion.execute("ALTER TABLE offres ADD COLUMN envoyee INTEGER DEFAULT 0")
    connexion.commit()
    return connexion


def garder_nouvelles(connexion, offres, entreprise):
    """Enregistre les offres jamais vues et les renvoie. Version CDI : pas de « première visite »
    silencieuse, toutes les offres actuelles sont jugées et envoyées dès le premier passage."""
    nouvelles = {}
    for url, titre in offres.items():
        if not connexion.execute("SELECT 1 FROM offres WHERE url = ?", (url,)).fetchone():
            connexion.execute(
                "INSERT INTO offres (url, entreprise, titre, vue_le, envoyee) VALUES (?, ?, ?, ?, 0)",
                (url, entreprise, titre, datetime.now().isoformat(timespec="seconds")))
            nouvelles[url] = titre
    connexion.commit()
    return nouvelles


# Sites à surveiller : pages validées (✅ Fiable et 🔧 Corrigée)
df = pd.read_excel(f"{DATA}/pages_a_surveiller.xlsx")
df = df[df["Verdict"].isin(["✅ Fiable", "🔧 Corrigée"])]
SITES = [(ligne["Entreprise"], ligne["Page à surveiller"]) for _, ligne in df.iterrows()]
if RAPIDE:                                     # test rapide : 10 sites seulement
    SITES = SITES[:10]


def visiter(navigateur, url, essais=2):
    """Renvoie (offres, erreur). Ne plante jamais : en cas d'échec, offres est vide."""
    url = adresse_a_visiter(url)
    if not autorise(url):
        return {}, "lecture interdite par le robots.txt du site"
    erreur = None
    for essai in range(essais):
        page = navigateur.new_page(user_agent=UA)
        try:
            reponse = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)   # 5 s pour laisser la liste s'afficher
            if "myworkday" in url:        # Workday : liste chargée en JavaScript, parfois lente
                try:
                    page.wait_for_selector("[data-automation-id='jobTitle']", timeout=25000)
                except Exception:
                    pass                  # vraiment aucune offre : la page reste vide
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
                page.wait_for_timeout(5000 if "myworkday" in url else 3000)   # Workday : page suivante lente
                nouvelles_offres = {u: t for u, t in extraire_offres(page).items() if u not in offres}
                if not nouvelles_offres:
                    break
                offres.update(nouvelles_offres)
            return offres, None
        except Exception as e:
            erreur = str(e).splitlines()[0]   # 1re ligne du message suffit
        finally:
            page.close()
    return {}, erreur


# ---------- 2. Lecture d'une offre (pour l'étape 2) ----------
def lire_offre(navigateur, url):
    """Renvoie (texte, titre) : texte de la page d'une offre (zone principale si elle existe),
    3000 caractères au plus, et le titre principal de la page (h1).
    Texte "" si la page ne s'ouvre pas : l'IA jugera alors sur le titre."""
    if "jobs.smartrecruiters.com/" in url:        # interface officielle : description ET lieu
        return description_smartrecruiters(url), None
    if not autorise(url):
        return "", None
    page = navigateur.new_page(user_agent=UA)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        if "myworkday" in url:            # Workday : description chargée en JavaScript
            try:
                page.wait_for_selector("[data-automation-id='jobPostingDescription']", timeout=15000)
            except Exception:
                pass
        try:
            bouton = page.locator(COOKIES).first
            if bouton.count() and bouton.is_visible():
                bouton.click(timeout=3000)
                page.wait_for_timeout(1500)
        except Exception:
            pass                                  # pas de bandeau ou bouton non cliquable : on lit tel quel
        texte = ""
        for zone in ("main", "article", "[role=main]", "body"):
            element = page.query_selector(zone)
            if element:
                texte = " ".join(element.inner_text().split())
                if len(texte) > 200:
                    break
        texte = texte[:3000]
        if ANTI_ROBOT.search(texte[:600]):         # page de vérification (Cloudflare…) : rien à lire
            return "", None
        h1 = page.query_selector("h1")
        titre = " ".join(h1.inner_text().split())[:150] if h1 else None
        # Contrat indiqué ailleurs dans la page (encadré, bas de page) : on transmet la phrase elle-même
        if not MOT_CONTRAT.search(texte):
            corps = " ".join(page.inner_text("body").split())
            trouve = MOT_CONTRAT.search(corps)
            if trouve:
                extrait = corps[max(0, trouve.start() - 120):trouve.end() + 120]
                texte = texte[:2700] + f" … [ailleurs sur la page : « {extrait} »]"
        return texte, titre
    except Exception:
        return "", None
    finally:
        page.close()


# ---------- 3. Message groupé ----------
def bloc(o):
    lieu = f"{o['ville'] or '?'} ({o['departement'] or '?'})"
    niveau = " · niveau non précisé" if o["niveau"] == "non précisé" else ""
    return f"{o['titre']}\n🏢 {o['entreprise']} · 📍 {lieu}{niveau}\n💡 {o['raison'] or '?'}\n{o['url']}\n"


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
        nouvelles = garder_nouvelles(connexion, offres, entreprise)
        total_nouvelles += len(nouvelles)
        if descriptions:                         # description fournie par l'interface : pas besoin d'ouvrir l'offre
            connexion.executemany("UPDATE offres SET description = ? WHERE url = ?",
                                  [(descriptions[u], u) for u in nouvelles if descriptions.get(u)])
            connexion.commit()
        source = "" if acces == "page" else f" [{acces}]"
        print(f"[{i}/{len(SITES)}] {entreprise}{source} : {len(offres)} offres, dont {len(nouvelles)} nouvelles")
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
    contrat = [(u,) for u, t in a_juger if AUTRE_CONTRAT.search(t or "")]
    connexion.executemany("UPDATE offres SET verdict = 'non', raison = 'contrat autre que CDI annoncé dans le titre' "
                          "WHERE url = ?", contrat)
    exclues = {u for (u,) in vides + contrat}
    a_juger = [(u, t) for u, t in a_juger if u not in exclues]
    try:
        for d in range(0, len(a_juger), TAILLE_PAQUET):
            paquet = a_juger[d:d + TAILLE_PAQUET]
            verdicts = juger([t for _, t in paquet])
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
                    desc, titre_page = lire_offre(navigateur, u)
                    connexion.execute("UPDATE offres SET description = ? WHERE url = ?", (desc, u))
                    if titre_page and TITRE_VIDE.match(t or ""):   # « Voir l'offre » → vrai titre
                        t = titre_page
                        connexion.execute("UPDATE offres SET titre = ? WHERE url = ?", (t, u))
                paquet.append((u, t, e, desc))
            connexion.commit()
            verdicts = juger_offres([(t, e, desc) for _, t, e, desc in paquet])
            connexion.executemany(
                "UPDATE offres SET verdict2 = ?, raison2 = ?, niveau = ?, ville = ?, departement = ? WHERE url = ?",
                [(v["verdict"], v["raison"], v["niveau"], v["ville"], v["departement"], u)
                 for (u, *_), v in zip(paquet, verdicts) if v])
            connexion.commit()
    except GeminiIndisponible as e:
        print(f"⚠️ {e} — le reste sera lu au prochain passage.")
    navigateur.close()

# 4.4 Envoi : offres junior par zone, section « à vérifier », compteur des offres confirmé
colonnes = ["url", "titre", "entreprise", "raison", "verdict", "niveau", "ville", "departement"]
lignes = [dict(zip(colonnes, l)) for l in connexion.execute(
    "SELECT url, titre, entreprise, raison2, verdict2, niveau, ville, departement FROM offres "
    "WHERE envoyee = 0 AND verdict2 IN ('oui', 'à vérifier') ORDER BY entreprise")]
# Doublon = même titre, même entreprise ET même département (une ESN publie le même poste dans
# plusieurs villes : ce sont des offres différentes pour le candidat)
cle = lambda o: (cle_titre(o["titre"]), (o["entreprise"] or "").lower(), (o["departement"] or "?")[:2])
deja = {cle(dict(zip(colonnes, l))) for l in connexion.execute(
    "SELECT url, titre, entreprise, raison2, verdict2, niveau, ville, departement FROM offres "
    "WHERE envoyee = 1 AND verdict2 IN ('oui', 'à vérifier')")}
a_envoyer = []
for o in lignes:
    if cle(o) not in deja:
        deja.add(cle(o))
        a_envoyer.append(o)
# Option retenue le 10/11 : seules les offres junior (ou sans niveau précisé) sont détaillées ;
# les offres « confirmé » restent en base pour les statistiques et sont seulement comptées.
confirmes = [o for o in a_envoyer if o["niveau"] == "confirmé"]
juniors = [o for o in a_envoyer if o["niveau"] != "confirmé"]
oui = [o for o in juniors if o["verdict"] == "oui"]
a_verifier = [o for o in juniors if o["verdict"] == "à vérifier"]
if oui or a_verifier:
    blocs = []
    for z, drapeau in (("Île-de-France", "🗼"), ("Reste de la France", "🇫🇷")):
        dans_zone = [o for o in oui if zone(o["departement"]) == z]
        if dans_zone:
            blocs.append(f"{drapeau} {z.upper()} — JUNIOR ({len(dans_zone)})\n")
            blocs += [bloc(o) for o in dans_zone]
    if a_verifier:
        blocs.append(f"🔎 À VÉRIFIER ({len(a_verifier)}) — description illisible ou métier ambigu\n")
        blocs += [bloc(o) for o in a_verifier]
    if confirmes:
        idf = sum(zone(o["departement"]) == "Île-de-France" for o in confirmes)
        blocs.append(f"🔵 + {len(confirmes)} offre(s) confirmé non affichée(s) "
                     f"(Île-de-France {idf}, reste {len(confirmes) - idf})\n")
    entete = f"💼 Veilleur CDI — {len(oui)} offre(s) sûre(s), {len(a_verifier)} à vérifier\n"
    if all(envoyer(m) for m in decouper(entete, blocs)):
        connexion.executemany("UPDATE offres SET envoyee = 1 WHERE url = ?", [(o["url"],) for o in lignes])
        connexion.commit()
    else:
        print("❌ Échec d'envoi Telegram : ces offres seront renvoyées au prochain passage.")
elif lignes:                           # doublons ou confirmés seulement : rien à envoyer, on les marque vus
    connexion.executemany("UPDATE offres SET envoyee = 1 WHERE url = ?", [(o["url"],) for o in lignes])
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
print(f"Envoyées : {len(oui)} oui + {len(a_verifier)} à vérifier (junior) ; {len(confirmes)} confirmé comptées")