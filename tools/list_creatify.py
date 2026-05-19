"""
Liste TOUTES les vidéos Creatify déjà générées (rien n'est régénéré,
0 dépense). Pour chacune : statut, le script prononcé, et l'URL .mp4
cliquable. Permet de réutiliser l'existant au lieu de repayer.

Usage (VPS) :
    cd /root/automatisation && git pull && .venv/bin/python tools/list_creatify.py
"""
import os
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

API_ID = os.getenv("CREATIFY_API_ID")
API_KEY = os.getenv("CREATIFY_API_KEY")
BASE = "https://api.creatify.ai/api"
H = {"X-API-ID": API_ID, "X-API-KEY": API_KEY}


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d[k]
    return None


r = requests.get(f"{BASE}/lipsyncs/", headers=H, timeout=30)
print("HTTP", r.status_code)
if not r.ok:
    print(r.text[:800])
    raise SystemExit

jobs = r.json()
if isinstance(jobs, dict):
    jobs = jobs.get("results") or jobs.get("data") or []

# Plus récents d'abord
def created(j):
    return pick(j, "created_at", "created", "updated_at") or ""

jobs = sorted(jobs, key=created, reverse=True)

print(f"\n{len(jobs)} job(s) Creatify trouvé(s) (récent -> ancien)\n")
for i, j in enumerate(jobs[:20], 1):
    jid = pick(j, "id")
    status = pick(j, "status")
    out = pick(j, "output", "video_output", "url")
    # le texte prononcé selon le schéma de l'API
    text = pick(j, "text") or pick(j.get("input", {}) if isinstance(j.get("input"), dict) else {}, "text")
    print("=" * 64)
    print(f"#{i}  id={jid}  status={status}  ({created(j)})")
    print(f"VIDÉO : {out or '(pas encore prête / pas d_URL)'}")
    print(f"SCRIPT: {text or '(non exposé par l_API — voir clés brutes ci-dessous)'}")
    if not text:
        # dump des clés utiles si le schéma diffère
        print("clés:", list(j.keys()))
print("=" * 64)
print("\nClique chaque VIDÉO pour la regarder. Colle-moi les blocs SCRIPT")
print("(au moins fibre + mobile) : je corrige le texte/ton à partir de là.")
print("Ces vidéos sont DÉJÀ payées et déjà uploadées sur Meta — on les")
print("réutilise, on ne régénère rien.")
