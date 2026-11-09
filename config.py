"""Choix de l'environnement : production (GitHub Actions) ou test (sur ton PC).

Production : rien à régler, c'est le comportement par défaut sur GitHub Actions.
Test       : MODE=test python capteur.py   (après ./preparer_test.sh)
             → travaille sur data_test/ (copie de la mémoire de production),
               affiche les messages Telegram au lieu de les envoyer.
             RAPIDE=1 en plus : capteur limité à Nantes, veilleur limité à 10 sites.
"""
import os
import sys
import time
from datetime import datetime
from alerte import envoyer as envoyer_telegram

MODE = os.environ.get("MODE", "prod")
EN_PRODUCTION = os.environ.get("GITHUB_ACTIONS") == "true"

if MODE == "prod" and not EN_PRODUCTION:
    sys.exit("⛔ La production ne tourne que sur GitHub Actions. Pour tester sur ton PC :\n"
             "   ./preparer_test.sh   puis   MODE=test python capteur.py   (ou veilleur.py)")
if MODE not in ("prod", "test"):
    sys.exit(f"⛔ MODE inconnu : {MODE} (valeurs possibles : prod, test)")

DATA = "data" if MODE == "prod" else "data_test"
RAPIDE = MODE == "test" and os.environ.get("RAPIDE") == "1"

if MODE == "test":
    if not os.path.exists(f"{DATA}/veilleur.db"):
        sys.exit("⛔ Pas de copie de test : lance d'abord ./preparer_test.sh")
    print(f"🧪 MODE TEST : données dans {DATA}/, aucun message Telegram envoyé"
          + (" — version rapide" if RAPIDE else ""))


def envoyer(texte):
    if MODE == "test":
        print("\n──── MESSAGE TELEGRAM (mode test, non envoyé) ────\n" + texte + "\n──────────────")
        return True
    return envoyer_telegram(texte)


def noter_passage(con, moteur, debut, nouvelles, envoyees, erreurs):
    """Journal des passages : sert à repérer les sites bloqués et les heures où les offres apparaissent."""
    con.execute("CREATE TABLE IF NOT EXISTS passages (moteur TEXT, debut TEXT, duree_min REAL, "
                "nouvelles INTEGER, envoyees INTEGER, erreurs TEXT)")
    con.execute("INSERT INTO passages VALUES (?,?,?,?,?,?)",
                (moteur, datetime.fromtimestamp(debut).isoformat(timespec="minutes"),
                 round((time.time() - debut) / 60, 1), nouvelles, envoyees, erreurs))
    con.commit()