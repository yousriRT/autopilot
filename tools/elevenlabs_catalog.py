"""
Liste les voix du compte ElevenLabs, priorité aux voix françaises /
québécoises (fr-CA), pour remplir config.elevenlabs.voice_id.

Usage: python tools/elevenlabs_catalog.py
Lecture seule — aucun coût (pas de synthèse).
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

KEY = os.getenv("ELEVENLABS_API_KEY")
if not KEY or KEY.startswith("___"):
    sys.exit("ELEVENLABS_API_KEY absent ou non rempli (.env).")

r = requests.get(
    "https://api.elevenlabs.io/v1/voices",
    headers={"xi-api-key": KEY},
    timeout=30,
)
r.raise_for_status()
voices = r.json().get("voices", [])


def is_fr(v):
    labels = v.get("labels", {}) or {}
    blob = json.dumps(labels).lower() + (v.get("description", "") or "").lower()
    fl = v.get("fine_tuning", {}) or {}
    langs = json.dumps(fl.get("language", "")).lower()
    return "fr" in langs or "french" in blob or "français" in blob \
        or "canad" in blob or "quebec" in blob or "québec" in blob


out = []
for v in voices:
    labels = v.get("labels", {}) or {}
    out.append({
        "voice_id": v.get("voice_id"),
        "name": v.get("name"),
        "labels": labels,
        "preview": v.get("preview_url"),
        "fr_match": is_fr(v),
    })

fr = [o for o in out if o["fr_match"]]

# --- Voice Library partagée : chercher des voix québécoises/canadiennes ---
shared = []
try:
    sr = requests.get(
        "https://api.elevenlabs.io/v1/shared-voices",
        headers={"xi-api-key": KEY},
        params={"language": "fr", "page_size": 100},
        timeout=30,
    )
    sr.raise_for_status()
    for v in sr.json().get("voices", []):
        accent = (v.get("accent") or "").lower()
        desc = (v.get("description") or "").lower()
        name = (v.get("name") or "").lower()
        blob = accent + desc + name
        is_qc = any(k in blob for k in ("canad", "quebec", "québec", "qc"))
        if is_qc or accent in ("", "fr", "french"):
            shared.append({
                "voice_id": v.get("voice_id"),
                "public_owner_id": v.get("public_owner_id"),
                "name": v.get("name"),
                "accent": v.get("accent"),
                "description": v.get("description"),
                "preview": v.get("preview_url"),
                "qc": is_qc,
            })
    shared.sort(key=lambda x: (not x["qc"], x["name"] or ""))
except Exception as e:  # noqa: BLE001
    print(f"(shared-voices indisponible: {e})")

dest = Path(__file__).parent.parent / "data" / "elevenlabs_catalog.json"
dest.write_text(
    json.dumps({"account_voices": out, "shared_fr": shared},
               ensure_ascii=True, indent=2),
    encoding="utf-8",
)
qc = [s for s in shared if s["qc"]]
print(f"OK: compte={len(out)} voix ({len(fr)} FR) | "
      f"library FR={len(shared)} dont {len(qc)} QC/Canada -> {dest}")
for s in qc[:25]:
    print(f"  [QC] voice_id={s['voice_id']} | {s['name']} | accent={s['accent']}")
