"""
Test ISOLÉ de génération vidéo : ElevenLabs (voix QC) -> HeyGen (avatar).
NE TOUCHE PAS Meta, ne publie rien. Coûte ~0,55 $ réel (1 vidéo).

Usage: python tools/test_video.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from creative_generator import CreativeGenerator

config = json.load(open(Path(__file__).parent.parent / "config" / "config.json", encoding="utf-8"))

# Script de test : québécois naturel, AUCUNE marque télécom, conforme policies Meta.
brief = {
    "angle": "frustration_facture",
    "avatar_persona": "femme 40 ans, Québec, ton naturel",
    "video_script": (
        "Franchement, ma facture d'internet arrêtait pas de monter. "
        "Un moment donné j'ai dit c'est assez. J'ai rempli une petite "
        "demande en ligne, deux minutes, pis là j'ai reçu une soumission "
        "ben moins chère pour la même affaire. Si t'es tanné de payer trop "
        "cher, vas-y, ça coûte rien d'essayer."
    ),
}

print("Provider:", config.get("video_provider"))
print("Avatar:", config["heygen"]["avatar_id"])
print("Voix:", config["elevenlabs"]["voice_id"], "(Caroline QC)")
print("Génération en cours (ElevenLabs -> upload HeyGen -> rendu)...")

import requests

gen = CreativeGenerator(config)
url = gen.generate_video(brief)

dest = Path(__file__).parent.parent / "data" / "test_video.mp4"
r = requests.get(url, timeout=120)
r.raise_for_status()
dest.write_bytes(r.content)
print("\n=== VIDÉO PRÊTE (locale) ===")
print(dest.resolve())
print(f"({len(r.content)//1024} Ko) — ouvre ce fichier directement.")
