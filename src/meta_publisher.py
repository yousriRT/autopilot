"""
Meta Publisher - Wrapper autour de la Marketing API
====================================================

Publie les ads, met à jour les budgets, récupère les insights.
Utilise l'API Graph v19+ avec un System User Token (pas un user token,
ces derniers expirent et casseraient le cron).

Documentation: https://developers.facebook.com/docs/marketing-apis/
"""

import os
import json
import time
import base64
import logging
import requests
from typing import Optional

from resilience import retry_with_backoff

log = logging.getLogger(__name__)

META_API_VERSION = "v19.0"
META_BASE_URL = f"https://graph.facebook.com/{META_API_VERSION}"


class MetaPublisher:
    def __init__(self, config: dict):
        self.access_token = os.getenv("META_ACCESS_TOKEN") or config["meta"]["access_token"]
        self.ad_account_id = config["meta"]["ad_account_id"]  # format: act_XXXXXXXX
        self.page_id = config["meta"]["page_id"]
        # Formulaire instantané Meta : les leads passent par lui, sont récupérés
        # via l'API et scorés par qualité avant d'alimenter le bandit.
        self.lead_gen_form_id = config["meta"].get("lead_gen_form_id")
        # Pixel optionnel : inutilisé avec un formulaire instantané (l'optimisation
        # LEAD_GENERATION se fait via la Page). Conservé si présent.
        self.pixel_id = config["meta"].get("pixel_id") or None

        # Mapping verticale → audience/ciblage
        self.targeting_by_vertical = config["meta"]["targeting"]

    @retry_with_backoff(
        max_attempts=3,
        retryable_exceptions=(requests.ConnectionError, requests.Timeout),
    )
    def _request(self, method: str, endpoint: str, **kwargs):
        """
        Wrapper requests avec retry exponentiel + gestion d'erreurs.

        Stratégie de retry :
        - 5xx Meta : on convertit en ConnectionError → retry par le decorator
        - 4xx Meta : HTTPError, PAS retryé (mauvaise requête, ne réussira pas)
        - ConnectionError / Timeout réseau : retry par le decorator
        """
        url = f"{META_BASE_URL}/{endpoint}"
        params = kwargs.pop("params", {})
        params["access_token"] = self.access_token

        timeout = kwargs.pop("timeout", 30)

        if method == "GET":
            r = requests.get(url, params=params, timeout=timeout, **kwargs)
        else:
            data = kwargs.pop("data", {})
            data["access_token"] = self.access_token
            r = requests.post(url, params=params, data=data, timeout=timeout, **kwargs)

        if not r.ok:
            log.error(f"Meta API error: {r.status_code} {r.text[:500]}")
            # 5xx = transient → retryable
            if 500 <= r.status_code < 600:
                raise requests.ConnectionError(
                    f"Meta {r.status_code}: {r.text[:200]}", response=r
                )
            # 4xx = mauvaise requête → ne pas retry
            r.raise_for_status()
        return r.json()

    # ----- Publication -----

    def upload_image(self, image_bytes: bytes) -> str:
        """Upload une image (octets bruts) et retourne son image_hash Meta."""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        result = self._request(
            "POST",
            f"{self.ad_account_id}/adimages",
            data={"bytes": b64}
        )
        images = result.get("images") or {}
        if not images:
            raise RuntimeError(f"Upload image Meta : réponse inattendue {str(result)[:200]}")
        first = next(iter(images.values()))
        image_hash = first.get("hash")
        if not image_hash:
            raise RuntimeError(f"Upload image Meta : hash absent {str(result)[:200]}")
        log.info(f"Image uploaded: {image_hash}")
        return image_hash

    def create_ad_creative(self, image_hash: str, primary_text: str,
                           headline: str, description: str,
                           cta: str = "GET_QUOTE") -> str:
        """Crée le creative image (ce qui sera affiché). Pub à formulaire
        instantané : le CTA ouvre le formulaire Meta natif, pas un lien externe."""
        if not self.lead_gen_form_id:
            raise RuntimeError(
                "lead_gen_form_id manquant dans config.meta — requis pour les "
                "pubs à formulaire instantané (sinon les leads ne sont pas captés)."
            )
        # Pub single-image à lead form : link_data avec image_hash. Le `link`
        # est requis par Meta mais inutilisé (le CTA ouvre le formulaire natif).
        creative = {
            "object_story_spec": {
                "page_id": self.page_id,
                "link_data": {
                    "image_hash": image_hash,
                    "link": f"https://facebook.com/{self.page_id}",
                    "message": primary_text,
                    "name": headline,
                    "description": description,
                    "call_to_action": {
                        "type": cta,
                        "value": {"lead_gen_form_id": self.lead_gen_form_id}
                    }
                }
            }
        }
        result = self._request(
            "POST",
            f"{self.ad_account_id}/adcreatives",
            data={"object_story_spec": json.dumps(creative["object_story_spec"])}
        )
        return result["id"]

    def publish_ad(self, vertical: str, image_bytes: bytes,
                   primary_text: str, headline: str,
                   description: str, daily_budget: float) -> dict:
        """
        Pipeline complet de publication.
        Crée campagne (si pas existante) → ad set → ad.
        Retourne les IDs.
        """
        # 1. Récupère ou crée la campagne pour cette verticale
        campaign_id = self._get_or_create_campaign(vertical)

        # 2. Upload image
        image_hash = self.upload_image(image_bytes)

        # 3. Creative
        creative_id = self.create_ad_creative(
            image_hash, primary_text, headline, description
        )

        # 4. Ad set avec budget
        adset_id = self._create_adset(
            campaign_id=campaign_id,
            vertical=vertical,
            daily_budget=daily_budget
        )

        # 5. Ad
        ad_id = self._create_ad(adset_id, creative_id, vertical)

        return {
            "campaign_id": campaign_id,
            "adset_id": adset_id,
            "ad_id": ad_id,
            "creative_id": creative_id,
            "image_hash": image_hash
        }

    def _get_or_create_campaign(self, vertical: str) -> str:
        """Une campagne par verticale, réutilisée."""
        # Check si elle existe (cache local serait mieux mais ok pour MVP)
        campaigns = self._request(
            "GET",
            f"{self.ad_account_id}/campaigns",
            params={"fields": "id,name,status", "limit": 100}
        )
        target_name = f"AUTOPILOT_{vertical.upper()}"
        for c in campaigns.get("data", []):
            if c["name"] == target_name and c["status"] != "DELETED":
                return c["id"]

        # Création
        result = self._request(
            "POST",
            f"{self.ad_account_id}/campaigns",
            data={
                "name": target_name,
                "objective": "OUTCOME_LEADS",
                "status": "ACTIVE",
                "special_ad_categories": "[]",  # ⚠ ajuster si Meta classe télécom comme spécial
                "buying_type": "AUCTION"
            }
        )
        log.info(f"Campagne créée: {target_name} ({result['id']})")
        return result["id"]

    def _create_adset(self, campaign_id: str, vertical: str,
                      daily_budget: float) -> str:
        """Ad set avec ciblage spécifique à la verticale."""
        targeting = self.targeting_by_vertical[vertical]

        result = self._request(
            "POST",
            f"{self.ad_account_id}/adsets",
            data={
                "name": f"AUTOPILOT_{vertical}_{int(time.time())}",
                "campaign_id": campaign_id,
                "daily_budget": int(daily_budget * 100),  # en cents
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "LEAD_GENERATION",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "targeting": json.dumps(targeting),
                "status": "ACTIVE",
                "promoted_object": json.dumps({"page_id": self.page_id})
            }
        )
        return result["id"]

    def _create_ad(self, adset_id: str, creative_id: str, vertical: str) -> str:
        result = self._request(
            "POST",
            f"{self.ad_account_id}/ads",
            data={
                "name": f"AUTOPILOT_AD_{vertical}_{int(time.time())}",
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative_id}),
                "status": "ACTIVE"
            }
        )
        return result["id"]

    # ----- Lecture -----

    def get_active_ads(self) -> list:
        """Liste toutes les ads actives gérées par l'autopilot."""
        result = self._request(
            "GET",
            f"{self.ad_account_id}/ads",
            params={
                "fields": "id,name,status,adset_id",
                "filtering": '[{"field":"name","operator":"CONTAIN","value":"AUTOPILOT_AD"}]',
                "limit": 200
            }
        )
        return [a for a in result.get("data", []) if a["status"] == "ACTIVE"]

    def get_ad_insights(self, ad_id: str, last_hours: int = 24) -> dict:
        """Récupère les perfs récentes d'une ad. Inclut reach pour calcul de frequency."""
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(hours=last_hours)).strftime("%Y-%m-%d")
        until = datetime.now().strftime("%Y-%m-%d")

        result = self._request(
            "GET",
            f"{ad_id}/insights",
            params={
                "fields": "spend,impressions,reach,clicks,actions,cpm,ctr,frequency",
                "time_range": json.dumps({"since": since, "until": until})
            }
        )

        if not result.get("data"):
            return {"spend": 0, "impressions": 0, "reach": 0, "leads": 0, "clicks": 0, "ctr": 0, "frequency": 0}

        d = result["data"][0]
        leads = 0
        for action in d.get("actions", []):
            if action["action_type"] in ("lead", "onsite_conversion.lead_grouped"):
                leads += int(action["value"])

        return {
            "spend": float(d.get("spend", 0)),
            "impressions": int(d.get("impressions", 0)),
            "reach": int(d.get("reach", 0)),
            "clicks": int(d.get("clicks", 0)),
            "leads": leads,
            "ctr": float(d.get("ctr", 0)),
            "frequency": float(d.get("frequency", 0)),
        }

    def get_ad_leads(self, ad_id: str, last_hours: int = 24, limit: int = 200) -> list:
        """
        Récupère les leads bruts (avec field_data) générés par une ad.
        Permet le scoring qualité côté lead_quality.LeadQualityScorer.

        Nécessite la permission `leads_retrieval` sur le System User Token.
        Documentation: https://developers.facebook.com/docs/marketing-api/guides/lead-ads/retrieving
        """
        from datetime import datetime, timedelta
        since_unix = int((datetime.now() - timedelta(hours=last_hours)).timestamp())

        try:
            result = self._request(
                "GET",
                f"{ad_id}/leads",
                params={
                    "fields": "id,created_time,field_data,ad_id,form_id",
                    "filtering": json.dumps([
                        {"field": "time_created", "operator": "GREATER_THAN", "value": since_unix}
                    ]),
                    "limit": limit,
                }
            )
            return result.get("data", [])
        except Exception as e:
            log.warning(f"Cannot fetch leads for ad {ad_id} (permission leads_retrieval ?): {e}")
            return []

    # ----- Updates -----

    def update_ad_budget(self, ad_id: str, daily_budget: float):
        """Met à jour le budget de l'ad set parent."""
        ad = self._request("GET", ad_id, params={"fields": "adset_id"})
        adset_id = ad["adset_id"]

        self._request(
            "POST",
            adset_id,
            data={"daily_budget": int(daily_budget * 100)}
        )
        log.info(f"Budget update {ad_id} → {daily_budget}€/j")

    def pause_ad(self, ad_id: str):
        self._request("POST", ad_id, data={"status": "PAUSED"})
        log.info(f"Ad pausée: {ad_id}")

    # ----- Validation pré-publish via Meta Preview API -----

    def preview_creative(self, creative_id: str, ad_format: str = "MOBILE_FEED_STANDARD") -> dict:
        """
        Génère un aperçu côté Meta avant publish réel. Si Meta a un problème
        avec la créa (texte trop long, image bizarre, comparaison brand, etc.),
        c'est ici qu'il le dit avant qu'on consomme du budget.

        Renvoie {ok: bool, body: str (HTML preview ou message d'erreur)}.

        Note: Meta Preview API ne fait PAS la validation policy complète. Une
        créa qui passe le preview peut quand même être rejetée par les
        reviewers. Mais le preview catch les erreurs structurelles.
        """
        try:
            result = self._request(
                "GET",
                f"{creative_id}/previews",
                params={"ad_format": ad_format}
            )
            data = result.get("data", [])
            if not data:
                return {"ok": False, "body": "Pas de preview généré (créa rejetée immédiatement par Meta)"}
            return {"ok": True, "body": data[0].get("body", "")[:500]}
        except requests.HTTPError as e:
            err_body = ""
            if e.response is not None:
                try:
                    err_body = e.response.json().get("error", {}).get("message", "")[:500]
                except Exception:
                    err_body = e.response.text[:500]
            return {"ok": False, "body": err_body or str(e)[:500]}
        except Exception as e:
            return {"ok": False, "body": str(e)[:500]}
