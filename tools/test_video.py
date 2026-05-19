"""
Test ISOLÉ : Claude génère un vrai concept -> script nettoyé -> ElevenLabs
(voix QC) -> HeyGen (avatar + décor). NE TOUCHE PAS Meta, ne publie rien.
Coûte ~0,75 $ réel (1 concept Claude + 1 vidéo HeyGen).

Usage: python tools/test_video.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from creative_generator import CreativeGenerator
import requests

config = json.load(open(Path(__file__).parent.parent / "config" / "config.json", encoding="utf-8"))

gen = CreativeGenerator(config)

print("1. Claude génère un concept (verticale=fibre, catégorie=problem)...")
brief = gen.generate_concept(vertical="fibre", past_winners=[], category="problem")

raw = brief["video_script"]
clean = gen._clean_script(raw)
print("\n--- SCRIPT BRUT (ce que Claude a écrit) ---")
print(raw)
print("\n--- SCRIPT NETTOYÉ (ce qui sera RÉELLEMENT prononcé) ---")
print(clean)
prov = config.get("video_provider")
print(f"\nProvider: {prov} | config: {config.get(prov, {})}")
print("Génération vidéo en cours...")

url = gen.generate_video(brief)

dest = Path(__file__).parent.parent / "data" / "test_video.mp4"
r = requests.get(url, timeout=120)
r.raise_for_status()
dest.write_bytes(r.content)
print("\n=== VIDÉO PRÊTE (locale) ===")
print(dest.resolve())
print(f"({len(r.content)//1024} Ko) — ouvre ce fichier directement.")
