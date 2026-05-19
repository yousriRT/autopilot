"""
Lead Quality Scorer
===================

Le bandit Thompson optimisait sur les leads bruts comptés par Meta. Problème:
un lead à 3 secondes de soumission ≠ un client. Volume haut, qualification basse.

Ce module score chaque lead 0..1 et le bandit utilise des "leads pondérés"
au lieu du compte brut. Une soumission avec score 0.3 compte comme 0.3 lead.

Deux approches plug-in :
1. Heuristique locale (par défaut) — vérifie email/téléphone/champs remplis.
   Suffisant pour démarrer, ne nécessite pas d'intégration externe.
2. CRM externe (optionnel) — via webhook qui POST le score dans le tracker.
   À brancher quand le CRM est prêt à exposer son scoring.

Le scoring heuristique se trompe rarement vers le haut (si on dit qualité haute,
généralement c'est vrai) et peut sous-estimer (faux négatifs). C'est OK pour le
bandit : on préfère sous-investir sur du faux qualifié que sur-investir sur du
faux qualifié.
"""

import re
import logging
from typing import Optional, Callable

log = logging.getLogger(__name__)


# Domaines email Québec/Canada considérés "résidentiels valides"
TRUSTED_EMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "icloud.com",
    "yahoo.ca", "yahoo.com", "hotmail.ca", "live.ca",
    "videotron.ca", "sympatico.ca", "bell.net", "rogers.com",
    "cogeco.ca",
}

# Domaines louches (souvent disposable / temp mail)
DISPOSABLE_DOMAINS = {
    "mailinator.com", "tempmail.com", "10minutemail.com",
    "guerrillamail.com", "throwaway.email", "trashmail.com",
    "yopmail.com", "fakeinbox.com",
}

# Indicatifs régionaux Québec valides (pour téléphone)
QUEBEC_AREA_CODES = {"418", "438", "450", "514", "579", "581", "819", "873"}


class LeadQualityScorer:
    """
    Score un lead Meta entre 0 et 1.

    Utilisation :
        scorer = LeadQualityScorer()
        score = scorer.score(lead_data)  # 0.0 à 1.0

    Pour brancher un scoring CRM externe :
        scorer = LeadQualityScorer(external_scorer=lambda lead: crm.score(lead))
    """

    def __init__(self, external_scorer: Optional[Callable[[dict], float]] = None):
        self.external_scorer = external_scorer

    def score(self, lead_data: dict) -> float:
        """
        Args:
            lead_data: format Meta Lead Ads, ex:
                {
                    "id": "...",
                    "created_time": "2026-04-25T10:00:00",
                    "field_data": [
                        {"name": "email", "values": ["jane@gmail.com"]},
                        {"name": "phone_number", "values": ["+15145551234"]},
                        {"name": "full_name", "values": ["Jane Tremblay"]},
                        ...
                    ]
                }

        Returns:
            float entre 0.0 (déchet) et 1.0 (lead premium).
        """
        if self.external_scorer is not None:
            try:
                return max(0.0, min(1.0, float(self.external_scorer(lead_data))))
            except Exception as e:
                log.warning(f"External scorer failed, fallback heuristic: {e}")

        return self._heuristic_score(lead_data)

    def _heuristic_score(self, lead_data: dict) -> float:
        score = 0.5  # baseline

        fields = self._extract_fields(lead_data)

        # Email
        email = (fields.get("email") or "").lower().strip()
        if email and "@" in email:
            domain = email.rsplit("@", 1)[-1]
            if domain in DISPOSABLE_DOMAINS:
                score -= 0.4
            elif domain in TRUSTED_EMAIL_DOMAINS:
                score += 0.15
            elif "." in domain and len(domain) >= 5:
                score += 0.05  # domaine plausible inconnu
            else:
                score -= 0.2  # domaine bizarre
        else:
            score -= 0.3  # pas d'email = très douteux

        # Téléphone — format canadien 10 chiffres + indicatif QC souhaité
        phone_raw = fields.get("phone_number") or ""
        digits = re.sub(r"\D", "", phone_raw)
        # Strip +1 / 1 leading
        if digits.startswith("1") and len(digits) == 11:
            digits = digits[1:]
        if len(digits) == 10:
            score += 0.15
            area_code = digits[:3]
            if area_code in QUEBEC_AREA_CODES:
                score += 0.1
        elif len(digits) > 0:
            score -= 0.1  # téléphone présent mais bizarre

        # Nom complet
        name = (fields.get("full_name") or fields.get("first_name") or "").strip()
        if len(name) >= 3 and " " in name:
            score += 0.05
        elif len(name) <= 1:
            score -= 0.15  # "a" / "x" → blague

        # Code postal Québec si fourni
        postal = (fields.get("postal_code") or fields.get("zip_code") or "").strip().upper().replace(" ", "")
        if re.match(r"^[GHJ]\d[A-Z]\d[A-Z]\d$", postal):
            score += 0.1  # codes postaux QC commencent par G/H/J

        return max(0.0, min(1.0, score))

    def _extract_fields(self, lead_data: dict) -> dict:
        """Aplatit le format field_data de Meta en dict."""
        out = {}
        for f in lead_data.get("field_data", []):
            name = f.get("name", "").lower().replace(" ", "_")
            values = f.get("values", [])
            if values:
                out[name] = values[0]
        return out

    def aggregate_quality_ratio(self, lead_scores: list) -> float:
        """Moyenne des scores. 1.0 = qualif parfaite, 0 = full déchet."""
        if not lead_scores:
            return 0.0
        return sum(lead_scores) / len(lead_scores)
