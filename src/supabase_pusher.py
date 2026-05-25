"""
Supabase Pusher
===============

Push des leads scorés vers la table `public.inbound_leads` du CRM Rappelle
(Supabase) via la REST API auto-générée. Auth via SUPABASE_SERVICE_KEY (bypass RLS).

L'upsert se fait sur la clé unique (organization_id, source, source_lead_id) :
si l'autopilot rejoue (cron qui re-fetch les mêmes leads Meta), pas de doublons.
Le score est mis à jour, le reste reste comme à l'insert initial.

Retry exponentiel 3× sur erreurs réseau / 5xx via le decorator commun.
4xx remontent sans retry (mauvais payload, ne réussira pas en re-tentant).
"""

import os
import json
import logging
import requests
from typing import Optional

from resilience import retry_with_backoff

log = logging.getLogger(__name__)


def _flatten_field_data(field_data: list) -> dict:
    """Aplatit le format Meta [{name, values:[v]}] en dict {name: v}."""
    out = {}
    for f in field_data or []:
        name = (f.get("name") or "").lower().replace(" ", "_")
        values = f.get("values") or []
        if name and values:
            out[name] = values[0]
    return out


def _normalize_phone_e164(raw: str) -> Optional[str]:
    """Normalise un téléphone canadien au format E.164 +1XXXXXXXXXX."""
    if not raw:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1{digits}"
    return None


def _normalize_postal_code(raw: str) -> Optional[str]:
    """Codes postaux QC formatés H2X 1Y4 → H2X1Y4 (matche le check de la table)."""
    if not raw:
        return None
    cleaned = raw.upper().replace(" ", "")
    if 6 <= len(cleaned) <= 7:
        return cleaned
    return None


class SupabasePusher:
    """
    Pousse un lead scoré vers la table inbound_leads du CRM.

    Usage minimal :
        pusher = SupabasePusher(
            url="https://xxx.supabase.co",
            service_key="eyJhbGc...",
            organization_id="82df0449-...",
        )
        pusher.push(lead_data=meta_lead, ad_id="...", vertical="famille_bundle", quality_score=0.85)
    """

    SOURCE = "meta_lead_ads"
    PATH = "/rest/v1/inbound_leads"

    def __init__(
        self,
        url: Optional[str] = None,
        service_key: Optional[str] = None,
        organization_id: Optional[str] = None,
        timeout: int = 15,
    ):
        self.url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.service_key = service_key or os.getenv("SUPABASE_SERVICE_KEY")
        self.organization_id = organization_id or os.getenv("SUPABASE_ORG_ID")
        self.timeout = timeout

    def is_configured(self) -> bool:
        """True si les 3 env vars critiques sont présentes."""
        return bool(self.url and self.service_key and self.organization_id)

    @retry_with_backoff(
        max_attempts=3,
        retryable_exceptions=(requests.ConnectionError, requests.Timeout),
    )
    def _post(self, payload: dict) -> requests.Response:
        """POST avec upsert via header `Prefer: resolution=merge-duplicates`."""
        headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
            # merge-duplicates → upsert sur la contrainte UNIQUE
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        r = requests.post(
            f"{self.url}{self.PATH}",
            headers=headers,
            data=json.dumps(payload),
            timeout=self.timeout,
        )
        if not r.ok:
            log.error(f"Supabase push failed: {r.status_code} {r.text[:300]}")
            # 5xx → ConnectionError pour retry, 4xx → HTTPError sans retry
            if 500 <= r.status_code < 600:
                raise requests.ConnectionError(
                    f"Supabase {r.status_code}: {r.text[:200]}", response=r
                )
            r.raise_for_status()
        return r

    def push(
        self,
        lead_data: dict,
        ad_id: str,
        vertical: str,
        quality_score: float,
    ) -> bool:
        """
        Pousse un lead Meta vers Supabase. Idempotent sur (org, source, lead_id).

        Returns:
            True si pushé (insert ou update), False si erreur ou pas configuré.
        """
        if not self.is_configured():
            log.debug("SupabasePusher non configuré (SUPABASE_URL/SERVICE_KEY/ORG_ID manquants), skip push.")
            return False

        lead_id = lead_data.get("id")
        if not lead_id:
            log.warning("SupabasePusher: lead sans id, skip push")
            return False

        fields = _flatten_field_data(lead_data.get("field_data", []))

        payload = {
            "organization_id": self.organization_id,
            "source": self.SOURCE,
            "source_lead_id": lead_id,
            "source_ad_id": ad_id,
            "source_form_id": lead_data.get("form_id"),
            "vertical": vertical,
            "customer_name": (fields.get("full_name") or fields.get("first_name"))[:120] if fields.get("full_name") or fields.get("first_name") else None,
            "customer_phone": _normalize_phone_e164(fields.get("phone_number") or fields.get("phone") or ""),
            "customer_email": (fields.get("email") or "").lower() or None,
            "customer_postal_code": _normalize_postal_code(fields.get("postal_code") or fields.get("zip_code") or ""),
            "quality_score": round(float(quality_score), 3),
            "raw_payload": {"field_data": lead_data.get("field_data", [])},
        }
        # Dégage les None pour pas écraser des colonnes lors d'un UPDATE upsert
        payload = {k: v for k, v in payload.items() if v is not None}

        try:
            self._post(payload)
            log.info(f"Supabase: lead {lead_id} pushé vers inbound_leads (vertical={vertical}, score={quality_score:.2f})")
            return True
        except requests.HTTPError as e:
            # 4xx : payload invalide. On log et on continue, le lead reste local.
            log.error(f"Supabase push HTTPError {e}")
            return False
        except requests.ConnectionError as e:
            # 5xx ou réseau après 3 retries
            log.error(f"Supabase push ConnectionError après retry: {e}")
            return False
        except Exception as e:
            log.error(f"Supabase push exception inattendue: {e}", exc_info=True)
            return False
