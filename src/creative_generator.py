"""
Creative Generator
==================

1. Claude génère le concept (angle, hook, script, copy)
2. Arcads.ai (ou HeyGen) génère la vidéo UGC à partir du script

5 verticales (fibre, mobile, TV, sécurité, famille_4lignes). Angles qui
convertissent historiquement dans le télécom :
- Économies (frustration de la facture qui monte)
- Pain point technique (lenteur, perte de signal, panne)
- Pression sociale (le voisin qui a la fibre)
- Simplicité (changement sans coupure)
- Famille (ado qui dépasse son forfait, partage de données, suivi parental)

CONTRAINTE STRICTE : aucune marque télécom n'est nommée dans les créas.
Le service est présenté comme une "offre" générique. Le branding se fait
côté landing page / formulaire de soumission, pas dans la pub.
"""

import os
import json
import time
import logging
import requests
import anthropic
from pydantic import BaseModel, Field
from typing import Optional

from resilience import retry_with_backoff

log = logging.getLogger(__name__)


class ConceptOutput(BaseModel):
    angle: str = Field(description="nom court de l'angle, ex: 'frustration_facture'")
    hook_first_3s: str
    video_script: str
    primary_text: str = Field(max_length=125)
    headline: str = Field(max_length=40)
    description: str = Field(max_length=30)
    avatar_persona: str


# Stable. Si on franchit ~4096 tokens (few-shot, charte éditoriale, exemples
# de créas historiques), le cache_control ephemeral plus bas s'active automatiquement.
CONCEPT_SYSTEM = """Tu es expert en publicité Meta pour des offres télécom au Canada (fibre, mobile, TV, sécurité résidentielle, forfaits famille multi-lignes).

CONTEXTE FIXE:
- Marché: Canada (Québec)
- Cible: dépend de la verticale (précisée dans le message utilisateur)
- Objectif: lead form Meta (soumission)
- Format: vidéo UGC 15-30 secondes, ratio 9:16
- Langue: français québécois naturel, pas de jargon corporate
- Positionnement: "une offre télécom" — JAMAIS de marque nommée. Le branding se fait sur la landing page après le lead, pas dans la pub.

CONTRAINTE BRANDING (CRITIQUE):
- Ne nomme JAMAIS de marque télécom — ni concurrent ni partenaire — ex interdits: Bell, Rogers, Vidéotron, Telus, Koodo, Fido, Lucky Mobile, Public Mobile, etc.
- Si l'avatar parle de son ancien fournisseur, dire "mon ancien fournisseur" / "l'autre compagnie" — jamais le nom.
- Pour le CTA: "soumets ta demande", "obtiens ta soumission", "vois si t'es éligible" — pas "passe chez X".

POLICIES META À RESPECTER ABSOLUMENT:
- Pas de "vous" qui implique des attributs personnels (santé, situation financière individuelle)
- Pas de promesses absolues ("garantie 100%", "économisez X$ sûr")
- Pas de pression artificielle ("dernière chance", "offre expire demain")
- Pas de comparaison nominative avec concurrents (cf. contrainte branding ci-dessus)
- Claims chiffrés réalistes uniquement (pas "économisez 500$/mois")

ANGLES RECOMMANDÉS PAR VERTICALE (à varier, pas répéter):
- fibre: lenteur, coupures, déménagement, télétravail
- mobile: facture qui monte, dépassement données, partage en couple
- tv: bouquet trop cher, sport/séries spécifiques, simplicité de changement
- securite: cambriolage de quartier, voyages tranquilles, parents âgés
- famille_4lignes: ado qui explose son forfait data, gestion des écrans, partage facile entre 4 lignes, contrôle parental optionnel, économies vs 4 lignes séparées

RÈGLE DE DIVERSITÉ: ne reproduis jamais un angle déjà gagnant à l'identique. Varie le hook, l'avatar, le pain point."""


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
        self.arcads_api_key = os.getenv("ARCADS_API_KEY") or config.get("arcads", {}).get("api_key")
        self.video_provider = config.get("video_provider", "arcads")
        # Config HeyGen (fournisseur retenu). Clé via env en priorité.
        self.heygen_cfg = config.get("heygen", {})
        self.heygen_api_key = os.getenv("HEYGEN_API_KEY") or self.heygen_cfg.get("api_key")
        # ElevenLabs : voix québécoise fr-CA (HeyGen n'a pas de voix fr-CA).
        # L'audio TTS est envoyé à l'avatar HeyGen qui fait le lip-sync.
        self.elevenlabs_cfg = config.get("elevenlabs", {})
        self.elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY") or self.elevenlabs_cfg.get("api_key")
        self.cost_tracker = cost_tracker
        self.diversity = diversity

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
        return response.parsed_output.model_dump()

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
                    f"Garde le même angle mais ajuste le wording."
                )
            }],
            output_format=ConceptOutput,
        )
        if self.cost_tracker:
            self.cost_tracker.record_anthropic(
                "claude-opus-4-7", response.usage, purpose=f"regenerate_safe:{source}"
            )
        return response.parsed_output.model_dump()

    def generate_video(self, brief: dict) -> str:
        """
        Génère la vidéo UGC via Arcads.
        Retourne l'URL publique de la vidéo générée.
        """
        if self.video_provider == "arcads":
            return self._generate_arcads(brief)
        elif self.video_provider == "heygen":
            return self._generate_heygen(brief)
        else:
            raise ValueError(f"Provider inconnu: {self.video_provider}")

    def _generate_arcads(self, brief: dict) -> str:
        """API Arcads.ai - documentation: https://docs.arcads.ai/"""
        headers = {
            "Authorization": f"Bearer {self.arcads_api_key}",
            "Content-Type": "application/json"
        }

        # Création du job avec retry
        @retry_with_backoff(max_attempts=3, retryable_exceptions=(requests.RequestException,))
        def _post_job():
            resp = requests.post(
                "https://api.arcads.ai/v1/videos",
                headers=headers,
                json={
                    "script": brief["video_script"],
                    "actor_persona": brief["avatar_persona"],
                    "language": "fr-CA",
                    "format": "9:16",
                    "duration_max": 30,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

        job = _post_job()
        job_id = job["job_id"]
        log.info(f"Arcads job lancé: {job_id}")

        # Polling
        @retry_with_backoff(max_attempts=2, retryable_exceptions=(requests.RequestException,))
        def _poll():
            r = requests.get(
                f"https://api.arcads.ai/v1/videos/{job_id}",
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
            return r.json()

        for _ in range(60):  # max 5 min
            time.sleep(5)
            status = _poll()
            if status["status"] == "completed":
                log.info(f"Vidéo prête: {status['video_url']}")
                if self.cost_tracker:
                    self.cost_tracker.record_arcads(video_id=job_id)
                return status["video_url"]
            elif status["status"] == "failed":
                raise RuntimeError(f"Arcads failed: {status.get('error')}")

        raise TimeoutError("Arcads timeout après 5 min")

    def _pick_heygen_avatar(self, brief: dict) -> str:
        """avatar_id fixe, ou rotation déterministe dans avatar_pool (diversité)."""
        pool = self.heygen_cfg.get("avatar_pool")
        if pool:
            seed = (brief.get("avatar_persona") or "") + (brief.get("angle") or "")
            return pool[hash(seed) % len(pool)]
        return self.heygen_cfg.get("avatar_id")

    def _elevenlabs_tts(self, text: str) -> bytes:
        """Synthèse vocale fr-CA via ElevenLabs. Retourne l'audio MP3 (bytes)."""
        voice_id = self.elevenlabs_cfg.get("voice_id")
        if not self.elevenlabs_api_key or not voice_id:
            raise RuntimeError(
                "Config ElevenLabs incomplète : ELEVENLABS_API_KEY + "
                "elevenlabs.voice_id requis."
            )
        model_id = self.elevenlabs_cfg.get("model_id", "eleven_multilingual_v2")
        # Réglages de voix (vitesse/dynamisme). Défauts orientés "pub punchée".
        voice_settings = self.elevenlabs_cfg.get("voice_settings", {
            "stability": 0.4,
            "similarity_boost": 0.8,
            "style": 0.35,
            "use_speaker_boost": True,
            "speed": 1.12,
        })
        body = {"text": text, "model_id": model_id, "voice_settings": voice_settings}

        @retry_with_backoff(max_attempts=3, retryable_exceptions=(requests.RequestException,))
        def _post():
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": self.elevenlabs_api_key,
                         "Content-Type": "application/json"},
                json=body,
                timeout=60,
            )
            r.raise_for_status()
            return r.content

        return _post()

    def _heygen_upload_audio(self, audio: bytes) -> str:
        """Upload l'audio à HeyGen, retourne l'asset_id utilisable en voice=audio."""
        @retry_with_backoff(max_attempts=3, retryable_exceptions=(requests.RequestException,))
        def _post():
            r = requests.post(
                "https://upload.heygen.com/v1/asset",
                headers={"X-Api-Key": self.heygen_api_key,
                         "Content-Type": "audio/mpeg"},
                data=audio,
                timeout=60,
            )
            r.raise_for_status()
            return r.json()

        res = _post()
        data = res.get("data", res)
        asset = data.get("id") or data.get("asset_id") or data.get("url")
        if not asset:
            raise RuntimeError(f"Upload audio HeyGen : réponse inattendue {res}")
        return asset

    def _heygen_voice_block(self, brief: dict) -> dict:
        """
        Construit le bloc 'voice' HeyGen. Par défaut on passe par ElevenLabs
        (voix québécoise fr-CA) puisque HeyGen n'a pas de voix fr-CA ;
        fallback voix HeyGen native si voice_source != 'elevenlabs'.
        """
        if self.heygen_cfg.get("voice_source", "elevenlabs") == "elevenlabs":
            audio = self._elevenlabs_tts(brief["video_script"])
            asset_id = self._heygen_upload_audio(audio)
            return {"type": "audio", "audio_asset_id": asset_id}
        voice_id = self.heygen_cfg.get("voice_id")
        if not voice_id:
            raise RuntimeError("heygen.voice_id requis quand voice_source != elevenlabs.")
        return {"type": "text", "input_text": brief["video_script"], "voice_id": voice_id}

    def _generate_heygen(self, brief: dict) -> str:
        """
        API HeyGen v2 (pay-as-you-go). Génère une vidéo UGC 9:16 fr-CA à
        partir du script + voix configurée, puis poll jusqu'à complétion.
        Doc: https://docs.heygen.com/
        """
        avatar_id = self._pick_heygen_avatar(brief)
        if not self.heygen_api_key or not avatar_id:
            raise RuntimeError(
                "Config HeyGen incomplète : HEYGEN_API_KEY + heygen.avatar_id "
                "(ou avatar_pool) requis."
            )

        voice_block = self._heygen_voice_block(brief)
        dimension = self.heygen_cfg.get("dimension", {"width": 720, "height": 1280})
        headers = {
            "X-Api-Key": self.heygen_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": avatar_id,
                        "avatar_style": self.heygen_cfg.get("avatar_style", "normal"),
                    },
                    "voice": voice_block,
                }
            ],
            "dimension": dimension,
        }

        @retry_with_backoff(max_attempts=3, retryable_exceptions=(requests.RequestException,))
        def _post_job():
            resp = requests.post(
                "https://api.heygen.com/v2/video/generate",
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

        job = _post_job()
        if job.get("error"):
            raise RuntimeError(f"HeyGen generate error: {job['error']}")
        video_id = job["data"]["video_id"]
        log.info(f"HeyGen job lancé: {video_id}")

        @retry_with_backoff(max_attempts=2, retryable_exceptions=(requests.RequestException,))
        def _poll():
            r = requests.get(
                "https://api.heygen.com/v1/video_status.get",
                headers=headers,
                params={"video_id": video_id},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()

        for _ in range(90):  # max ~7.5 min
            time.sleep(5)
            status = _poll()
            data = status.get("data", {})
            state = data.get("status")
            if state == "completed":
                video_url = data["video_url"]
                log.info(f"Vidéo HeyGen prête: {video_url}")
                if self.cost_tracker:
                    self.cost_tracker.record_heygen(video_id=video_id)
                return video_url
            elif state == "failed":
                raise RuntimeError(f"HeyGen failed: {data.get('error')}")

        raise TimeoutError("HeyGen timeout après ~7 min")
