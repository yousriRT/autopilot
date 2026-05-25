"""
Creative Generator
==================

1. Claude génère le concept (angle, prompt visuel, copy)
2. OpenAI (gpt-image-1) génère l'IMAGE publicitaire à partir du prompt visuel

OFFRE UNIQUE : forfait famille **4 lignes mobiles + TV + internet**, pour des
foyers québécois qui paient cher en payant tout séparément. Angles qui
convertissent :
- Économies (combiner 4 lignes + TV + internet coûte moins que tout séparément)
- Famille (ado qui explose son forfait, partage de données, contrôle parental)
- Simplicité (une seule facture, un seul fournisseur, changement sans coupure)

CONTRAINTE STRICTE : aucune marque télécom n'est nommée dans les créas.
Le service est présenté comme une "offre" générique. Le branding se fait
côté landing page / formulaire de soumission, pas dans la pub.

Pas de vidéo, pas d'avatar : la créa visuelle est une image fixe générée
par IA (gpt-image-1). Le texte (titre, accroche, description) reste écrit
par Claude et affiché par Meta autour de l'image.
"""

import os
import base64
import json
import logging
import requests
import anthropic
from pydantic import BaseModel, Field
from typing import Optional

from resilience import retry_with_backoff

log = logging.getLogger(__name__)


class ConceptOutput(BaseModel):
    angle: str = Field(description="nom court de l'angle, ex: 'frustration_facture'")
    # NB : pas de contrainte `max_length` dure sur les champs texte. Si Claude
    # dépasse de quelques caractères, on NE veut PAS perdre la créa sur une
    # ValidationError → on tronque proprement après parsing (_normalize_lengths).
    image_prompt: str = Field(
        description="Prompt EN ANGLAIS pour le générateur d'images gpt-image-1. "
                    "Décrit une photo réaliste, lumineuse et authentique (style "
                    "lifestyle/UGC) illustrant l'angle de l'offre famille. "
                    "AUCUN texte/mot/chiffre visible dans l'image (les modèles "
                    "rendent mal le texte et Meta pénalise). AUCUN logo ni marque. "
                    "Pas de célébrité ni de personne reconnaissable. Contexte "
                    "canadien/québécois quand c'est pertinent."
    )
    primary_text: str = Field(description="≤ 125 caractères")
    headline: str = Field(description="≤ 40 caractères")
    description: str = Field(description="≤ 30 caractères")


# Stable. Si on franchit ~4096 tokens (few-shot, charte éditoriale, exemples
# de créas historiques), le cache_control ephemeral plus bas s'active automatiquement.
CONCEPT_SYSTEM = """Tu es expert en publicité Meta pour UNE offre télécom au Québec : un forfait FAMILLE qui combine 4 lignes mobiles + la télé + l'internet en un seul forfait.

CONTEXTE FIXE:
- Marché: province de Québec (Canada)
- Cible: parents / foyers qui paient cher parce qu'ils payent mobile, télé et internet séparément (souvent avec des ados qui consomment beaucoup de données)
- Offre: regrouper 4 lignes mobiles + TV + internet → moins cher et plus simple que tout payer à part
- Objectif: lead form Meta (soumission)
- Format: IMAGE FIXE publicitaire (single image ad), ratio carré 1:1, pour le fil Meta
- Langue: français québécois naturel pour le copy, pas de jargon corporate
- Positionnement: "une offre" — JAMAIS de marque nommée. Le branding se fait sur la landing page après le lead, pas dans la pub.

CONTRAINTE BRANDING (CRITIQUE):
- Ne nomme JAMAIS de marque télécom — ni concurrent ni partenaire — ex interdits: Bell, Rogers, Vidéotron, Telus, Koodo, Fido, Lucky Mobile, Public Mobile, etc.
- Aucun logo, slogan officiel ou élément de marque reconnaissable, ni dans le copy ni dans l'image.
- Pour le CTA: "soumets ta demande", "obtiens ta soumission", "vois si t'es éligible" — pas "passe chez X".

POLICIES META À RESPECTER ABSOLUMENT:
- Pas de "vous" qui implique des attributs personnels (santé, situation financière individuelle)
- Pas de promesses absolues ("garantie 100%", "économisez X$ sûr")
- Pas de pression artificielle ("dernière chance", "offre expire demain")
- Pas de comparaison nominative avec concurrents (cf. contrainte branding ci-dessus)
- Claims chiffrés réalistes uniquement (pas "économisez 500$/mois")

ANGLES RECOMMANDÉS POUR CETTE OFFRE (à varier, pas répéter):
- économies: payer mobile + TV + internet séparément revient cher ; tout regrouper coûte moins
- famille: ado qui explose son forfait data, gestion des écrans, partage facile entre 4 lignes, contrôle parental optionnel
- simplicité: une seule facture, un seul fournisseur, changement sans coupure ni paperasse

RÈGLE DE DIVERSITÉ: ne reproduis jamais un angle déjà gagnant à l'identique. Varie l'angle, la scène visuelle, le pain point.

CONTRAINTE CRITIQUE SUR LE CHAMP image_prompt:
- `image_prompt` est rédigé EN ANGLAIS (meilleurs résultats de gpt-image-1).
- Décris une PHOTO réaliste et authentique (style lifestyle/UGC, lumière naturelle), pas une illustration ni un montage marketing chargé.
- ZÉRO texte, mot, chiffre, sous-titre ou watermark visible dans l'image : les modèles rendent mal le texte et Meta pénalise les images chargées de texte.
- AUCUN logo, marque, slogan, ni célébrité ou personne reconnaissable.
- La scène doit illustrer l'angle de façon évidente et positive pour une famille (ex: parents et ados détendus à la maison, soirée télé en famille, chacun sur son appareil sans souci de données), contexte canadien/québécois quand c'est pertinent.

TON DU COPY (IMPORTANT):
- primary_text / headline / description: ton NATUREL et POSÉ, comme une vraie personne — PAS une pub sur-jouée.
- INTERDIT: onomatopées et interjections théâtrales, points d'exclamation à répétition, MAJUSCULES d'emphase.
- Phrases simples et fluides, vocabulaire de tous les jours. Un seul pain point évoqué simplement, puis un CTA bref et naturel.
- Si une phrase sonne "pub", réécris-la plus simple."""


class CreativeGenerator:
    def __init__(
        self,
        config: dict,
        claude_client: Optional[anthropic.Anthropic] = None,
        cost_tracker=None,
        diversity=None,
    ):
        self.claude = claude_client or anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY") or config["anthropic"]["api_key"]
        )
        # Fournisseur d'images IA (par défaut OpenAI gpt-image-1).
        self.image_provider = config.get("image_provider", "openai")
        self.openai_cfg = config.get("openai", {})
        self.openai_api_key = os.getenv("OPENAI_API_KEY") or self.openai_cfg.get("api_key")
        self.cost_tracker = cost_tracker
        self.diversity = diversity

    @staticmethod
    def _trim(text: str, limit: int, sentence_aware: bool = False) -> str:
        """Tronque proprement à <= limit caractères, sur une frontière de
        phrase (si sentence_aware) sinon de mot, sans couper un mot."""
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        cut = text[:limit]
        if sentence_aware:
            end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
            if end >= limit * 0.5:
                return cut[:end + 1].strip()
        sp = cut.rfind(" ")
        return (cut[:sp] if sp > 0 else cut).strip()

    @classmethod
    def _normalize_lengths(cls, concept: dict) -> dict:
        """Garantit des champs aux longueurs sûres pour Meta. Évite de
        perdre une créa entière à cause de quelques caractères en trop."""
        concept["primary_text"] = cls._trim(concept.get("primary_text", ""), 125)
        concept["headline"] = cls._trim(concept.get("headline", ""), 40)
        concept["description"] = cls._trim(concept.get("description", ""), 30)
        return concept

    def generate_concept(self, vertical: str, past_winners: list, category: Optional[str] = None) -> dict:
        """
        Claude génère un concept complet.
        Si `category` est fourni, contrainte de diversité forcée (anti mode-collapse).
        """
        winners_str = "\n".join(f"- {w}" for w in past_winners) if past_winners else "(aucun encore, première itération)"

        category_brief = ""
        if category and self.diversity:
            brief = self.diversity.get_brief(category)
            category_brief = (
                f"\n\nCATÉGORIE D'ANGLE IMPOSÉE pour cette créa : **{category}**\n"
                f"Définition: {brief}\n"
                f"Tu DOIS produire une créa qui rentre dans cette catégorie, "
                f"même si tes 'winning angles' sont d'une autre catégorie."
            )

        response = self.claude.messages.parse(
            model="claude-opus-4-7",
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": CONCEPT_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{
                "role": "user",
                "content": (
                    f"Verticale ciblée: **{vertical}**\n\n"
                    f"Angles gagnants récents (à varier, pas répéter):\n{winners_str}"
                    f"{category_brief}\n\n"
                    f"Génère UNE créa publicitaire."
                )
            }],
            output_format=ConceptOutput,
        )
        if self.cost_tracker:
            self.cost_tracker.record_anthropic("claude-opus-4-7", response.usage, purpose=f"concept:{vertical}")
        return self._normalize_lengths(response.parsed_output.model_dump())

    def regenerate_safe(self, original: dict, issues: list, source: str = "policy") -> dict:
        """
        Si validation policy ou Meta Preview a échoué, on régénère avec contrainte.
        `source` peut être "policy" (Claude validator) ou "meta_preview" (Meta API).
        """
        issues_str = "\n".join(f"- {i}" for i in issues)
        source_label = "policy validator (Claude)" if source == "policy" else "Meta Preview API"
        response = self.claude.messages.parse(
            model="claude-opus-4-7",
            max_tokens=1500,
            system=[
                {
                    "type": "text",
                    "text": CONCEPT_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{
                "role": "user",
                "content": (
                    f"Cette créa a été rejetée par {source_label} pour ces raisons:\n{issues_str}\n\n"
                    f"Créa rejetée:\n{json.dumps(original, ensure_ascii=False, indent=2)}\n\n"
                    f"Régénère en corrigeant SPÉCIFIQUEMENT les problèmes ci-dessus. "
                    f"Garde le même angle mais ajuste le wording et la scène visuelle."
                )
            }],
            output_format=ConceptOutput,
        )
        if self.cost_tracker:
            self.cost_tracker.record_anthropic(
                "claude-opus-4-7", response.usage, purpose=f"regenerate_safe:{source}"
            )
        return self._normalize_lengths(response.parsed_output.model_dump())

    def generate_image(self, brief: dict) -> bytes:
        """
        Génère l'image publicitaire à partir du `image_prompt` du brief.
        Retourne les octets bruts de l'image (PNG).
        """
        if self.image_provider == "openai":
            return self._generate_openai(brief)
        raise ValueError(f"Provider image inconnu: {self.image_provider}")

    def _generate_openai(self, brief: dict) -> bytes:
        """
        API OpenAI Images (gpt-image-1). Retourne les octets de l'image.
        Doc: https://platform.openai.com/docs/api-reference/images/create
        gpt-image-1 renvoie toujours du base64 (b64_json), jamais d'URL.
        """
        if not self.openai_api_key:
            raise RuntimeError(
                "Config OpenAI incomplète : OPENAI_API_KEY (ou openai.api_key) requis."
            )
        prompt = (brief.get("image_prompt") or "").strip()
        if not prompt:
            raise RuntimeError("image_prompt manquant dans le brief — impossible de générer l'image.")

        model = self.openai_cfg.get("model", "gpt-image-1")
        size = self.openai_cfg.get("size", "1024x1024")
        quality = self.openai_cfg.get("quality", "medium")
        body = {"model": model, "prompt": prompt, "size": size, "n": 1}
        # `quality` n'existe que pour gpt-image-1 (low/medium/high/auto).
        if quality:
            body["quality"] = quality

        log.info(
            "IMAGE_PROMPT [angle=%s model=%s size=%s]: %s",
            brief.get("angle"), model, size, prompt[:300],
        )

        @retry_with_backoff(max_attempts=3, retryable_exceptions=(requests.RequestException,))
        def _post():
            r = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {self.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120,
            )
            r.raise_for_status()
            return r.json()

        data = _post()
        try:
            b64 = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Réponse OpenAI Images inattendue: {str(data)[:300]}") from e

        image_bytes = base64.b64decode(b64)
        log.info("Image générée: %d octets (angle=%s)", len(image_bytes), brief.get("angle"))
        if self.cost_tracker:
            self.cost_tracker.record_openai_image(model=model, size=size, quality=quality)
        return image_bytes
