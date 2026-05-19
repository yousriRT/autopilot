"""
Diagnostic Meta — VERDICT final : la Page est-elle rattachée au Business
qui possède le compte pub ? `promote_pages` vide => non rattachée. On le
confirme en listant owned_pages + client_pages du Business du compte pub.
Lectures seules, 0 dépense.

Usage (VPS) :
    cd /root/automatisation && git pull && .venv/bin/python tools/check_page.py
"""
import os
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

CONFIG = json.load(
    open(Path(__file__).parent.parent / "config" / "config.json", encoding="utf-8")
)
TOKEN = os.getenv("META_ACCESS_TOKEN") or CONFIG["meta"]["access_token"]
PAGE_ID = str(CONFIG["meta"]["page_id"])
ACT_ID = CONFIG["meta"]["ad_account_id"]
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
        return r.status_code, {"raw": r.text[:500]}


# Business propriétaire du compte pub
_, acct = get(ACT_ID, "name,account_status,business{id,name}")
biz = (acct.get("business") or {})
biz_id = biz.get("id")
print("=" * 60)
print(f"Compte pub {ACT_ID} -> Business {biz_id} ({biz.get('name')})")
print(f"account_status = {acct.get('account_status')} (1 = actif)")

if not biz_id:
    print("\n>>> Le compte pub n'est dans AUCUN Business. C'est la cause.")
    raise SystemExit

# Pages possédées + clientes de ce Business
sc_o, owned = get(f"{biz_id}/owned_pages", "id,name")
sc_c, client = get(f"{biz_id}/client_pages", "id,name")
owned_l = owned.get("data", []) if isinstance(owned, dict) else []
client_l = client.get("data", []) if isinstance(client, dict) else []

print("\n--- owned_pages du Business (%s) ---" % sc_o)
print(json.dumps(owned_l, indent=2, ensure_ascii=False))
print("\n--- client_pages du Business (%s) ---" % sc_c)
print(json.dumps(client_l, indent=2, ensure_ascii=False))

all_ids = {str(p.get("id")) for p in owned_l + client_l}

print("\n" + "=" * 60)
if PAGE_ID in all_ids:
    print(f"VERDICT : la Page {PAGE_ID} EST dans le Business du compte pub.")
    print("  => le blocage vient d'ailleurs (lien Page<->compte pub à faire")
    print("     explicitement, ou propagation Meta en cours ~quelques min).")
else:
    print(f"VERDICT : la Page {PAGE_ID} N'EST PAS dans le Business {biz_id}.")
    print("  => CAUSE CONFIRMÉE. Il faut AJOUTER la Page à ce Business.")
print("Colle TOUTE cette sortie.")
