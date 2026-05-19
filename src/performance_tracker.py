"""
Performance Tracker
===================

Stocke les perfs et fournit:
- les "winning angles" pour nourrir Claude quand il génère de nouveaux concepts.
- le score de fatigue par ad (via FatigueDetector) pour le bandit.
- les leads pondérés par qualité (via LeadQualityScorer) pour le bandit.

C'est cette boucle qui rend le système auto-apprenant côté créa (en plus du
bandit côté budget).
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from fatigue_detector import FatigueDetector
from lead_quality import LeadQualityScorer
from lead_store import LeadStore

log = logging.getLogger(__name__)


class PerformanceTracker:
    def __init__(
        self,
        config: dict,
        lead_scorer: Optional[LeadQualityScorer] = None,
        lead_store: Optional[LeadStore] = None,
        supabase_pusher=None,
    ):
        self.data_path = Path(__file__).parent.parent / "data" / "performance.json"
        self.data_path.parent.mkdir(exist_ok=True)
        self.data = self._load()
        self.fatigue = FatigueDetector()
        self.lead_scorer = lead_scorer or LeadQualityScorer()
        self.lead_store = lead_store or LeadStore()
        self.supabase_pusher = supabase_pusher  # optionnel — push vers CRM Rappelle

    def _load(self):
        if self.data_path.exists():
            with open(self.data_path) as f:
                return json.load(f)
        return {"ads": {}}

    def _save(self):
        with open(self.data_path, "w") as f:
            json.dump(self.data, f, indent=2, default=str)

    def register_ad(self, ad_id: str, vertical: str, angle: str,
                    initial_budget: float):
        self.data["ads"][ad_id] = {
            "vertical": vertical,
            "angle": angle,
            "initial_budget": initial_budget,
            "created_at": datetime.now().isoformat(),
            "leads": 0,                     # raw count Meta
            "quality_weighted_leads": 0.0,  # somme des scores qualité (utilisé par bandit)
            "spend": 0.0,
            "impressions": 0,
            "reach": 0,
            "history": [],
            "scored_lead_ids": [],          # éviter de double-scorer
        }
        self._save()

    def update_performance(self, ad_id: str, insights: dict, leads_data: Optional[list] = None):
        """
        Met à jour les insights agrégés ET, si fournis, score les nouveaux leads
        pour mettre à jour quality_weighted_leads.

        Args:
            ad_id: ID Meta de l'ad
            insights: dict des perfs (spend, impressions, reach, clicks, leads, ctr, frequency)
            leads_data: liste optionnelle des leads bruts Meta à scorer (format Lead Ads API).
                        Si None, le scoring est sauté (le bandit utilisera le fallback raw).
        """
        if ad_id not in self.data["ads"]:
            return
        ad = self.data["ads"][ad_id]

        # Insights cumulatifs (Meta renvoie déjà cumulé sur la fenêtre)
        ad["leads"] = insights.get("leads", ad.get("leads", 0))
        ad["spend"] = insights.get("spend", ad.get("spend", 0))
        ad["impressions"] = insights.get("impressions", ad.get("impressions", 0))
        ad["reach"] = insights.get("reach", ad.get("reach", 0))

        # Snapshot historique pour le calcul de momentum CTR
        ad["history"].append({
            "ts": datetime.now().isoformat(),
            **insights
        })
        ad["history"] = ad["history"][-100:]  # plafond mémoire

        # Scoring qualité — incrémental (on ne re-score pas un lead déjà vu)
        # ET persistance dans le lead_store (CRM-substitute).
        if leads_data:
            new_score_total = 0.0
            new_count = 0
            for lead in leads_data:
                lead_id = lead.get("id")
                if not lead_id or lead_id in ad["scored_lead_ids"]:
                    continue
                s = self.lead_scorer.score(lead)
                new_score_total += s
                new_count += 1
                ad["scored_lead_ids"].append(lead_id)
                # Persiste dans le store local (idempotent sur lead_id)
                self.lead_store.add(
                    lead_data=lead,
                    ad_id=ad_id,
                    vertical=ad.get("vertical", "?"),
                    quality_score=s,
                )
                # Push vers CRM Supabase (idempotent côté DB via UNIQUE constraint)
                if self.supabase_pusher is not None:
                    self.supabase_pusher.push(
                        lead_data=lead,
                        ad_id=ad_id,
                        vertical=ad.get("vertical", "?"),
                        quality_score=s,
                    )
            if new_count > 0:
                ad["quality_weighted_leads"] = ad.get("quality_weighted_leads", 0.0) + new_score_total
                log.info(
                    f"[{ad_id}] +{new_count} leads scorés, qualité moy {new_score_total/new_count:.2f}"
                )
            ad["scored_lead_ids"] = ad["scored_lead_ids"][-500:]

        self._save()

    # ----- Lectures pour bandit / orchestrator -----

    def get_quality_weighted_leads(self, ad_id: str) -> float:
        """
        Renvoie les leads pondérés par qualité. Si aucun scoring n'a été fait
        (leads_retrieval non dispo), fallback sur leads bruts × 0.6 (assumption
        moyenne télécom : 60% des soumissions Meta sont qualifiées).
        """
        ad = self.data["ads"].get(ad_id)
        if not ad:
            return 0.0
        if ad.get("scored_lead_ids"):
            return ad.get("quality_weighted_leads", 0.0)
        return ad.get("leads", 0) * 0.6  # fallback prudent

    def get_fatigue(self, ad_id: str) -> dict:
        """Score de fatigue 0-100 + raisons + flag should_pause."""
        ad = self.data["ads"].get(ad_id)
        if not ad:
            return {"score": 0, "level": "fresh", "should_pause": False, "reasons": ["unknown ad"]}
        return self.fatigue.compute_score(
            ad_history=ad.get("history", []),
            created_at=ad.get("created_at"),
        )

    def get_winning_angles(self, vertical: str, last_n_days: int = 14) -> list:
        """
        Top angles par CPL bas pour cette verticale. Utilise les LEADS PONDÉRÉS
        plutôt que les leads bruts — un angle qui génère du déchet ne remonte plus
        comme "gagnant".
        """
        cutoff = datetime.now() - timedelta(days=last_n_days)
        candidates = []
        for ad_id, ad in self.data["ads"].items():
            if ad.get("vertical") != vertical:
                continue
            try:
                created = datetime.fromisoformat(ad["created_at"])
            except (ValueError, KeyError):
                continue
            if created < cutoff:
                continue
            quality_leads = self.get_quality_weighted_leads(ad_id)
            if quality_leads < 3:  # min 3 leads pondérés pour considérer
                continue
            cpl = ad["spend"] / quality_leads
            candidates.append({
                "angle": ad["angle"],
                "cpl_qualifie": cpl,
                "leads_qualifies": quality_leads,
            })
        candidates.sort(key=lambda x: x["cpl_qualifie"])
        return [
            f"{c['angle']} (CPL qualifié {c['cpl_qualifie']:.2f}€, {c['leads_qualifies']:.1f} leads pondérés)"
            for c in candidates[:5]
        ]
