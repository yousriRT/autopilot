"""
Diversity Enforcement
=====================

Sans contrainte, le LLM converge vers le meilleur angle gagnant et arrête
d'explorer (mode collapse). On force la rotation par catégorie d'angle.

5 catégories structurelles :
- problem        : pain technique, frustration produit (lenteur, coupures, etc.)
- social_pressure: pression sociale, FOMO, comparaison avec entourage
- economic_gain  : économies, promo, optimisation budget
- ease           : facilité, simplicité, sans friction (changement, install)
- family         : dynamique familiale (ado, conjoint, partage, contrôle parental)

À chaque launch, on tire la catégorie LRU (la moins utilisée récemment dans
l'offre concernée). Claude reçoit la catégorie comme contrainte dure.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


CATEGORIES = ["problem", "social_pressure", "economic_gain", "ease", "family"]


# Catégories pertinentes par offre. Offre unique : forfait famille
# 4 lignes + TV + internet (économies, dynamique familiale, simplicité).
VERTICAL_CATEGORIES = {
    "famille_bundle": ["economic_gain", "family", "ease"],
}


CATEGORY_BRIEFS = {
    "problem": "Pain technique ou frustration produit. Avatar décrit un problème concret qu'il vit (lenteur, coupures, débordement de facture, panne).",
    "social_pressure": "Comparaison sociale ou FOMO. Avatar mentionne un voisin, un collègue, un ami qui a une meilleure offre / un meilleur service.",
    "economic_gain": "Économies, optimisation budget. Avatar parle de combien il économise ou de comment il a optimisé.",
    "ease": "Simplicité, facilité, sans friction. Avatar insiste sur la rapidité du changement, l'absence de coupure, l'install rapide.",
    "family": "Dynamique familiale. Avatar parle d'ado, conjoint, partage de données, contrôle parental, gestion d'écrans.",
}


class DiversityEnforcer:
    """Choisit la prochaine catégorie pour une verticale donnée (LRU)."""

    def __init__(self, state_path=None):
        self.state_path = Path(state_path) if state_path else (Path(__file__).parent.parent / "data" / "diversity_state.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _load(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {"launches": []}
        return {"launches": []}

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        tmp.replace(self.state_path)

    def pick_next_category(self, vertical: str) -> str:
        """
        Renvoie la catégorie la moins utilisée dans cette verticale sur les 30
        derniers jours. Si égalité, ordre alphabétique.
        """
        valid = VERTICAL_CATEGORIES.get(vertical, CATEGORIES)

        # Compte les usages récents par catégorie
        cutoff = datetime.now() - timedelta(days=30)
        counts = {c: 0 for c in valid}
        last_used = {c: datetime.min for c in valid}
        for launch in self.state.get("launches", []):
            if launch.get("vertical") != vertical:
                continue
            try:
                ts = datetime.fromisoformat(launch["ts"])
            except (ValueError, KeyError):
                continue
            if ts < cutoff:
                continue
            cat = launch.get("category")
            if cat in counts:
                counts[cat] += 1
                if ts > last_used[cat]:
                    last_used[cat] = ts

        # LRU : moins de count d'abord, puis last_used le plus ancien
        ranked = sorted(valid, key=lambda c: (counts[c], last_used[c]))
        chosen = ranked[0]
        log.info(
            f"[diversity:{vertical}] catégorie choisie={chosen} "
            f"(counts 30j: {counts})"
        )
        return chosen

    def record_launch(self, vertical: str, category: str, ad_id: str = "", angle: str = ""):
        """Enregistre qu'une ad de cette catégorie a été lancée."""
        self.state.setdefault("launches", []).append({
            "ts": datetime.now().isoformat(),
            "vertical": vertical,
            "category": category,
            "ad_id": ad_id,
            "angle": angle,
        })
        # Plafond mémoire
        self.state["launches"] = self.state["launches"][-500:]
        self._save()

    def get_brief(self, category: str) -> str:
        return CATEGORY_BRIEFS.get(category, "")
