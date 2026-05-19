"""
Cost Tracker
============

Consolide les coûts des 3 fournisseurs pour avoir UNE vue unique:
- Anthropic : tokens consommés × prix par modèle (extrait de response.usage)
- Arcads : nombre de vidéos × estimation $/vidéo (config)
- Meta : spend remonté par les insights

Utilité :
- Détecter une explosion de coût Anthropic (boucle de regenerate_safe par ex)
- Mettre des plafonds par fournisseur (Anthropic_max_daily_usd)
- Alimenter le dashboard avec un vrai coût total par lead qualifié
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# Tarifs Anthropic (mis à jour avril 2026, voir shared/models.md)
# Format : (input $/1M, output $/1M, cache_write_multiplier, cache_read_multiplier)
ANTHROPIC_PRICING = {
    "claude-opus-4-7": (5.0, 25.0, 1.25, 0.1),
    "claude-opus-4-6": (5.0, 25.0, 1.25, 0.1),
    "claude-sonnet-4-6": (3.0, 15.0, 1.25, 0.1),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.1),
}

# Estimation Arcads (en USD, cf README projet)
ARCADS_USD_PER_VIDEO = 7.0

# Estimation HeyGen API (Avatar III ~1$/min → ~0,50$ pour une UGC 30s)
HEYGEN_USD_PER_VIDEO = 0.5

# Estimation Creatify API Starter (5 crédits / vidéo 30s, 99$/500 crédits)
CREATIFY_USD_PER_VIDEO = 1.0


class CostTracker:
    """Persiste les coûts dans data/cost_log.jsonl (append-only) + agrégats."""

    def __init__(self, config: dict, log_path=None):
        self.config = config
        self.log_path = Path(log_path) if log_path else (Path(__file__).parent.parent / "data" / "cost_log.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_budget_meta = float(config.get("daily_total_budget", 100.0))
        # Plafond optionnel sur Anthropic+Arcads par jour (défaut: 30% du budget Meta)
        self.daily_cap_creation_usd = float(
            config.get("daily_creation_cost_cap_usd", self.daily_budget_meta * 0.3)
        )

    def _append(self, entry: dict):
        entry["ts"] = datetime.now().isoformat()
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def record_anthropic(self, model: str, usage, purpose: str = ""):
        """Enregistre un appel Anthropic. usage = response.usage (Pydantic ou dict)."""
        if hasattr(usage, "model_dump"):
            u = usage.model_dump()
        elif hasattr(usage, "__dict__"):
            u = {k: getattr(usage, k, 0) for k in (
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"
            )}
        else:
            u = dict(usage) if usage else {}

        in_tok = u.get("input_tokens", 0) or 0
        out_tok = u.get("output_tokens", 0) or 0
        cache_w = u.get("cache_creation_input_tokens", 0) or 0
        cache_r = u.get("cache_read_input_tokens", 0) or 0

        pricing = ANTHROPIC_PRICING.get(model, (5.0, 25.0, 1.25, 0.1))
        in_price, out_price, cw_mult, cr_mult = pricing

        cost = (
            in_tok * in_price / 1_000_000
            + out_tok * out_price / 1_000_000
            + cache_w * in_price * cw_mult / 1_000_000
            + cache_r * in_price * cr_mult / 1_000_000
        )

        self._append({
            "type": "anthropic",
            "model": model,
            "purpose": purpose,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_input_tokens": cache_w,
            "cache_read_input_tokens": cache_r,
            "cost_usd": round(cost, 6),
        })
        return cost

    def record_arcads(self, video_id: str = "", duration_s: Optional[int] = None):
        """Une vidéo générée Arcads."""
        cost = ARCADS_USD_PER_VIDEO
        self._append({
            "type": "arcads",
            "video_id": video_id,
            "duration_s": duration_s,
            "cost_usd": cost,
        })
        return cost

    def record_heygen(self, video_id: str = "", duration_s: Optional[int] = None):
        """Une vidéo générée HeyGen (API pay-as-you-go)."""
        cost = HEYGEN_USD_PER_VIDEO
        self._append({
            "type": "heygen",
            "video_id": video_id,
            "duration_s": duration_s,
            "cost_usd": cost,
        })
        return cost

    def record_creatify(self, video_id: str = "", duration_s: Optional[int] = None):
        """Une vidéo générée Creatify (vrai UGC)."""
        cost = CREATIFY_USD_PER_VIDEO
        self._append({
            "type": "creatify",
            "video_id": video_id,
            "duration_s": duration_s,
            "cost_usd": cost,
        })
        return cost

    def get_daily_costs(self, day: Optional[datetime] = None) -> dict:
        """Agrège les coûts du jour donné (défaut = aujourd'hui)."""
        target_date = (day or datetime.now()).date()
        totals = {
            "anthropic": 0.0, "arcads": 0.0, "heygen": 0.0, "creatify": 0.0,
            "anthropic_calls": 0, "arcads_calls": 0, "heygen_calls": 0,
            "creatify_calls": 0,
            "creation_total_usd": 0.0,
        }

        if not self.log_path.exists():
            return totals

        with open(self.log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    entry_date = datetime.fromisoformat(entry["ts"]).date()
                    if entry_date != target_date:
                        continue
                    t = entry.get("type")
                    cost = float(entry.get("cost_usd", 0))
                    if t == "anthropic":
                        totals["anthropic"] += cost
                        totals["anthropic_calls"] += 1
                    elif t == "arcads":
                        totals["arcads"] += cost
                        totals["arcads_calls"] += 1
                    elif t == "heygen":
                        totals["heygen"] += cost
                        totals["heygen_calls"] += 1
                    elif t == "creatify":
                        totals["creatify"] += cost
                        totals["creatify_calls"] += 1
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue

        totals["creation_total_usd"] = (
            totals["anthropic"] + totals["arcads"]
            + totals["heygen"] + totals["creatify"]
        )
        return totals

    def is_creation_capped(self) -> tuple[bool, dict]:
        """
        True si on a atteint le plafond Anthropic+Arcads du jour.
        Sert à bloquer un --action launch si on a déjà brûlé trop de crédit.
        """
        totals = self.get_daily_costs()
        capped = totals["creation_total_usd"] >= self.daily_cap_creation_usd
        return capped, totals
