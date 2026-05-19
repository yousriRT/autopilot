"""
Diagnostic Meta : pourquoi create_ad_creative renvoie « la Page doit être
publique ». Vérifie l'état de la Page + la validité/portée du token, sans
rien publier ni dépenser (lectures API seulement).

Usage (sur le VPS) :
    cd /root/automatisation && .venv/bin/python tools/check_page.py
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
PAGE_ID = CONFIG["meta"]["page_id"]
ACT_ID = CONFIG["meta"]["ad_account_id"]
V = "v19.0"


def show(title, resp):
    print("\n" + "=" * 60)
    print(title, "->", resp.status_code)
    try:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except Exception:
        print(resp.text[:1000])


# 1. État de la Page (le coeur du diagnostic)
show(
    f"PAGE {PAGE_ID}",
    requests.get(
        f"https://graph.facebook.com/{V}/{PAGE_ID}",
        params={
            "fields": "name,is_published,link,verification_status,"
                      "is_webhooks_subscribed,tasks",
            "access_token": TOKEN,
        },
        timeout=30,
    ),
)

# 2. Le token : portée + à quoi il est rattaché
show(
    "TOKEN debug",
    requests.get(
        f"https://graph.facebook.com/{V}/debug_token",
        params={"input_token": TOKEN, "access_token": TOKEN},
        timeout=30,
    ),
)

# 3. Les Pages accessibles par ce token (la Page cible doit y être)
show(
    "PAGES accessibles (/me/accounts)",
    requests.get(
        f"https://graph.facebook.com/{V}/me/accounts",
        params={"fields": "id,name,is_published,tasks", "access_token": TOKEN},
        timeout=30,
    ),
)

# 4. Le compte pub voit-il la Page comme promouvable ?
show(
    f"PROMOTABLE pages du compte {ACT_ID}",
    requests.get(
        f"https://graph.facebook.com/{V}/{ACT_ID}/promote_pages",
        params={"fields": "id,name,is_published", "access_token": TOKEN},
        timeout=30,
    ),
)

print("\n" + "=" * 60)
print("Colle TOUTE cette sortie. Points clés : PAGE.is_published, "
      "le scopes du TOKEN, et si la Page apparaît dans /me/accounts.")
