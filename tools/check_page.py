"""
Diagnostic Meta FINAL — App/Page/Business/Form sont confirmés OK (le probe
video_id bidon est passé jusqu'à la vidéo). Le souci est vidéo/miniature.
Ici on réutilise une VRAIE vidéo déjà uploadée par les runs ratés (aucun
upload, 0 dépense) et on rejoue create_ad_creative EXACTEMENT comme
l'orchestrateur, pour capturer le message Meta complet.

Usage (VPS) :
    cd /root/automatisation && git pull && .venv/bin/python tools/check_page.py
"""
import os
import json
import time
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


def api(method, path, **params):
    params["access_token"] = TOKEN
    if method == "GET":
        r = requests.get(f"https://graph.facebook.com/{V}/{path}",
                          params=params, timeout=30)
    else:
        r = requests.post(f"https://graph.facebook.com/{V}/{path}",
                          data=params, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:800]}


# 1. Vidéos déjà présentes sur le compte pub (uploadées par les runs ratés)
sc, vids = api("GET", f"{ACT_ID}/advideos",
               fields="id,status,title,created_time", limit=10)
print("=" * 60)
print(f"VIDÉOS déjà sur le compte ({sc}) :")
data = vids.get("data", []) if isinstance(vids, dict) else []
print(json.dumps(data, indent=2, ensure_ascii=False)[:1500])
if not data:
    print("\nAucune vidéo déjà uploadée -> impossible de rejouer. "
          "Colle quand même cette sortie.")
    raise SystemExit

video_id = data[0]["id"]
print(f"\n>>> On réutilise video_id = {video_id}")

# 2. Statut + miniature de cette vidéo (comme _video_thumbnail_url)
sc, vinfo = api("GET", video_id, fields="status,thumbnails")
print("\n" + "=" * 60)
print(f"ÉTAT VIDÉO {video_id} ({sc}) :")
print(json.dumps(vinfo, indent=2, ensure_ascii=False)[:1500])
thumbs = ((vinfo.get("thumbnails") or {}).get("data")) or []
thumb_url = thumbs[0]["uri"] if thumbs else "https://via.placeholder.com/720x1280.png"

# 3. Rejoue EXACTEMENT create_ad_creative de meta_publisher
story = {
    "page_id": PAGE_ID,
    "video_data": {
        "video_id": video_id,
        "image_url": thumb_url,
        "title": "Diagnostic",
        "message": "Diagnostic (aucune pub diffusée, aucun budget).",
        "link_description": "Diagnostic",
        "call_to_action": {
            "type": "GET_QUOTE",
            "value": {"lead_gen_form_id": FORM_ID},
        },
    },
}
print("\n" + "=" * 60)
print(f"POST {ACT_ID}/adcreatives avec la VRAIE vidéo (0 dépense) :")
sc, j = api("POST", f"{ACT_ID}/adcreatives",
            object_story_spec=json.dumps(story))
print("HTTP", sc)
print(json.dumps(j, indent=2, ensure_ascii=False)[:2500])
err = j.get("error", {}) if isinstance(j, dict) else {}
print("\n--- RÉSUMÉ ERREUR ---")
print("message    :", err.get("message"))
print("subcode    :", err.get("error_subcode"))
print("user_title :", err.get("error_user_title"))
print("user_msg   :", err.get("error_user_msg"))
if "id" in (j or {}):
    print("\n>>> SUCCÈS : creative créé", j["id"],
          "— le pipeline Meta fonctionne, le bug était ailleurs (transitoire ?)")
print("\nColle TOUTE la sortie.")
