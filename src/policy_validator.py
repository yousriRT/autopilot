"""
Policy Validator
================

AVANT de payer pour générer une image (gpt-image-1) et
AVANT de pousser vers Meta (rejet = ad credit gaspillé), on valide
le concept contre les policies Meta.

Claude joue ici le rôle d'un reviewer Meta strict.

Sonnet 4.6 + adaptive thinking : tâche de rubrique structurée, pas créative.
Sonnet 4.6 coûte 60% de moins qu'Opus 4.7 et adaptive thinking compense
la rigueur sur les cas borderline.
"""

import os
import json
import logging
import anthropic
from pydantic import BaseModel
from typing import Optional

log = logging.getLogger(__name__)


class PolicyResult(BaseModel):
    valid: bool
    issues: list[str] = []


VALIDATION_SYSTEM = """Tu es un reviewer interne Meta Ads Policy. Sois STRICT.

Le contexte est: pub IMAGE FIXE pour UNE offre télécom au Québec — un forfait famille combinant 4 lignes mobiles + TV + internet. La créa contient un prompt visuel (image_prompt) + du copy (primary_text/headline/description).

Vérifie ces 9 catégories de policy:

1. **Brand Mention (CRITIQUE business rule)**: la créa NE DOIT PAS nommer de marque télécom — ni concurrent ni partenaire. Marques interdites (liste non exhaustive): Bell, Rogers, Vidéotron, Telus, Koodo, Fido, Lucky Mobile, Public Mobile, Chatr, Freedom Mobile. Si une marque est nommée explicitement OU implicitement reconnaissable (slogan officiel, logo décrit), rejette.
2. **Personal Attributes (Meta)**: pas d'affirmations qui impliquent ou identifient des attributs personnels (santé, ethnie, orientation, situation financière individuelle, dette, statut matrimonial)
3. **Misleading Claims (Meta)**: pas de promesses absolues non vérifiables ("garantie", "100% sûr", "économies garanties de X$")
4. **Sensational Content (Meta)**: pas de pression artificielle ("dernière chance", "expire ce soir")
5. **Comparison Claims (Meta)**: pas de comparaison nominative avec d'autres marques (couvert aussi par #1)
6. **Unrealistic Outcomes (Meta)**: les chiffres doivent être plausibles (pas "économisez 1000$/mois")
7. **Before/After (Meta)**: éviter les avant/après trop dramatiques sur factures
8. **Negative Self-Perception (Meta)**: ne pas dire au lecteur qu'il est "stupide" / "mauvais" / "victime"
9. **Mineurs (CRITIQUE — offre famille)**: l'image ne doit pas mettre en scène un mineur comme sujet principal/identifiable. Évoquer des ados ("mon ado", "mes enfants") dans le copy est OK, mais pas une image centrée sur un enfant protagoniste.

Pour chaque issue trouvée, sois précis et cite le passage problématique."""


class PolicyValidator:
    def __init__(self, config: dict, claude_client: Optional[anthropic.Anthropic] = None, cost_tracker=None):
        self.claude = claude_client or anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY") or config["anthropic"]["api_key"]
        )
        self.cost_tracker = cost_tracker

    def validate(self, creative: dict) -> tuple[bool, list]:
        """
        Retourne (is_valid, list_of_issues).
        """
        response = self.claude.messages.parse(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=[
                {
                    "type": "text",
                    "text": VALIDATION_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{
                "role": "user",
                "content": f"Créa à valider:\n{json.dumps(creative, ensure_ascii=False, indent=2)}"
            }],
            output_format=PolicyResult,
        )
        if self.cost_tracker:
            self.cost_tracker.record_anthropic("claude-sonnet-4-6", response.usage, purpose="policy_validate")
        result = response.parsed_output
        return result.valid, result.issues
