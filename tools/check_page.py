"""
Diagnostic Meta DÉCISIF — rejoue exactement l'appel POST /adcreatives qui
échoue, mais avec un video_id bidon (0 vidéo générée, 0 dépense). Meta
valide App/Page/permissions AVANT la vidéo :
  - si erreur "app en mode développement / Page must be public"
        => cause = App pas en Live (fix gratuit côté developers.facebook)
  - si erreur "video ... not found / invalid"
        => couche App/Page OK, le souci est vidéo/miniature
Affiche le JSON d'erreur Meta COMPLET (error_user_title/msg/subcode).

Usage (VPS) :
    cd /root/automatisation && git pull && .venv/bin/python tools/check_page.py
"""
import os
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

CONFIG = json.load(open(ROOT / "config" / "config.json", encoding="utf-8"))
TOKEN = os.getenv("META_ACCESS_TOKEN") or CONFIG["meta"]["access_token"]
ACT_ID = CONFIG["meta"]["ad_account_id"]
PAGE_ID = str(CONFIG["meta"]["page_id"])
FORM_ID = str(CONFIG["meta"].get("lead_gen_form_id", ""))
V = "v19.0"

# Reproduit EXACTEMENT object_story_spec de meta_publisher.create_ad_creative,
# avec un video_id volontairement faux.
story = {
    "page_id": PAGE_ID,
    "video_data": {
        "video_id": "000000000000000",          # bidon -> aucune vidéo réelle
        "image_url": "https://via.placeholder.com/720x1280.png",
        "title": "Diagnostic",
        "message": "Diagnostic create_ad_creative (aucune pub créée).",
        "link_description": "Diagnostic",
        "call_to_action": {
            "type": "GET_QUOTE",
            "value": {"lead_gen_form_id": FORM_ID},
        },
    },
}

print("=" * 60)
print(f"POST {ACT_ID}/adcreatives (video_id bidon — 0 dépense)")
print("=" * 60)
r = requests.post(
    f"https://graph.facebook.com/{V}/{ACT_ID}/adcreatives",
    data={"object_story_spec": json.dumps(story), "access_token": TOKEN},
    timeout=30,
)
print("HTTP", r.status_code)
try:
    j = r.json()
    print(json.dumps(j, indent=2, ensure_ascii=False))
    err = j.get("error", {})
    print("\n--- RÉSUMÉ ---")
    print("message      :", err.get("message"))
    print("type         :", err.get("type"))
    print("code/subcode :", err.get("code"), "/", err.get("error_subcode"))
    print("user_title   :", err.get("error_user_title"))
    print("user_msg     :", err.get("error_user_msg"))
except Exception:
    print(r.text[:1500])

print("\n" + "=" * 60)
print("INTERPRÉTATION :")
print(" - 'app ... development mode' / 'doit être publique' (parle de")
print("   l'application) => passer l'App en LIVE sur developers.facebook.com")
print(" - 'video ... does not exist/invalid' => App/Page OK, problème vidéo")
print(" - autre => colle tout, je tranche.")
print("Colle TOUTE la sortie.")
