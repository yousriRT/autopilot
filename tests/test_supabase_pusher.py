"""Tests pour SupabasePusher — push HTTP vers Supabase REST avec upsert idempotent."""

import json
import pytest
import requests
from unittest.mock import patch, MagicMock
from supabase_pusher import (
    SupabasePusher,
    _flatten_field_data, _normalize_phone_e164, _normalize_postal_code,
)


# ----------- Helpers normalization -----------

class TestFlattenFieldData:
    def test_flattens_meta_format(self):
        result = _flatten_field_data([
            {"name": "email", "values": ["jane@gmail.com"]},
            {"name": "phone_number", "values": ["+15145551234"]},
        ])
        assert result == {"email": "jane@gmail.com", "phone_number": "+15145551234"}

    def test_lowercases_and_underscores_keys(self):
        result = _flatten_field_data([{"name": "Full Name", "values": ["X"]}])
        assert "full_name" in result

    def test_empty_values_skipped(self):
        result = _flatten_field_data([{"name": "email", "values": []}])
        assert "email" not in result

    def test_handles_none_input(self):
        assert _flatten_field_data(None) == {}

    def test_takes_first_value(self):
        result = _flatten_field_data([{"name": "email", "values": ["a@x.com", "b@x.com"]}])
        assert result["email"] == "a@x.com"


class TestNormalizePhone:
    @pytest.mark.parametrize("raw,expected", [
        ("+1 514-555-1234", "+15145551234"),
        ("5145551234", "+15145551234"),
        ("1-514-555-1234", "+15145551234"),
        ("(514) 555-1234", "+15145551234"),
        ("514.555.1234", "+15145551234"),
    ])
    def test_normalizes_canadian_formats(self, raw, expected):
        assert _normalize_phone_e164(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "123", "abc", "555-1234"])
    def test_returns_none_for_invalid(self, raw):
        assert _normalize_phone_e164(raw) is None


class TestNormalizePostal:
    @pytest.mark.parametrize("raw,expected", [
        ("H2X 1Y4", "H2X1Y4"),
        ("h2x1y4", "H2X1Y4"),
        ("M5V 3A8", "M5V3A8"),
    ])
    def test_strips_spaces_uppercases(self, raw, expected):
        assert _normalize_postal_code(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "X"])
    def test_returns_none_for_short(self, raw):
        assert _normalize_postal_code(raw) is None


# ----------- SupabasePusher.is_configured -----------

class TestIsConfigured:
    def test_all_three_present(self):
        p = SupabasePusher(url="https://x.supabase.co", service_key="k", organization_id="o")
        assert p.is_configured() is True

    def test_missing_url(self):
        p = SupabasePusher(url=None, service_key="k", organization_id="o")
        assert p.is_configured() is False

    def test_missing_key(self):
        p = SupabasePusher(url="https://x.supabase.co", service_key=None, organization_id="o")
        assert p.is_configured() is False

    def test_missing_org(self):
        p = SupabasePusher(url="https://x.supabase.co", service_key="k", organization_id=None)
        assert p.is_configured() is False

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://env.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "env_key")
        monkeypatch.setenv("SUPABASE_ORG_ID", "env_org")
        p = SupabasePusher()
        assert p.is_configured() is True
        assert p.url == "https://env.supabase.co"


# ----------- SupabasePusher.push -----------

@pytest.fixture
def configured_pusher():
    return SupabasePusher(
        url="https://test.supabase.co",
        service_key="sk_test",
        organization_id="org_uuid",
    )


def make_meta_lead(lead_id="lead_001"):
    return {
        "id": lead_id,
        "form_id": "form_x",
        "field_data": [
            {"name": "email", "values": ["jane@gmail.com"]},
            {"name": "phone_number", "values": ["+15145551234"]},
            {"name": "full_name", "values": ["Jane Tremblay"]},
            {"name": "postal_code", "values": ["H2X 1Y4"]},
        ],
    }


class TestPushSuccess:
    def test_pushes_with_upsert_header(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost:
            r = MagicMock()
            r.ok = True
            r.status_code = 201
            mpost.return_value = r
            ok = configured_pusher.push(make_meta_lead(), "ad_x", "famille_4lignes", 0.85)
            assert ok is True
            # Vérifie l'header Prefer: merge-duplicates
            headers = mpost.call_args.kwargs["headers"]
            assert "merge-duplicates" in headers["Prefer"]
            assert headers["apikey"] == "sk_test"
            assert headers["Authorization"] == "Bearer sk_test"

    def test_payload_has_normalized_fields(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost:
            r = MagicMock(); r.ok = True; r.status_code = 201
            mpost.return_value = r
            configured_pusher.push(make_meta_lead("lead_xyz"), "ad_abc", "fibre", 0.75)
            payload = json.loads(mpost.call_args.kwargs["data"])
            assert payload["organization_id"] == "org_uuid"
            assert payload["source"] == "meta_lead_ads"
            assert payload["source_lead_id"] == "lead_xyz"
            assert payload["source_ad_id"] == "ad_abc"
            assert payload["vertical"] == "fibre"
            assert payload["customer_phone"] == "+15145551234"
            assert payload["customer_email"] == "jane@gmail.com"
            assert payload["customer_postal_code"] == "H2X1Y4"
            assert payload["quality_score"] == 0.75

    def test_payload_includes_raw_field_data(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost:
            r = MagicMock(); r.ok = True; r.status_code = 201
            mpost.return_value = r
            configured_pusher.push(make_meta_lead(), "ad_x", "fibre", 0.5)
            payload = json.loads(mpost.call_args.kwargs["data"])
            assert "raw_payload" in payload
            assert "field_data" in payload["raw_payload"]

    def test_drops_none_fields_to_avoid_overwriting_on_upsert(self, configured_pusher):
        # Lead minimal sans postal_code → la colonne ne doit PAS être envoyée
        with patch("supabase_pusher.requests.post") as mpost:
            r = MagicMock(); r.ok = True; r.status_code = 201
            mpost.return_value = r
            minimal = {"id": "L1", "field_data": [
                {"name": "email", "values": ["x@gmail.com"]}
            ]}
            configured_pusher.push(minimal, "ad", "fibre", 0.5)
            payload = json.loads(mpost.call_args.kwargs["data"])
            assert "customer_postal_code" not in payload
            assert "customer_phone" not in payload  # absent du lead


class TestPushFailures:
    def test_not_configured_returns_false(self):
        p = SupabasePusher(url=None, service_key=None, organization_id=None)
        assert p.push(make_meta_lead(), "ad", "fibre", 0.5) is False

    def test_no_lead_id_returns_false(self, configured_pusher):
        assert configured_pusher.push({"field_data": []}, "ad", "fibre", 0.5) is False

    def test_4xx_no_retry(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost:
            r = MagicMock()
            r.ok = False
            r.status_code = 400
            r.text = "bad payload"
            r.raise_for_status.side_effect = requests.HTTPError("400", response=r)
            mpost.return_value = r
            ok = configured_pusher.push(make_meta_lead(), "ad", "fibre", 0.5)
            assert ok is False
            # 1 seul appel (pas de retry sur 4xx)
            assert mpost.call_count == 1

    def test_5xx_retries(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost, \
             patch("supabase_pusher.time.sleep" if False else "resilience.time.sleep"):
            r_5xx = MagicMock()
            r_5xx.ok = False
            r_5xx.status_code = 503
            r_5xx.text = "unavailable"
            mpost.return_value = r_5xx
            ok = configured_pusher.push(make_meta_lead(), "ad", "fibre", 0.5)
            assert ok is False
            # Retry 3 fois sur 5xx
            assert mpost.call_count == 3

    def test_connection_error_retries(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost, \
             patch("resilience.time.sleep"):
            mpost.side_effect = requests.ConnectionError("network down")
            ok = configured_pusher.push(make_meta_lead(), "ad", "fibre", 0.5)
            assert ok is False
            assert mpost.call_count == 3

    def test_unexpected_exception_caught(self, configured_pusher):
        with patch("supabase_pusher.requests.post") as mpost:
            mpost.side_effect = RuntimeError("very unexpected")
            ok = configured_pusher.push(make_meta_lead(), "ad", "fibre", 0.5)
            assert ok is False


class TestEdgeCases:
    def test_url_with_trailing_slash_stripped(self):
        p = SupabasePusher(url="https://x.supabase.co/", service_key="k", organization_id="o")
        assert p.url == "https://x.supabase.co"  # pas de slash final

    def test_constants_match_db_schema(self):
        # La SOURCE doit matcher le CHECK constraint de la DB
        assert SupabasePusher.SOURCE == "meta_lead_ads"
        assert SupabasePusher.PATH == "/rest/v1/inbound_leads"
