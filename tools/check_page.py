"""
Diagnostic Meta : pourquoi create_ad_creative renvoie « la Page doit être
publique ». On sait déjà que la Page est publiée et accessible ; le souci
est le lien compte-pub <-> Page. Ce script localise les deux assets
(quel Business possède quoi) pour donner le fix exact. Lectures seules.

Usage (sur le VPS) :
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


def get(path, fields):
    return requests.get(
        f"https://graph.facebook.com/{V}/{path}",
        params={"fields": fields, "access_token": TOKEN},
        timeout=30,
    )


# 1. Le compte pub : dans quel Business vit-il ? Est-il actif ?
show(
    f"AD ACCOUNT {ACT_ID}",
    get(ACT_ID, "name,account_status,disable_reason,business{id,name},"
                "owner,funding_source,currency"),
)

# 2. La Page : dans quel Business vit-elle ?
show(
    f"PAGE {PAGE_ID}",
    get(PAGE_ID, "name,is_published,link,verification_status"),
)

# 3. Le Business propriétaire de la Page (via le token System User)
show(
    "BUSINESSES du System User (/me/businesses)",
    get("me/businesses", "id,name"),
)

# 4. Les Pages déjà rattachées au Business du compte pub
#    (client_pages + owned_pages selon le partage)
show(
    f"AD ACCOUNT -> assigned pages ({ACT_ID}/assigned_pages)",
    get(f"{ACT_ID}/assigned_pages", "id,name"),
)

print("\n" + "=" * 60)
print("LECTURE : compare AD ACCOUNT.business.id et le Business de la Page.")
print("  - s'ils diffèrent (ou si AD ACCOUNT n'a pas de .business) -> il faut")
print("    rattacher la Page et le compte pub au MÊME Business.")
print("  - account_status doit valoir 1 (actif). Sinon, compte désactivé.")
print("Colle TOUTE la sortie.")
