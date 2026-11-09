"""Trois environnements, un seul code : seule la configuration change (variable MODE).

    dev  : MODE=dev  python capteur.py   → sur ton PC, mémoire vide dans data_dev/, version rapide
                                            (une seule zone, 10 sites), messages affichés à l'écran.
    test : MODE=test python capteur.py   → sur ton PC, copie de la mémoire de production dans data_test/
                                            (./preparer_test.sh avant), passage complet, messages affichés.
    prod : sur GitHub Actions uniquement → mémoire officielle dans data/, messages envoyés sur Telegram.
"""
import os
import sys
import time
from datetime import datetime

MODE = os.environ.get("MODE", "prod")
EN_PRODUCTION = os.environ.get("GITHUB_ACTIONS") == "true"

if MODE not in ("dev", "test", "prod"):
    sys.exit(f"⛔ MODE inconnu : {MODE} (valeurs possibles : dev, test, prod)")
if MODE == "prod" and not EN_PRODUCTION:
    sys.exit("⛔ La production ne tourne que sur GitHub Actions. Sur ton PC :\n"
             "   MODE=dev python capteur.py            (essai rapide, mémoire vide)\n"
             "   ./preparer_test.sh puis MODE=test python capteur.py   (copie de la production)")
if MODE != "prod" and EN_PRODUCTION:
    sys.exit("⛔ Sur GitHub Actions, seul le mode prod est autorisé.")

DATA = {"dev": "data_dev", "test": "data_test", "prod": "data"}[MODE]
RAPIDE = MODE == "dev"

if MODE == "test" and not os.path.isdir(DATA):
    sys.exit("⛔ Pas de copie de test : lance d'abord ./preparer_test.sh")
os.makedirs(DATA, exist_ok=True)
if MODE != "prod":
    print(f"🧪 MODE {MODE.upper()} : données dans {DATA}/, aucun message Telegram envoyé"
          + (" — version rapide" if RAPIDE else ""))


def envoyer(texte):
    if MODE != "prod":
        print("\n──── MESSAGE TELEGRAM (non envoyé) ────\n" + texte + "\n──────────────")
        return True
    from alerte import envoyer as envoyer_telegram      # importé seulement en production
    return envoyer_telegram(texte)


def noter_passage(con, moteur, debut, nouvelles, envoyees, erreurs):
    """Journal des passages : repérer les sites bloqués et les heures où les offres apparaissent."""
    con.execute("CREATE TABLE IF NOT EXISTS passages (moteur TEXT, debut TEXT, duree_min REAL, "
                "nouvelles INTEGER, envoyees INTEGER, erreurs TEXT)")
    con.execute("INSERT INTO passages VALUES (?,?,?,?,?,?)",
                (moteur, datetime.fromtimestamp(debut).isoformat(timespec="minutes"),
                 round((time.time() - debut) / 60, 1), nouvelles, envoyees, erreurs))
    con.commit()