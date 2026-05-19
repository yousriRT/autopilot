"""
Fatigue Detector
================

Détecte l'épuisement créatif d'une ad pour pauser avant que le CPC explose.

Trois signaux complémentaires (Meta n'expose pas un score officiel) :
- frequency : impressions / reach (plus c'est haut, plus l'audience a vu la même créa)
- ctr_momentum : CTR(7 derniers jours) / CTR(7 jours d'avant) — en chute = fatigue
- days_active : durée en vol (les créas plus vieilles fatiguent plus vite)

Le score composite (0-100) sert au bandit Thompson pour déclencher une pause
AVANT que le CPL exploser ne soit visible dans les leads.

Référence: Meta documente la "creative fatigue" comme cause #1 d'augmentation
du CPM en compte mature : https://www.facebook.com/business/help/463519856008541
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# Seuils — calibrés sur la base que la frequency Meta "saine" est < 3-4
# pour du UGC court, et que CTR baisse de >25% en 7j est un signal fort.
FREQUENCY_WARNING = 4.0       # impressions/reach > 4 → ad commence à saturer
FREQUENCY_CRITICAL = 6.0      # > 6 → audience a vu la créa 6 fois en moyenne
CTR_DROP_WARNING = 0.85       # CTR_recent / CTR_previous < 0.85 → -15%
CTR_DROP_CRITICAL = 0.70      # < 0.70 → -30% sur 7j
DAYS_ACTIVE_WARNING = 14
DAYS_ACTIVE_CRITICAL = 30
MIN_IMPRESSIONS_TO_TRUST_SIGNAL = 1000


class FatigueDetector:
    """Calcule un score de fatigue 0-100 par ad à partir de l'historique d'insights."""

    def compute_score(self, ad_history: list, created_at: Optional[str] = None) -> dict:
        """
        Args:
            ad_history: liste d'entrées {ts, spend, impressions, clicks, leads, reach}
                        triée du plus ancien au plus récent.
            created_at: ISO timestamp de création de l'ad.

        Returns:
            {
                "score": 0-100 (0 = fraîche, 100 = épuisée),
                "level": "fresh" | "warning" | "critical",
                "frequency": float | None,
                "ctr_momentum": float | None,
                "days_active": int,
                "reasons": [str],  # raisons lisibles
                "should_pause": bool,
            }
        """
        reasons = []
        score_components = []

        # 1. Frequency cumulative
        cumulative_impressions = sum(h.get("impressions", 0) for h in ad_history)
        cumulative_reach = sum(h.get("reach", 0) for h in ad_history)
        frequency = (cumulative_impressions / cumulative_reach) if cumulative_reach > 0 else None

        if cumulative_impressions < MIN_IMPRESSIONS_TO_TRUST_SIGNAL:
            # Pas assez de data — fatigue par défaut très basse
            return {
                "score": 0,
                "level": "fresh",
                "frequency": frequency,
                "ctr_momentum": None,
                "days_active": self._days_since(created_at),
                "reasons": ["pas assez d'impressions pour mesurer"],
                "should_pause": False,
            }

        if frequency is not None:
            if frequency >= FREQUENCY_CRITICAL:
                score_components.append(50)
                reasons.append(f"frequency {frequency:.1f} (critique > {FREQUENCY_CRITICAL})")
            elif frequency >= FREQUENCY_WARNING:
                # Interpolation linéaire entre 0 et 50
                norm = (frequency - FREQUENCY_WARNING) / (FREQUENCY_CRITICAL - FREQUENCY_WARNING)
                score_components.append(25 + 25 * norm)
                reasons.append(f"frequency {frequency:.1f} (élevée)")
            else:
                score_components.append(0)

        # 2. CTR momentum (7 derniers jours vs 7 jours d'avant)
        ctr_momentum = self._compute_ctr_momentum(ad_history)
        if ctr_momentum is not None:
            if ctr_momentum < CTR_DROP_CRITICAL:
                score_components.append(40)
                reasons.append(f"CTR en chute -{int((1-ctr_momentum)*100)}% sur 7j")
            elif ctr_momentum < CTR_DROP_WARNING:
                norm = (CTR_DROP_WARNING - ctr_momentum) / (CTR_DROP_WARNING - CTR_DROP_CRITICAL)
                score_components.append(20 + 20 * norm)
                reasons.append(f"CTR en baisse {int((1-ctr_momentum)*100)}% sur 7j")
            else:
                score_components.append(0)

        # 3. Days active
        days_active = self._days_since(created_at)
        if days_active >= DAYS_ACTIVE_CRITICAL:
            score_components.append(20)
            reasons.append(f"{days_active}j en vol (très vieille)")
        elif days_active >= DAYS_ACTIVE_WARNING:
            norm = (days_active - DAYS_ACTIVE_WARNING) / (DAYS_ACTIVE_CRITICAL - DAYS_ACTIVE_WARNING)
            score_components.append(10 + 10 * norm)
            reasons.append(f"{days_active}j en vol")
        else:
            score_components.append(0)

        # Score composite : on prend la somme cappée à 100
        score = min(100, int(sum(score_components)))

        if score >= 70:
            level = "critical"
            should_pause = True
        elif score >= 40:
            level = "warning"
            should_pause = False
        else:
            level = "fresh"
            should_pause = False

        return {
            "score": score,
            "level": level,
            "frequency": frequency,
            "ctr_momentum": ctr_momentum,
            "days_active": days_active,
            "reasons": reasons,
            "should_pause": should_pause,
        }

    def _compute_ctr_momentum(self, ad_history: list) -> Optional[float]:
        """CTR moyen des 7 derniers jours / CTR moyen des 7 jours d'avant."""
        if len(ad_history) < 4:
            return None

        now = datetime.now()
        recent_cutoff = now - timedelta(days=7)
        previous_cutoff = now - timedelta(days=14)

        recent = [h for h in ad_history if self._parse_ts(h.get("ts")) >= recent_cutoff]
        previous = [
            h for h in ad_history
            if previous_cutoff <= self._parse_ts(h.get("ts")) < recent_cutoff
        ]

        if not recent or not previous:
            return None

        recent_imp = sum(h.get("impressions", 0) for h in recent)
        previous_imp = sum(h.get("impressions", 0) for h in previous)
        recent_clicks = sum(h.get("clicks", 0) for h in recent)
        previous_clicks = sum(h.get("clicks", 0) for h in previous)

        if recent_imp < 200 or previous_imp < 200:
            return None  # bruit statistique

        recent_ctr = recent_clicks / recent_imp
        previous_ctr = previous_clicks / previous_imp

        if previous_ctr == 0:
            return None
        return recent_ctr / previous_ctr

    def _days_since(self, iso_ts: Optional[str]) -> int:
        if not iso_ts:
            return 0
        try:
            return (datetime.now() - datetime.fromisoformat(iso_ts)).days
        except (ValueError, TypeError):
            return 0

    def _parse_ts(self, ts) -> datetime:
        if isinstance(ts, datetime):
            return ts
        if not ts:
            return datetime.min
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return datetime.min
