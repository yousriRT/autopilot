"""
Liste les voix françaises (fr-CA en priorité) et les avatars HeyGen
disponibles sur le compte, pour remplir config.heygen.{voice_id, avatar_id}.

Usage: python tools/heygen_catalog.py
Lecture seule — n'engendre aucun coût (pas de génération vidéo).
"""
import os
import sys
import json
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

KEY = os.getenv("HEYGEN_API_KEY")
if not KEY:
    sys.exit("HEYGEN_API_KEY absent (.env ou environnement).")

H = {"X-Api-Key": KEY}


def get(url):
    r = requests.get(url, headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


voices = get("https://api.heygen.com/v2/voices").get("data", {}).get("voices", [])
fr = [v for v in voices if "french" in (v.get("language", "") or "").lower()
      or "fr" == (v.get("language", "") or "").lower()[:2]]
fr.sort(key=lambda v: ("canad" not in (v.get("language", "")).lower(),
                        v.get("language", "")))

data = get("https://api.heygen.com/v2/avatars").get("data", {})
avatars = data.get("avatars", []) or data.get("avatar_list", [])

out = {
    "french_voices": [
        {"voice_id": v.get("voice_id"), "language": v.get("language"),
         "gender": v.get("gender"), "name": v.get("name"),
         "preview": v.get("preview_audio")}
        for v in fr
    ],
    "avatars": [
        {"avatar_id": a.get("avatar_id"), "gender": a.get("gender"),
         "name": a.get("avatar_name") or a.get("name")}
        for a in avatars
    ],
}
dest = Path(__file__).parent.parent / "data" / "heygen_catalog.json"
dest.write_text(json.dumps(out, ensure_ascii=True, indent=2), encoding="utf-8")
print(f"OK: {len(fr)} voix FR, {len(avatars)} avatars -> {dest}")
fr_ca = [v for v in fr if "canad" in (v.get("language", "")).lower()]
print(f"Dont {len(fr_ca)} voix French (Canada).")
