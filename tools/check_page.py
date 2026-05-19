"""
Diagnostic Meta — la Page/compte pub/Business sont OK (déjà confirmé).
On cherche la VRAIE cause du 400 sur create_ad_creative :
  (1) le message d'erreur Meta COMPLET (stocké dans le dead_letter),
  (2) le statut du formulaire instantané (un form en brouillon => rejet),
  (3) infos App (mode dev éventuel).
Lectures seules, 0 dépense.

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
FORM_ID = str(CONFIG["meta"].get("lead_gen_form_id", ""))
PAGE_ID = str(CONFIG["meta"]["page_id"])
V = "v19.0"


def get(path, fields):
    r = requests.get(
        f"https://graph.facebook.com/{V}/{path}",
        params={"fields": fields, "access_token": TOKEN},
        timeout=30,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:800]}


print("=" * 60)
print("1. ERREUR META COMPLÈTE (dernier échec create_ad_creative)")
print("=" * 60)
dlq = ROOT / "data" / "dead_letter.json"
if dlq.exists():
    try:
        items = json.loads(dlq.read_text(encoding="utf-8"))
        if isinstance(items, dict):
            items = items.get("items", items.get("_items", []))
        creatives = [
            it for it in items
            if isinstance(it, dict) and it.get("operation") == "launch_creative"
        ]
        last = creatives[-3:] if creatives else []
        for it in last:
            print(json.dumps(it, indent=2, ensure_ascii=False)[:2500])
            print("-" * 60)
        if not last:
            print("Aucune entrée launch_creative dans le dead_letter.")
    except Exception as e:
        print(f"Lecture dead_letter impossible: {e}")
else:
    print("Pas de data/dead_letter.json.")

print("\n" + "=" * 60)
print("2. FORMULAIRE INSTANTANÉ (un statut != ACTIVE bloque la créa)")
print("=" * 60)
if FORM_ID:
    sc, form = get(
        FORM_ID,
        "id,name,status,locale,privacy_policy{url,link_text},"
        "page,questions,follow_up_action_url",
    )
    print(f"GET form {FORM_ID} -> {sc}")
    print(json.dumps(form, indent=2, ensure_ascii=False)[:2500])
    # Tous les forms de la Page, avec leur statut
    sc2, forms = get(f"{PAGE_ID}/leadgen_forms", "id,name,status")
    print(f"\nFormulaires de la Page -> {sc2}")
    print(json.dumps(forms, indent=2, ensure_ascii=False)[:2000])
else:
    print("Pas de lead_gen_form_id en config.")

print("\n" + "=" * 60)
print("3. APP liée au token")
print("=" * 60)
sc, dbg = get("debug_token", "")
# debug_token ne prend pas 'fields' ; refais l'appel correctement
r = requests.get(
    f"https://graph.facebook.com/{V}/debug_token",
    params={"input_token": TOKEN, "access_token": TOKEN}, timeout=30,
)
app_id = (r.json().get("data", {}) or {}).get("app_id")
print(f"app_id = {app_id}")
if app_id:
    sca, app = get(app_id, "id,name,link,app_type,category")
    print(f"GET app {app_id} -> {sca}")
    print(json.dumps(app, indent=2, ensure_ascii=False)[:1200])

print("\n" + "=" * 60)
print("LECTURE : regarde (1) error_user_title / error_user_msg / subcode,")
print("et (2) form.status — s'il n'est pas 'ACTIVE', c'est LA cause.")
print("Colle TOUTE la sortie.")
