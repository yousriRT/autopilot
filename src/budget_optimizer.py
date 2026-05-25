"""
Budget Optimizer - Thompson Sampling (Multi-Arm Bandit)
========================================================

C'est ICI que se trouve la magie. Pas de seuils hardcodés.

Principe : chaque ad est un "bras" du bandit. On modélise sa performance
(probabilité de générer un lead par euro dépensé) avec une distribution
Beta. À chaque cycle, on tire un échantillon de chaque distribution et
on alloue plus de budget aux bras qui ressortent les meilleurs.

Avantages vs règles fixes :
- Auto-équilibre exploration (tester nouvelles ads) vs exploitation (scale gagnantes)
- Pas besoin de définir "CPL cible" : le système trouve le meilleur tout seul
- S'adapte tout seul (pas de "CPL bon" hardcodé : ça dépend de la créa)
- Robuste au bruit statistique (ne pause pas une ad sur un mauvais jour)

Référence : Thompson Sampling pour bandits stochastiques (Russo et al. 2018)
"""

import json
import math
import logging
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import Callable, Optional

log = logging.getLogger(__name__)


class ThompsonSamplingOptimizer:
    """
    Allocation de budget par Thompson Sampling.

    Pour chaque ad, on maintient :
    - alpha = nombre de leads + prior
    - beta = (dépense en € / cost_normalizer) - leads + prior
    
    Échantillonner Beta(alpha, beta) donne un estimé du "taux de leads par €".
    """

    # Prior bayésien (équivaut à "on suppose 1 lead pour 30€ avant de savoir")
    PRIOR_ALPHA = 1.0
    PRIOR_BETA = 30.0

    # Normalisation : 1 unité = 1€ dépensé
    COST_NORMALIZER = 1.0

    # Combien d'ads max actives par verticale (évite de saupoudrer)
    MAX_ACTIVE_ADS_PER_VERTICAL = 8

    # Budget minimum par ad (plancher Meta + cohérence des décisions)
    MIN_AD_BUDGET = 5.0

    # Combien on garde "en exploration" en permanence (évite de figer)
    MIN_EXPLORATION_BUDGET_RATIO = 0.20

    # Données minimales avant de prendre des décisions agressives
    MIN_SPEND_BEFORE_PAUSE = 30.0  # en €
    MIN_IMPRESSIONS_BEFORE_DECISION = 1000

    def __init__(self, config: dict):
        self.config = config
        self.data_path = Path(__file__).parent.parent / "data" / "bandit_state.json"
        self.data_path.parent.mkdir(exist_ok=True)
        self.state = self._load_state()

        # Budget total par jour défini par l'utilisateur
        # (la SEULE chose qu'il fixe : combien il veut dépenser au max)
        self.daily_total_budget = config.get("daily_total_budget", 100.0)

    def _load_state(self) -> dict:
        if self.data_path.exists():
            with open(self.data_path) as f:
                return json.load(f)
        return {"ads": {}, "verticals": {}}

    def _save_state(self):
        with open(self.data_path, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    # ----- Sampling -----

    def _sample_quality(self, ad_id: str) -> float:
        """
        Tire un échantillon de la distribution Beta de l'ad.
        Plus le sample est haut, meilleure est la performance estimée.

        Utilise quality_weighted_leads (somme des scores qualité) au lieu du
        compte brut de leads. Beta accepte les non-entiers (np.random.beta).
        Fallback sur leads bruts × 0.6 si pas de scoring (estimation prudente).
        """
        ad = self.state["ads"].get(ad_id, {})
        spend = ad.get("spend", 0.0)

        # Préfère leads pondérés qualité, fallback sur raw × 0.6
        qwl = ad.get("quality_weighted_leads")
        if qwl is None:
            qwl = ad.get("leads", 0) * 0.6
        effective_leads = max(0.0, float(qwl))

        alpha = self.PRIOR_ALPHA + effective_leads
        beta = self.PRIOR_BETA + (spend / self.COST_NORMALIZER) - effective_leads
        beta = max(beta, 0.1)

        return np.random.beta(alpha, beta)

    # ----- Décisions -----

    def how_many_to_explore(self, vertical: str) -> int:
        """
        Combien de NOUVELLES créas lancer pour cette verticale ?
        
        Logique : si on a peu d'ads avec assez de data, on explore plus.
        """
        active = [
            ad for ad in self.state["ads"].values()
            if ad.get("vertical") == vertical and ad.get("status") == "active"
        ]

        # Pas encore assez d'ads matures = on explore
        mature = [a for a in active if a.get("spend", 0) > self.MIN_SPEND_BEFORE_PAUSE]

        if len(active) < 3:
            return 3 - len(active)  # min 3 actives pour comparer
        if len(mature) >= 2 and len(active) < 5:
            return 1  # on a 2 gagnantes, on continue à explorer doucement
        if len(active) >= self.MAX_ACTIVE_ADS_PER_VERTICAL:
            return 0
        return 1  # cadence d'exploration normale

    def get_initial_budget(self, vertical: str) -> float:
        """
        Budget de départ pour une nouvelle ad.

        Part d'exploration du budget total, divisée par le nombre d'offres
        CONFIGURÉES (pas un fallback hardcodé), × ~2 ads d'expé. Toujours
        ≥ MIN_AD_BUDGET pour ne pas tomber sous le minimum Meta.
        """
        exploration_budget = self.daily_total_budget * self.MIN_EXPLORATION_BUDGET_RATIO
        configured = self.config.get("meta", {}).get("targeting", {})
        if configured:
            n_verticals = max(1, len(configured))
        else:
            active_verticals = {
                a.get("vertical") for a in self.state["ads"].values()
                if a.get("status") == "active" and a.get("vertical")
            }
            n_verticals = max(1, len(active_verticals))
        raw = exploration_budget / (n_verticals * 2)
        return round(max(self.MIN_AD_BUDGET, raw), 2)

    def decide_allocations(
        self,
        vertical: str,
        fatigue_provider: Optional[Callable[[str], dict]] = None,
    ) -> dict:
        """
        Pour chaque ad de la verticale : scale, reduce, pause, ou hold ?

        Algorithme :
        1. Pause prioritaire si la fatigue créa est critique (avant même le bandit)
        2. Sample Thompson pour chaque ad sur les leads QUALIFIÉS
        3. Softmax pour transformer en proportions de budget
        4. Pause si spend significatif sans qualité

        Args:
            vertical: nom de la verticale
            fatigue_provider: callable optionnel ad_id -> {score, level, should_pause, reasons}
                              (typiquement tracker.get_fatigue). Si None, fatigue ignorée.
        """
        decisions = {}
        ads = {
            ad_id: ad for ad_id, ad in self.state["ads"].items()
            if ad.get("vertical") == vertical and ad.get("status") == "active"
        }

        if not ads:
            return decisions

        # Étape 0 : pause prioritaire pour fatigue critique (avant calculs bandit)
        # On retire les ads pausées du pool actif pour ne pas leur réallouer du budget.
        if fatigue_provider is not None:
            fatigue_paused = []
            for ad_id in list(ads.keys()):
                f = fatigue_provider(ad_id)
                if f.get("should_pause"):
                    decisions[ad_id] = {
                        "action": "pause",
                        "reason": f"creative_fatigue ({', '.join(f.get('reasons', []))})",
                        "fatigue_score": f.get("score"),
                    }
                    fatigue_paused.append(ad_id)
            for ad_id in fatigue_paused:
                ads.pop(ad_id)

        if not ads:
            return decisions

        # Étape 1 : sample Thompson sur leads pondérés qualité
        samples = {ad_id: self._sample_quality(ad_id) for ad_id in ads}

        # Étape 2 : softmax → proportions de budget (dict keyé par ad_id)
        temperature = 2.0
        ad_ids = list(ads.keys())
        sample_array = np.array([samples[a] for a in ad_ids])
        scaled = sample_array * temperature * 100
        exp = np.exp(scaled - scaled.max())
        props = exp / exp.sum()
        proportions = {a: float(p) for a, p in zip(ad_ids, props)}

        # Étape 3 : budget de cette verticale = budget total / nb verticales actives
        active_verticals = {
            a.get("vertical") for a in self.state["ads"].values()
            if a.get("status") == "active" and a.get("vertical")
        }
        n_verticals = max(1, len(active_verticals))
        budget_for_vertical = self.daily_total_budget / n_verticals

        def _qwl(ad):
            q = ad.get("quality_weighted_leads")
            return q if q is not None else ad.get("leads", 0) * 0.6

        # Étape 4 : pauses (retire les ads concernées du pool d'allocation)
        survivors = {}
        for ad_id, ad in ads.items():
            spend = ad.get("spend", 0)
            qwl = _qwl(ad)
            impressions = ad.get("impressions", 0)
            cpl_qualifie = (spend / qwl) if qwl > 0 else float("inf")

            if spend >= self.MIN_SPEND_BEFORE_PAUSE * 2 and qwl < 0.5:
                decisions[ad_id] = {
                    "action": "pause",
                    "reason": "no_quality_leads_after_significant_spend",
                    "cpl_qualifie": cpl_qualifie,
                }
                continue

            if (spend > self.MIN_SPEND_BEFORE_PAUSE
                and impressions > self.MIN_IMPRESSIONS_BEFORE_DECISION
                and proportions[ad_id] < 0.05
                and len(ads) > 3):
                decisions[ad_id] = {
                    "action": "pause",
                    "reason": "consistently_underperforming",
                    "cpl_qualifie": cpl_qualifie,
                }
                continue

            survivors[ad_id] = ad

        if not survivors:
            return decisions

        # Étape 5 : budgets cibles bornés par ad (±50%/cycle, plancher MIN_AD_BUDGET)
        surv_prop_sum = sum(proportions[a] for a in survivors) or 1.0
        clamped = {}
        for ad_id, ad in survivors.items():
            current_budget = ad.get("daily_budget", self.MIN_AD_BUDGET)
            target = (proportions[ad_id] / surv_prop_sum) * budget_for_vertical
            max_change = current_budget * 0.5
            nb = max(current_budget - max_change,
                     min(current_budget + max_change, target))
            clamped[ad_id] = max(self.MIN_AD_BUDGET, nb)

        # Étape 6 : GARANTIE anti-surdépense — la somme des budgets ne dépasse
        # jamais le budget de la verticale (sinon Meta dépenserait > plafond).
        total = sum(clamped.values())
        if total > budget_for_vertical:
            factor = budget_for_vertical / total
            # Troncature au centime (jamais round-up) pour que la somme reste
            # garantie ≤ budget malgré l'arrondi.
            clamped = {a: math.floor(v * factor * 100) / 100 for a, v in clamped.items()}
        else:
            clamped = {a: round(v, 2) for a, v in clamped.items()}

        # Étape 7 : action scale / reduce / hold
        for ad_id, ad in survivors.items():
            current_budget = ad.get("daily_budget", self.MIN_AD_BUDGET)
            new_budget = clamped[ad_id]
            spend = ad.get("spend", 0)
            qwl = _qwl(ad)
            cpl_qualifie = (spend / qwl) if qwl > 0 else float("inf")

            if new_budget > current_budget * 1.1:
                action = "scale"
            elif new_budget < current_budget * 0.9:
                action = "reduce"
            else:
                action = "hold"

            decisions[ad_id] = {
                "action": action,
                "new_budget": new_budget,
                "cpl_qualifie": cpl_qualifie,
                "thompson_sample": samples[ad_id],
                "budget_share": proportions[ad_id],
            }

        return decisions

    # ----- Updates -----

    def register_ad(self, ad_id: str, vertical: str, initial_budget: float):
        """Enregistre une nouvelle ad dans le bandit."""
        self.state["ads"][ad_id] = {
            "vertical": vertical,
            "status": "active",
            "leads": 0,
            "quality_weighted_leads": 0.0,
            "spend": 0.0,
            "impressions": 0,
            "daily_budget": initial_budget,
            "created_at": datetime.now().isoformat()
        }
        self._save_state()

    def update_from_insights(
        self,
        ad_id: str,
        insights: dict,
        quality_weighted_leads: Optional[float] = None,
    ):
        """
        Met à jour les stats à partir des insights Meta.

        Args:
            ad_id: ID Meta
            insights: dict spend/impressions/leads/...
            quality_weighted_leads: somme des scores qualité depuis tracker.
                                    Si None, le bandit utilisera leads × 0.6 fallback.
        """
        if ad_id not in self.state["ads"]:
            return
        ad = self.state["ads"][ad_id]
        ad["leads"] = insights.get("leads", 0)
        ad["spend"] = insights.get("spend", 0.0)
        ad["impressions"] = insights.get("impressions", 0)
        if quality_weighted_leads is not None:
            ad["quality_weighted_leads"] = float(quality_weighted_leads)
        ad["last_update"] = datetime.now().isoformat()
        self._save_state()
