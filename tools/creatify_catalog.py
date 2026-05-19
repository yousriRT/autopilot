"""
Liste les personas Creatify (style 'selfie' = UGC) et les voix françaises,
pour remplir config.creatify.{persona_id, voice_id}.

Usage: python tools/creatify_catalog.py
Lecture seule — ne consomme aucun crédit.
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

API_ID = os.getenv("CREATIFY_API_ID")
API_KEY = os.getenv("CREATIFY_API_KEY")
if not API_ID or not API_KEY or API_ID.startswith("___"):
    sys.exit("CREATIFY_API_ID / CREATIFY_API_KEY absents (.env).")

H = {"X-API-ID": API_ID, "X-API-KEY": API_KEY}
BASE = "https://api.creatify.ai/api"


def get(path, params=None):
    r = requests.get(f"{BASE}{path}", headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

# Personas style selfie (UGC) — on prend aussi 'other' au cas où
personas = get("/personas/")
ugc = [p for p in personas
       if (p.get("style") or "").lower() in ("selfie", "")
       or "selfie" in json.dumps(p).lower()]

# Voix : chercher le français / québécois
voices = get("/voices/")
fr = []
for v in voices:
    for a in v.get("accents", []) or []:
        an = (a.get("accent_name") or "").lower()
        if "french" in an or "fr" == an[:2] or "canad" in an or "qu" in an:
            fr.append({
                "voice_id": a.get("id"),
                "voice_name": v.get("name"),
                "accent": a.get("accent_name"),
                "gender": v.get("gender"),
                "preview": a.get("preview_url"),
            })

dest = Path(__file__).parent.parent / "data" / "creatify_catalog.json"
dest.write_text(json.dumps({
    "ugc_personas": [
        {"id": p.get("id"), "name": p.get("creator_name"),
         "gender": p.get("gender"), "age": p.get("age_range"),
         "location": p.get("location"), "style": p.get("style"),
         "preview": p.get("preview")}
        for p in ugc
    ],
    "french_voices": fr,
}, ensure_ascii=True, indent=2), encoding="utf-8")

print(f"OK: {len(ugc)} personas UGC, {len(fr)} voix FR/QC -> {dest}")
for v in fr[:20]:
    print(f"  [voix] id={v['voice_id']} | {v['voice_name']} | "
          f"{v['accent']} | {v['gender']}")
