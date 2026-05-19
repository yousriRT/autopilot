"""
Diagnostic Meta — on cherche le message d'erreur Meta COMPLET (dans
logs/cron.log, le dead_letter ne stocke qu'une version tronquée) +
le statut réel du formulaire instantané. Lectures seules, 0 dépense.

Usage (VPS) :
    cd /root/automatisation && git pull && .venv/bin/python tools/check_page.py
"""
import os
import re
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

CONFIG = json.load(open(ROOT / "config" / "config.json", encoding="utf-8"))
TOKEN = os.getenv("META_ACCESS_TOKEN") or CONFIG["meta"]["access_token"]
FORM_ID = str(CONFIG["meta"].get("lead_gen_form_id", ""))
PAGE_ID = str(CONFIG["meta"]["page_id"])
V = "v19.0"

print("=" * 60)
print("1. MESSAGE META COMPLET (logs/cron.log : lignes 'Meta API error')")
print("=" * 60)
log = ROOT / "logs" / "cron.log"
if log.exists():
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [
        ln for ln in lines
        if ("Meta API error" in ln or "error_user" in ln
            or "développement" in ln or "doit être" in ln
            or "adcreatives" in ln)
    ]
    # Dédoublonne en gardant l'ordre, garde les 8 derniers (sans le token)
    seen, uniq = set(), []
    for ln in hits:
        clean = re.sub(r"access_token=[A-Za-z0-9]+", "access_token=<caché>", ln)
        key = clean[:300]
        if key not in seen:
            seen.add(key)
            uniq.append(clean)
    for ln in uniq[-8:]:
        print(ln[:1200])
        print("-" * 60)
    if not uniq:
        print("Aucune ligne d'erreur Meta trouvée dans cron.log.")
else:
    print("Pas de logs/cron.log (les runs manuels loguent peut-être ailleurs).")

print("\n" + "=" * 60)
print("2. FORMULAIRE INSTANTANÉ — statut réel (via Page Access Token)")
print("=" * 60)
# Récupère un Page Access Token (le token a pages_read_engagement/manage_ads)
pt = requests.get(
    f"https://graph.facebook.com/{V}/{PAGE_ID}",
    params={"fields": "access_token", "access_token": TOKEN}, timeout=30,
)
page_token = (pt.json() or {}).get("access_token")
print(f"Page Access Token obtenu : {'oui' if page_token else 'NON -> ' + pt.text[:300]}")
if page_token and FORM_ID:
    f = requests.get(
        f"https://graph.facebook.com/{V}/{FORM_ID}",
        params={"fields": "id,name,status,locale,leads_count",
                "access_token": page_token}, timeout=30,
    )
    print(f"\nGET form {FORM_ID} -> {f.status_code}")
    print(json.dumps(f.json(), indent=2, ensure_ascii=False)[:1500])
    lst = requests.get(
        f"https://graph.facebook.com/{V}/{PAGE_ID}/leadgen_forms",
        params={"fields": "id,name,status", "access_token": page_token},
        timeout=30,
    )
    print(f"\nTous les formulaires de la Page -> {lst.status_code}")
    print(json.dumps(lst.json(), indent=2, ensure_ascii=False)[:2000])

print("\n" + "=" * 60)
print("LECTURE :")
print(" - Section 1 : le message Meta en entier. Si on lit 'application")
print("   ... mode développement' => il faut passer l'App en Live.")
print(" - Section 2 : form.status doit être 'ACTIVE' (pas DRAFT/ARCHIVED).")
print("Colle TOUTE la sortie.")
