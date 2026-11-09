import os
import requests
from dotenv import load_dotenv

load_dotenv()   # lit le fichier .env
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

def envoyer(texte):
    """Envoie un message Telegram. Renvoie True si l'envoi a réussi."""
    reponse = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": texte, "disable_web_page_preview": True},
        timeout=15,
    )
    return reponse.ok

# Test : ne s'exécute que si on lance "python alerte.py" directement
if __name__ == "__main__":
    ok = envoyer("✅ Test réussi : ton agent de veille alternance peut t'écrire !")
    print("Message envoyé" if ok else "Échec de l'envoi")
